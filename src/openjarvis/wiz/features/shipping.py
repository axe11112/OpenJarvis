"""Whether a finished feature may go to production — a separate question.

§19 of the brief is emphatic and it is the most important sentence in this
module: *do not assume reliability repair auto-merge authority automatically
authorises feature auto-merge.* They look alike and they are not alike.

A repair merge is a change to code that is **already broken in production**,
made to restore behaviour that used to work, verified by the probe that caught
the break. The counterfactual is a site that stays down. A feature merge is a
change to code that is **working fine**, made to add behaviour nobody has ever
depended on, verified by a contract Wiz derived itself. The counterfactual is
waiting until morning.

So feature shipping has its own switch, its own defaults and its own gates, and
:class:`FeatureShippingPolicy` deliberately shares no field with the reliability
merge configuration. Turning one on does not turn the other on, and a test says
so — because the way this fails is not that somebody decides to conflate them,
it is that a helpful refactor notices two similar booleans and merges them.

The shipped defaults ship nothing. ``create_pull_request`` is the only thing on
by default, and only when the authority model separately permits ``PR_WRITE``.
Merging is off at every risk level. Enabling it is a deliberate act by the
operator, in configuration, with the risk level named.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from openjarvis.reliability.briefing import redact_secrets
from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy
from openjarvis.wiz.capabilities import Risk
from openjarvis.wiz.features.model import FeatureRequest, FeatureState

logger = logging.getLogger(__name__)

__all__ = [
    "FeatureShippingPolicy",
    "ShipDecision",
    "ShipGate",
    "FeatureShipper",
    "evaluate_shipping",
]


@dataclass(frozen=True, slots=True)
class ShipGate:
    """One condition, and whether it held."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ShipDecision:
    """Whether this feature may be merged, and every reason considered."""

    allowed: bool
    reason: str
    gates: List[ShipGate] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def refusals(self) -> List[ShipGate]:
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "gates": [g.to_dict() for g in self.gates],
        }

    def explain(self) -> str:
        """Why not, in the operator's language."""
        if self.allowed:
            return "everything needed to merge this is in place"
        blocked = self.refusals
        if not blocked:  # pragma: no cover - defensive
            return self.reason
        return "; ".join(g.detail or g.name for g in blocked)


@dataclass(frozen=True, slots=True)
class FeatureShippingPolicy:
    """When Wiz may merge a feature it built.

    Every field defaults to the safe answer. A missing configuration file must
    mean "ship nothing", never "ship whatever the defaults happen to be" — the
    latter is how an autonomous system acquires authority nobody granted it.

    Deliberately *not* derived from, defaulted from, or cross-referenced with
    the reliability merge settings. See the module docstring.
    """

    #: Open a pull request when a feature reaches READY. The only thing on by
    #: default, and still subject to ``PR_WRITE`` in the authority model.
    create_pull_request: bool = True

    #: Merge a verified LOW-risk feature without asking. Off.
    merge_low_risk: bool = False

    #: Merge a verified MEDIUM-risk feature without asking. Off, and expected
    #: to stay off: "ordinary new behaviour" is precisely the category where a
    #: person reading the diff is worth the delay.
    merge_medium_risk: bool = False

    #: There is no ``merge_high_risk``. HIGH always needs the operator, and the
    #: way to keep that true is to give the configuration no way to say
    #: otherwise.

    #: Required CI contexts on the feature's exact commit. Empty means CI is
    #: not consulted, which is only sane when the local gates are the whole
    #: story; an operator merging automatically should name them.
    required_status_contexts: Sequence[str] = ()

    #: Refuse to merge when the base branch has moved since verification.
    require_base_unmoved: bool = True

    def merge_allowed_for(self, risk: str) -> bool:
        """Whether *risk* is a level this policy will merge at all."""
        level = (risk or "").strip().upper()
        if level == Risk.LOW.value:
            return self.merge_low_risk
        if level == Risk.MEDIUM.value:
            return self.merge_medium_risk
        # HIGH, and anything a future version adds that nobody has classified.
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "create_pull_request": self.create_pull_request,
            "merge_low_risk": self.merge_low_risk,
            "merge_medium_risk": self.merge_medium_risk,
            "merge_high_risk": False,
            "required_status_contexts": list(self.required_status_contexts),
            "require_base_unmoved": self.require_base_unmoved,
        }

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "FeatureShippingPolicy":
        """Read the policy from configuration, defaulting to refusing.

        Unknown keys are ignored with a warning rather than accepted. A typo
        like ``merge_low`` must not silently mean "not configured, so off" in a
        way the operator reads as "configured, and on".
        """
        if not raw:
            return cls()
        known = {
            "create_pull_request",
            "merge_low_risk",
            "merge_medium_risk",
            "required_status_contexts",
            "require_base_unmoved",
        }
        for key in raw:
            if key not in known:
                logger.warning(
                    "unknown feature shipping setting '%s'; ignored. Valid "
                    "settings: %s",
                    key,
                    ", ".join(sorted(known)),
                )
        return cls(
            create_pull_request=bool(raw.get("create_pull_request", True)),
            merge_low_risk=bool(raw.get("merge_low_risk", False)),
            merge_medium_risk=bool(raw.get("merge_medium_risk", False)),
            required_status_contexts=tuple(
                str(c) for c in (raw.get("required_status_contexts") or ())
            ),
            require_base_unmoved=bool(raw.get("require_base_unmoved", True)),
        )


def evaluate_shipping(
    feature: FeatureRequest,
    *,
    policy: FeatureShippingPolicy,
    authority: Optional[AuthorityPolicy] = None,
    pull_request: Optional[Mapping[str, Any]] = None,
    status: Optional[Mapping[str, Any]] = None,
    base_sha_at_verification: str = "",
    observed_base_sha: str = "",
    operator_approved: bool = False,
    medium_risk_approved: bool = False,
) -> ShipDecision:
    """Decide whether *feature* may be merged.

    Pure. Every input is passed in — nothing is read from the network or the
    clock — so the same inputs always give the same answer and every branch is
    reachable from a test. The same discipline the reliability merge decision
    uses, for the same reason: a merge decision that cannot be reproduced
    offline cannot be reviewed.

    ``medium_risk_approved`` is a one-off, per-feature yes for a MEDIUM-risk
    feature the standing ``merge_medium_risk`` policy would otherwise refuse —
    the caller's job, not this function's, to have verified that "yes" is a
    redeemed, fingerprint-bound approval naming this exact feature and head
    SHA (see :meth:`FeaturePipeline._medium_ship_approved`), never a bare
    flag. It changes nothing about HIGH, which keeps its own, separate
    ``operator_approved`` gate exactly as it was; the two are not the same
    knob; and it changes nothing about the standing policy default, which
    stays whatever ``merge_medium_risk`` says regardless of any one feature's
    approval.
    """
    gates: List[ShipGate] = []

    def gate(name: str, passed: bool, detail: str = "") -> None:
        gates.append(ShipGate(name=name, passed=bool(passed), detail=detail))

    risk = (feature.risk or "").strip().upper()

    # -- 1. the switch, which is not reliability's switch ------------------
    if risk == Risk.HIGH.value:
        gate(
            "risk_level_shippable",
            operator_approved,
            "a high-risk feature always needs your approval before it merges"
            if not operator_approved
            else "you approved this high-risk feature",
        )
    else:
        policy_allows = policy.merge_allowed_for(risk)
        one_off_approved = risk == Risk.MEDIUM.value and medium_risk_approved
        allowed = policy_allows or one_off_approved
        if policy_allows:
            detail = f"automatic merging of {risk or 'unclassified'}-risk features is on"
        elif one_off_approved:
            detail = "the owner explicitly approved shipping this specific MEDIUM-risk feature"
        else:
            detail = (
                f"automatic merging of {risk or 'unclassified'}-risk features "
                "is off. Turning on automatic repair of production did not turn "
                "this on; it is a separate setting."
            )
        gate("risk_level_shippable", allowed, detail)

    # -- 2. the feature actually finished ---------------------------------
    gate(
        "feature_ready",
        feature.state is FeatureState.READY,
        f"the feature is {feature.state.value}"
        if feature.state is not FeatureState.READY
        else "the feature reached READY",
    )

    attempt = feature.attempts[-1] if feature.attempts else None
    gate(
        "attempt_recorded",
        attempt is not None,
        "there is a recorded attempt" if attempt else "nothing was built",
    )
    verified_sha = attempt.commit_sha if attempt else ""
    gate(
        "verified_commit_known",
        bool(verified_sha),
        f"verified {verified_sha[:12]}" if verified_sha else "no verified commit",
    )

    acceptance = (
        (feature.metadata.get("verification") or {}) if feature.metadata else {}
    )
    gate(
        "acceptance_passed",
        bool(acceptance.get("passed")),
        acceptance.get("summary", "the acceptance checks did not pass"),
    )
    gate(
        "nothing_awaiting_a_person",
        not acceptance.get("awaiting_a_person"),
        "; ".join(acceptance.get("awaiting_a_person") or []) or "nothing outstanding",
    )

    # -- 3. the thing being merged is the thing that was verified ---------
    pr = dict(pull_request or {})
    gate(
        "pull_request_open",
        str(pr.get("state", "")).lower() == "open",
        f"the pull request is {pr.get('state', 'missing')}",
    )
    head_sha = str(pr.get("head_sha", "") or pr.get("head", {}).get("sha", ""))
    gate(
        "head_is_the_verified_commit",
        bool(head_sha) and bool(verified_sha) and head_sha[:12] == verified_sha[:12],
        (
            f"the branch has moved since I checked it "
            f"({head_sha[:12] or 'unknown'} now, {verified_sha[:12] or 'unknown'} "
            "verified)"
        )
        if head_sha[:12] != verified_sha[:12]
        else f"the pull request head is the verified commit {head_sha[:12]}",
    )
    gate(
        "mergeable",
        pr.get("mergeable") is not False,
        "the pull request has a conflict"
        if pr.get("mergeable") is False
        else "no conflict reported",
    )

    if policy.require_base_unmoved:
        moved = bool(
            base_sha_at_verification
            and observed_base_sha
            and base_sha_at_verification != observed_base_sha
        )
        gate(
            "base_unmoved",
            not moved,
            "the branch this would merge into has changed since I verified against it"
            if moved
            else "the base branch is where it was when I verified",
        )

    # -- 4. CI agrees, on exactly those checks ----------------------------
    if policy.required_status_contexts:
        # GitHubSource.combined_status's "contexts" is a flat list of every
        # context name observed, for diagnostics; the per-context verdict
        # this gate needs is under "required" — see its docstring.
        answered = dict((status or {}).get("required") or {})
        for context in policy.required_status_contexts:
            state = str(answered.get(context, "")).lower()
            gate(
                f"status:{context}",
                state == "success",
                f"{context} is {state or 'missing'}",
            )

    # -- 5. the authority model still has the last word -------------------
    if authority is not None:
        actor = Actor(
            actor_id=feature.actor_id or "wiz",
            channel=_channel_for(feature.source),
            authenticated=True,
        )
        decision = authority.decide(
            actor, Authority.PRODUCTION_CHANGE, capability="feature.ship"
        )
        gate("authority", decision.allowed, decision.reason)

    allowed = all(g.passed for g in gates)
    reason = (
        "every shipping gate passed"
        if allowed
        else "; ".join(g.detail or g.name for g in gates if not g.passed)
    )
    return ShipDecision(allowed=allowed, reason=reason, gates=gates)


@dataclass
class FeatureShipper:
    """Opens the pull request, and merges only if everything says yes.

    The two are separate methods because they are separate authorities. Opening
    a pull request is how a feature becomes visible to a person; merging it is
    how it reaches users. Wiz does the first routinely and the second almost
    never.
    """

    policy: FeatureShippingPolicy
    github: Any
    authority: Optional[AuthorityPolicy] = None
    journal: Any = None
    base_branch: str = "main"

    def open_pull_request(self, feature: FeatureRequest) -> Dict[str, Any]:
        """Open the feature's pull request, if that is permitted.

        Idempotent, because the caller cannot promise this runs exactly once.
        Called a second time for a feature that already has a recorded PR, it
        is a no-op — the common case, and the cheap one to make safe. The
        narrower case is a crash between GitHub answering "created" and this
        process recording the number: on retry, ``create_pull_request`` fails
        (GitHub refuses two open pull requests from the same head branch into
        the same base), and rather than reporting that failure as the last
        word, :meth:`_find_existing_pull_request` looks for the PR that
        failure implies already exists and adopts its number. A restart is
        then a delay, not a duplicate and not a stall.
        """
        if feature.pr_number:
            return {
                "created": False,
                "reason": "already has a pull request",
                "number": feature.pr_number,
                "url": feature.pr_url,
            }

        if not self.policy.create_pull_request:
            return {"created": False, "reason": "opening pull requests is switched off"}

        if self.authority is not None:
            actor = Actor(
                actor_id=feature.actor_id or "wiz",
                channel=_channel_for(feature.source),
                authenticated=True,
            )
            decision = self.authority.decide(
                actor, Authority.PR_WRITE, capability="feature.pull_request"
            )
            if not decision.allowed:
                return {"created": False, "reason": decision.reason}

        if not feature.branch:
            return {"created": False, "reason": "the feature has no branch"}

        try:
            raw = self.github.create_pull_request(
                title=self._title(feature),
                body=pull_request_body(feature),
                head=feature.branch,
            )
        except Exception as exc:
            reconciled = self._find_existing_pull_request(feature)
            if reconciled is not None:
                feature.pr_url = str(reconciled.get("url", ""))
                feature.pr_number = int(reconciled.get("number", 0) or 0)
                self._record(feature, "feature.pull_request_reconciled", feature.pr_url)
                return {
                    "created": False,
                    "reconciled": True,
                    "url": feature.pr_url,
                    "number": feature.pr_number,
                }
            logger.warning("could not open a pull request for %s: %s", feature.id, exc)
            return {"created": False, "reason": str(exc)}

        url = str(raw.get("html_url", "") or raw.get("url", ""))
        number = int(raw.get("number", 0) or 0)
        feature.pr_url = url
        feature.pr_number = number
        self._record(feature, "feature.pull_request_opened", url)
        return {"created": True, "url": url, "number": number}

    def _find_existing_pull_request(
        self, feature: FeatureRequest
    ) -> Optional[Dict[str, Any]]:
        """Look for an already-open PR from this feature's branch.

        Best-effort: called only after ``create_pull_request`` has already
        failed, so any exception here is reported as "could not find it
        either" rather than raised — the original failure is still the one
        that matters if this also comes up empty.
        """
        try:
            open_prs = self.github.list_pull_requests(state="open")
        except Exception as exc:
            logger.warning(
                "could not check for an existing pull request for %s: %s",
                feature.id,
                exc,
            )
            return None
        for pr in open_prs:
            if pr.get("head") == feature.branch:
                return pr
        return None

    def evaluate(self, feature: FeatureRequest, **kwargs: Any) -> ShipDecision:
        return evaluate_shipping(
            feature, policy=self.policy, authority=self.authority, **kwargs
        )

    def merge_feature(
        self,
        feature: FeatureRequest,
        *,
        pull_request: Mapping[str, Any],
        status: Optional[Mapping[str, Any]] = None,
        base_sha_at_verification: str = "",
        observed_base_sha: str = "",
        operator_approved: bool = False,
        medium_risk_approved: bool = False,
    ) -> Dict[str, Any]:
        """Merge the feature's pull request, if every gate and authority agree.

        ``evaluate_shipping`` is pure and cannot ask GitHub anything; this
        method is the one place that does, and it asks two questions
        ``evaluate_shipping`` has no way to answer on its own.

        The first is the same authority question, re-asked immediately before
        the write rather than trusted from an earlier call, because a decision
        computed even a second ago is a decision about a world that may have
        changed.

        The second is new: whether the credential this process is actually
        running with can push to this repository at all. A repository's
        collaborator list can say "maintainer" while the token in the
        environment is scoped read-only — those are different facts, and only
        asking GitHub what the token itself may do (``can_write()``, which
        reads the API's own ``permissions`` block for this token rather than
        inferring it from role metadata) answers the one that matters. Refusing
        here, before attempting the merge, means a scoped-down token produces a
        clear refusal instead of an opaque 403 three gates later.

        TOCTOU beyond that is closed by GitHub itself: ``merge_pull_request``
        sends the verified commit as the expected head and the API refuses the
        merge server-side if the branch moved between this call and its own
        read — see :meth:`GitHubSource.merge_pull_request`.
        """
        decision = self.evaluate(
            feature,
            pull_request=pull_request,
            status=status,
            base_sha_at_verification=base_sha_at_verification,
            observed_base_sha=observed_base_sha,
            operator_approved=operator_approved,
            medium_risk_approved=medium_risk_approved,
        )
        if not decision.allowed:
            return {
                "merged": False,
                "reason": decision.explain(),
                "gates": decision.to_dict(),
            }

        try:
            can_write = bool(self.github.can_write())
        except Exception as exc:
            reason = f"could not verify GitHub write permission: {exc}"
            logger.warning("refusing merge of %s: %s", feature.id, reason)
            self._record(feature, "feature.merge_refused", reason)
            return {"merged": False, "reason": reason}
        if not can_write:
            reason = (
                "the configured GitHub token does not have push permission on "
                "this repository, so I am not attempting the merge — a "
                "collaborator role is not the same fact as what this token "
                "may actually do"
            )
            logger.warning("refusing merge of %s: %s", feature.id, reason)
            self._record(feature, "feature.merge_refused", reason)
            return {"merged": False, "reason": reason}

        attempt = feature.attempts[-1] if feature.attempts else None
        verified_sha = attempt.commit_sha if attempt else ""
        number = feature.pr_number or int(pull_request.get("number", 0) or 0)
        if not number:
            reason = "no pull request number to merge"
            self._record(feature, "feature.merge_refused", reason)
            return {"merged": False, "reason": reason}

        try:
            result = self.github.merge_pull_request(
                number=number,
                expected_head_sha=verified_sha,
                method="squash",
                title=self._title(feature),
            )
        except Exception as exc:
            reason = str(exc)
            logger.warning("merge of %s failed: %s", feature.id, reason)
            self._record(feature, "feature.merge_failed", reason)
            return {
                "merged": False,
                "reason": reason,
                "permission_error": "403" in reason or "permission" in reason.lower(),
            }

        if not result.get("merged"):
            reason = (
                result.get("message") or "GitHub reported the merge did not complete"
            )
            self._record(feature, "feature.merge_refused", reason)
            return {"merged": False, "reason": reason}

        sha = result.get("sha", "")
        self._record(feature, "feature.merged", sha)
        return {"merged": True, "sha": sha}

    def _title(self, feature: FeatureRequest) -> str:
        return f"{feature.title} ({feature.id})"

    def _record(self, feature: FeatureRequest, kind: str, reason: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record(
                at="",
                kind=kind,
                capability="feature.ship",
                actor_id=feature.actor_id,
                channel=feature.source,
                reason=reason[:500],
                detail={"feature_id": feature.id, "risk": feature.risk},
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not journal a shipping event")


def pull_request_body(feature: FeatureRequest, *, max_chars: int = 6000) -> str:
    """The pull request description.

    §18 asks for concise. The reviewer needs what was asked for, what was done,
    how risky it is, what proved it and where to look — and nothing else. A
    generated essay is a description nobody reads, which is worse than a short
    one.
    """
    verification = feature.metadata.get("verification") or {}
    review = feature.metadata.get("review") or {}
    attempt = feature.attempts[-1] if feature.attempts else None

    lines: List[str] = [
        f"**{feature.id}** — requested via {feature.source or 'unknown'}",
        "",
        "### What was asked for",
        f"> {feature.operator_request}",
        "",
        f"### Risk: {feature.risk}",
    ]
    reasons = feature.metadata.get("risk_reasons") or []
    if reasons:
        lines.extend(f"- {reason}" for reason in list(reasons)[:5])

    if attempt:
        lines.extend(
            [
                "",
                "### What changed",
                f"{len(attempt.changed_files)} file(s), "
                f"{attempt.lines_changed} line(s), "
                f"attempt {attempt.number} of {len(feature.attempts)}",
                "",
            ]
        )
        lines.extend(f"- `{path}`" for path in attempt.changed_files[:20])
        if len(attempt.changed_files) > 20:
            lines.append(f"- … and {len(attempt.changed_files) - 20} more")

    gates = (feature.metadata.get("gates") or {}).get("summary", "")
    if gates:
        lines.extend(["", "### Checks", gates])

    if verification:
        lines.extend(["", "### Verification", verification.get("summary", "")])
        if feature.preview_url:
            lines.append(f"Preview: {feature.preview_url}")
        shots = verification.get("screenshots") or {}
        if shots:
            lines.append(
                "Screenshots: "
                + ", ".join(f"{name} ({len(paths)})" for name, paths in shots.items())
            )
        outstanding = verification.get("awaiting_a_person") or []
        if outstanding:
            lines.append("")
            lines.append("**Still needs a person:**")
            lines.extend(f"- {item}" for item in outstanding)

    if review.get("ran"):
        lines.extend(
            [
                "",
                "### Independent review (advisory)",
                str(review.get("text", ""))[:1500],
            ]
        )

    lines.extend(
        [
            "",
            "---",
            "_Generated by [Claude Code](https://claude.ai/code)_",
        ]
    )
    # Redacted before truncation, not after: several fields above are prose a
    # model wrote after reading the repository and running its test suite —
    # exactly the case redact_secrets() exists for (see its docstring). This
    # body is about to become a real GitHub pull request; a secret that
    # reached this far and were merely truncated could still have its
    # earliest characters exposed.
    body = redact_secrets("\n".join(lines))
    return body if len(body) <= max_chars else body[:max_chars] + "\n… (truncated)"


def _channel_for(source: str) -> Any:
    from openjarvis.wiz.authority import Channel

    try:
        return Channel(source)
    except ValueError:
        return Channel.AUTONOMOUS
