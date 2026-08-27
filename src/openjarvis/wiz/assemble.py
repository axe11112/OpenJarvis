"""Building the whole thing: settings on disk to a Wiz that can be asked to build.

One function, :func:`assemble`, and it is deliberately the only place the
product-development side is wired together. Every module below it takes its
collaborators as arguments and knows nothing about configuration; this is where
a settings file becomes a pipeline with a worktree, a browser and a preview
observer attached.

Keeping the assembly in one function has a specific payoff: the answer to "can
Wiz build something right now, and if not why not" is a single readable
sequence, and :func:`describe` returns it. An operator who is told "I cannot
build features" and nothing else has been given a puzzle rather than an answer.

Nothing here is imported at module scope from the heavy end of the codebase.
Vercel, GitHub and Playwright are pulled in only when the settings say they are
configured, so a machine with none of them still starts.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from openjarvis.wiz.approvals import ApprovalStore
from openjarvis.wiz.features.acceptance import Viewport
from openjarvis.wiz.features.engineer import ClaudeCodeEngineeringAgent
from openjarvis.wiz.features.model import FeatureState
from openjarvis.wiz.features.pipeline import FeaturePipeline
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.queue import DevelopmentQueue
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.features.workspace import FeatureWorkspace
from openjarvis.wiz.memory import ProductMemory
from openjarvis.wiz.product import ProductVerbs
from openjarvis.wiz.settings import SETTINGS_FILENAME, WizSettings, load_settings

logger = logging.getLogger(__name__)

__all__ = ["assemble", "describe"]


def assemble(
    *,
    home: Optional[Path] = None,
    settings: Optional[WizSettings] = None,
    config: Any = None,
    production_busy: Optional[Callable[[], bool]] = None,
) -> Optional[ProductVerbs]:
    """Build the product-development side, or return ``None`` if it cannot be.

    ``None`` rather than a half-built object that fails later. A Wiz that
    declares ``feature.build`` and then cannot open a worktree has told the
    operator something false; a Wiz with no such verb has told them the truth.
    """
    from openjarvis.wiz.runtime import wiz_home

    root = Path(home) if home is not None else wiz_home()
    resolved = (
        settings if settings is not None else load_settings(root / SETTINGS_FILENAME)
    )

    profile = resolved.profile()
    if profile is None:
        logger.info("no engineering target is configured; Wiz will not build anything")
        return None
    if not profile.complete:
        logger.warning(
            "engineering target '%s' has no test command or no checkout, so "
            "nothing built against it could be proved; not enabling feature work",
            profile.name,
        )
        return None

    store = FeatureStore(root / "features.db")
    memory = ProductMemory(root / "memory.db")

    workspace = FeatureWorkspace(
        repo_path=profile.checkout,
        root=str(Path(resolved.worktree_root).expanduser()),
        protected_checkouts=resolved.all_protected(),
        git_identity=resolved.git_identity(),
    )

    engineer = ClaudeCodeEngineeringAgent()
    queue = DevelopmentQueue(max_concurrent=1, production_busy=production_busy)

    # In memory, and deliberately so: an approval that survives a restart is
    # consent granted to a process the operator has not seen since.
    approvals = ApprovalStore(clock=time.monotonic)

    verifier = _verifier(Path(resolved.evidence_root).expanduser())

    pipeline = FeaturePipeline(
        store=store,
        profile=profile,
        engineer=engineer,
        workspace=workspace,
        check_suite_factory=_check_suite_factory,
        preview=_preview_observer(config, profile),
        verifier=verifier,
        queue=queue,
        approvals=approvals,
        journal=None,  # set by build_wiz, which owns the journal
        max_attempts=resolved.max_attempts,
        reviewer=_reviewer(engineer, workspace)
        if resolved.independent_review
        else None,
        shipper=_shipper(resolved, config, profile),
        # journal likewise set by build_wiz, once construction has succeeded.
        postship=_postship(config, profile, verifier),
    )

    return ProductVerbs(
        pipeline=pipeline,
        memory=memory,
        runner=lambda feature_id: _run_and_auto_ship(pipeline, config, feature_id),
    )


def describe(
    *, home: Optional[Path] = None, settings: Optional[WizSettings] = None
) -> Dict[str, Any]:
    """Why Wiz can or cannot build, step by step.

    Returned as an ordered list of checks rather than a single boolean, because
    "I cannot build features" is a puzzle and "there is no test command for
    target 'wize'" is an answer.
    """
    from openjarvis.wiz.runtime import claude_cli_available, wiz_home

    root = Path(home) if home is not None else wiz_home()
    resolved = (
        settings if settings is not None else load_settings(root / SETTINGS_FILENAME)
    )
    profile = resolved.profile()

    checks = [
        {
            "name": "settings",
            "ok": (root / SETTINGS_FILENAME).is_file(),
            "detail": str(root / SETTINGS_FILENAME),
        },
        {
            "name": "target",
            "ok": profile is not None,
            "detail": profile.name if profile else "no engineering target configured",
        },
        {
            "name": "checkout",
            "ok": bool(profile and Path(profile.checkout).expanduser().is_dir()),
            "detail": profile.checkout if profile else "",
        },
        {
            "name": "gates",
            "ok": bool(profile and profile.complete),
            "detail": ", ".join(profile.configured_gates) if profile else "",
        },
        {
            "name": "coding_engine",
            "ok": claude_cli_available().configured,
            "detail": claude_cli_available().detail,
        },
        {
            "name": "browser",
            "ok": _playwright_available(),
            "detail": (
                "Playwright is installed"
                if _playwright_available()
                else "Playwright is not installed, so I cannot check a preview"
            ),
        },
    ]
    return {
        "can_build": all(c["ok"] for c in checks[:5]),
        "can_verify": all(c["ok"] for c in checks),
        "checks": checks,
        "shipping": resolved.shipping.to_dict(),
    }


# ---------------------------------------------------------------------------
# The optional halves
# ---------------------------------------------------------------------------


def _check_suite_factory(profile: EngineeringProfile) -> Any:
    from openjarvis.reliability.checks import CheckSuite

    node_bin_dir = profile.resolve_node_bin_dir()
    if profile.node_version and not node_bin_dir:
        logger.warning(
            "target '%s' asks for node_version=%r but no matching Node was "
            "found on this machine; checks will run under whatever `node` "
            "this process already has",
            profile.name,
            profile.node_version,
        )
    return CheckSuite.from_config(
        **profile.check_commands(),
        path_prepend=[node_bin_dir] if node_bin_dir else None,
    )


def _run_and_auto_ship(pipeline: FeaturePipeline, config: Any, feature_id: str) -> Any:
    """What every intake channel gets for free: ``run``, then the one thing it
    never does on its own.

    :meth:`~openjarvis.wiz.features.pipeline.FeaturePipeline.run` stops dead at
    ``READY`` — deliberately, and that is not renegotiable here. Reaching
    production without a person is a *second*, separate decision, made once in
    advance in :class:`~openjarvis.wiz.features.shipping.FeatureShippingPolicy`
    rather than implicitly by whoever happened to type the request. This is
    where that second decision actually gets asked, for every caller of
    ``ProductVerbs.runner`` alike — the CLI, and anything else built on the
    same capability — so "genuinely LOW-risk work may go all the way on its
    own" is true of Wiz's real intake, not only of a path the HTTP server
    happens to also take.

    :meth:`~FeaturePipeline.auto_ship_if_eligible` is already a strict no-op
    for anything not sitting at ``READY`` with LOW risk and ``merge_low_risk``
    on, so calling it unconditionally after every ``run`` costs nothing on the
    far more common paths that end in ``HUMAN_REQUIRED`` or a still-MEDIUM
    ``READY``.
    """
    feature = pipeline.run(feature_id)
    if feature.state is not FeatureState.READY:
        return feature
    return pipeline.auto_ship_if_eligible(
        feature_id,
        emergency_stop_engaged=lambda: _emergency_stop_engaged(config),
        reliability_busy=lambda: _reliability_busy(pipeline),
        audit_healthy=lambda: _audit_healthy(pipeline.journal),
    )


def _emergency_stop_engaged(config: Any) -> bool:
    """Whether the operator's one emergency stop is pulled.

    The exact same flag file the reliability watcher honours — not a second
    switch a panicked operator would have to remember exists. Unreadable is
    treated as engaged: a stop this process cannot confirm is clear is not a
    stop this process should ship features through.
    """
    try:
        from openjarvis.reliability.watch import stop_flag_path

        return stop_flag_path(config).is_file()
    except Exception:  # noqa: BLE001
        return True


def _audit_healthy(journal: Any) -> bool:
    """Whether Wiz's own hash-chained journal still checks out.

    No journal attached is not "healthy" here — it is nothing this process can
    vouch for, which for an autonomous merge decision is the same refusal.
    """
    if journal is None:
        return False
    try:
        intact, _break_at = journal.verify()
        return bool(intact)
    except Exception:  # noqa: BLE001
        return False


def _reliability_busy(pipeline: Any) -> bool:
    """Whether the development queue currently defers to production."""
    queue = getattr(pipeline, "queue", None)
    if queue is None:
        return False
    try:
        return bool(queue.snapshot().get("production_busy", False))
    except Exception:  # noqa: BLE001
        return True


def _preview_observer(config: Any, profile: EngineeringProfile) -> Any:
    """A preview observer, when the target actually has previews."""
    if profile.preview_provider != "vercel":
        return None
    try:
        from openjarvis.reliability.sources.vercel import VercelSource
        from openjarvis.wiz.features.preview import PreviewObserver
    except ImportError as exc:  # pragma: no cover - depends on extras
        logger.warning("no preview support available: %s", exc)
        return None

    vercel_config = getattr(getattr(config, "reliability", None), "vercel", None)
    if vercel_config is None or not getattr(vercel_config, "project_id", ""):
        logger.info("Vercel is not configured, so I cannot observe previews")
        return None
    try:
        source = VercelSource(
            project_id=vercel_config.project_id,
            team_id=getattr(vercel_config, "team_id", ""),
        )
    except Exception as exc:
        logger.warning("could not build the Vercel client: %s", exc)
        return None
    return PreviewObserver(vercel=source)


def _verifier(evidence_root: Path) -> Any:
    """The browser verifier, when a browser exists."""
    if not _playwright_available():
        return None
    from openjarvis.reliability.probes.browser import BrowserProbeRunner
    from openjarvis.wiz.features.verification import FeatureVerifier

    def runner_for(viewport: Viewport) -> Any:
        # A fresh runner per viewport: the size is a constructor argument, and
        # the mobile pass has to be a genuinely different browser context.
        return BrowserProbeRunner(headless=True, viewport=viewport.size)

    return FeatureVerifier(runner_factory=runner_for, evidence_root=evidence_root)


def _postship(config: Any, profile: EngineeringProfile, verifier: Any) -> Any:
    """Proves a merged feature in production, when previews are configured.

    Reuses the same Vercel client :func:`_preview_observer` builds and the same
    browser verifier the preview stage used — see the module docstring on
    :class:`~openjarvis.wiz.features.postship.PostShipVerifier` for why that
    reuse is the point rather than a shortcut.
    """
    if profile.preview_provider != "vercel":
        return None
    try:
        from openjarvis.reliability.sources.vercel import VercelSource
        from openjarvis.wiz.features.postship import PostShipVerifier
        from openjarvis.wiz.features.preview import PreviewObserver
    except ImportError as exc:  # pragma: no cover - depends on extras
        logger.warning("no post-ship support available: %s", exc)
        return None

    vercel_config = getattr(getattr(config, "reliability", None), "vercel", None)
    if vercel_config is None or not getattr(vercel_config, "project_id", ""):
        return None
    try:
        source = VercelSource(
            project_id=vercel_config.project_id,
            team_id=getattr(vercel_config, "team_id", ""),
        )
    except Exception as exc:
        logger.warning("could not build the Vercel client for post-ship: %s", exc)
        return None
    return PostShipVerifier(
        deployments=PreviewObserver(vercel=source, target="production"),
        verifier=verifier,
    )


def _reviewer(engineer: Any, workspace: Any) -> Any:
    from openjarvis.wiz.features.review import IndependentReviewer

    return IndependentReviewer(engineer=engineer, workspace=workspace)


def _shipper(settings: WizSettings, config: Any, profile: EngineeringProfile) -> Any:
    """The pull-request opener, when GitHub is configured."""
    github_config = getattr(getattr(config, "reliability", None), "github", None)
    if github_config is None or not getattr(github_config, "repo", ""):
        return None
    try:
        from openjarvis.reliability.sources.github import GitHubSource
        from openjarvis.wiz.features.shipping import FeatureShipper
    except ImportError as exc:  # pragma: no cover - depends on extras
        logger.warning("no GitHub support available: %s", exc)
        return None
    try:
        source = GitHubSource(
            repo=github_config.repo,
            token_env=getattr(github_config, "token_env", "GITHUB_READONLY_TOKEN"),
            base_branch=profile.base_branch or "main",
            # Feature branches, not incident branches. A reviewer scanning
            # `git branch` should be able to tell which is which without
            # looking anything up.
            branch_prefix="wiz/feature/",
            # Never, whatever else is configured. Pull requests are how a
            # feature becomes visible to a person; pushing to the base branch
            # skips the person.
            allow_push_to_default_branch=False,
            protected_paths=list(profile.protected_paths),
        )
    except Exception as exc:
        logger.warning("could not build the GitHub client: %s", exc)
        return None
    return FeatureShipper(
        policy=settings.shipping,
        github=source,
        base_branch=profile.base_branch or "main",
    )


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True
