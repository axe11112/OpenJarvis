"""The Control Center's product-development API.

Every endpoint here goes through :meth:`openjarvis.wiz.brain.Wiz.handle` with
``Channel.CONTROL_CENTER`` on the actor. None of them reach a pipeline directly,
so the authority model, the risk gate and the journal apply to a button in the
dashboard exactly as they apply to a sentence typed at a terminal. A route that
skipped the brain would be a second authority path, and the second one is always
the one that turns out to be missing a check.

The Control Center is the only channel whose ceiling includes
``PRODUCTION_CHANGE``, because it is the only one that authenticates the
operator and can show them exactly what they are approving. That makes these
routes the most consequential surface in the system, which is why they are
thin: they translate HTTP into a ``Request`` and translate an ``Outcome`` back
into JSON, and they contain no decisions of their own.

Building is deliberately asynchronous. A feature takes minutes to an hour, and
an HTTP request that waits for it is an HTTP request that times out — so
``POST /api/wiz/features/{id}/build`` starts the work in a background thread and
returns immediately. The page polls. What the operator sees is the pipeline's
own state, read from the store, rather than anything this module invents.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

try:
    from fastapi import APIRouter, Body, HTTPException, Query
except ImportError:  # pragma: no cover - server extra not installed
    raise ImportError("fastapi is required for the Wiz routes")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wiz", tags=["wiz"])

#: Built once and reused. Building it per request would re-open the feature
#: database and re-probe the CLI on every poll, which on this hardware is the
#: difference between a dashboard that feels instant and one that does not.
_RUNTIME: Any = None
_RUNTIME_LOCK = threading.Lock()

#: Features currently being driven in the background, so a second click does
#: not start a second pipeline for the same request.
_RUNNING: Dict[str, threading.Thread] = {}


def reset_state() -> None:
    """Drop the cached runtime. For tests, and for a config reload."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = None
        _RUNNING.clear()


def _runtime() -> Any:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            from openjarvis.core.config import load_config
            from openjarvis.wiz.assemble import assemble
            from openjarvis.wiz.runtime import build_wiz

            config = load_config()
            _RUNTIME = build_wiz(
                config=config,
                product=assemble(config=config),
                # Wired the same way the CLI wires them (see
                # `cli/wiz_cmd.py::_runtime`), so `/api/wiz/health` and
                # `jarvis wiz doctor` answer from the same checks rather than
                # two implementations of "is Wiz healthy" drifting apart.
                watcher_status=_watcher_status_probe(config),
                ledger=_notification_ledger(config),
            )
        return _RUNTIME


def _watcher_status_probe(config: Any) -> Any:
    try:
        from openjarvis.reliability.dashboard.supervisor import LaunchdSupervisor

        return LaunchdSupervisor(config).status
    except Exception:  # noqa: BLE001 - health reporting must not block startup
        return None


def _notification_ledger(config: Any) -> Any:
    try:
        from openjarvis.reliability.notify_ledger import NotificationLedger, ledger_path

        return NotificationLedger(path=ledger_path(config))
    except Exception:  # noqa: BLE001
        return None


def _handle(
    capability: str, *, text: str = "", approved: bool = False, **arguments: Any
) -> Any:
    from openjarvis.wiz.authority import Channel
    from openjarvis.wiz.brain import Request
    from openjarvis.wiz.runtime import operator

    runtime = _runtime()
    request = Request(
        text=text,
        actor=operator(Channel.CONTROL_CENTER),
        arguments=arguments,
        # Only the Control Center may set this, and only for the action it
        # actually showed the operator. It is a flag on the request rather than
        # a parameter of the capability so that no other channel can construct
        # one: every other caller builds a ``Request`` without it.
        approved=approved,
    )
    return runtime.wiz.handle(request, capability=capability)


def _result_or_error(outcome: Any) -> Dict[str, Any]:
    """Translate an outcome into JSON, preserving refusals as refusals.

    A refusal is a 200 with ``allowed: false`` rather than a 403. The operator
    is not unauthorised — they are being told that *Wiz* is, and the page has to
    render that sentence rather than an error banner.
    """
    if outcome.handled:
        return {"ok": True, "result": outcome.result}
    return {"ok": False, "message": outcome.message}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@router.get("/status")
def get_status() -> Dict[str, Any]:
    """What Wiz can do here, and why not, if not."""
    from openjarvis.wiz.assemble import describe

    runtime = _runtime()
    report = describe()
    return {
        "configured": runtime.product is not None,
        "can_build": report["can_build"],
        "can_verify": report["can_verify"],
        "checks": report["checks"],
        "shipping": report["shipping"],
        "capabilities": runtime.registry.describe(),
    }


@router.get("/health")
def get_health() -> Dict[str, Any]:
    """Wiz's own health — the watcher, the coding tool, the audit trail.

    Deliberately a separate route from `/api/snapshot`'s reliability panel,
    the same way `wiz/health.py` is a separate module from
    `reliability/diagnostic.py`: a green answer here says nothing about
    whether Wize is up, and a red one here can coexist with a perfectly
    healthy site.
    """
    outcome = _handle("wiz.health")
    return outcome.result if outcome.handled else {"available": False}


@router.get("/features")
def list_features() -> Dict[str, Any]:
    """Everything in progress, ready, or waiting for the operator."""
    return _result_or_error(_handle("feature.list"))


@router.get("/features/{feature_id}")
def get_feature(feature_id: str) -> Dict[str, Any]:
    """One request, in full."""
    outcome = _handle("feature.status", feature_id=feature_id)
    payload = _result_or_error(outcome)
    if payload["ok"] and not payload["result"].get("available"):
        raise HTTPException(status_code=404, detail=payload["result"].get("detail"))
    return payload


@router.get("/memory")
def get_memory(
    query: str = Query("", description="Free text; empty returns what is recent."),
    limit: int = Query(10, ge=1, le=50),
) -> Dict[str, Any]:
    """What was built, decided and learned."""
    if query.strip():
        return _result_or_error(_handle("product.search", query=query, limit=limit))
    return _result_or_error(_handle("product.recent", limit=limit))


@router.get("/morning")
def get_morning() -> Dict[str, Any]:
    """This morning's summary, and whether it is worth sending anywhere.

    ``worth_sending`` is returned rather than acted on. Nothing in this process
    delivers it; who does, and whether they are allowed to, is a separate
    decision made somewhere that holds an authority.
    """
    from openjarvis.wiz.briefing import compose

    runtime = _runtime()
    pipeline = getattr(getattr(runtime, "product", None), "pipeline", None)

    def reliability_status() -> Any:
        outcome = _handle("reliability.status")
        return outcome.result if outcome.handled else {"available": False}

    briefing = compose(
        store=getattr(pipeline, "store", None),
        memory=getattr(getattr(runtime, "product", None), "memory", None),
        reliability=reliability_status,
        site_name="Wize",
    )
    return {"ok": True, "result": briefing.to_dict(), "text": briefing.render()}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@router.post("/features")
def create_feature(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Record a request, and start it unless asked not to.

    The two halves are separate capabilities with separate authorities, so a
    Control Center that may record but not build gets the first and is told
    about the second.
    """
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="say what you would like built")

    recorded = _handle(
        "feature.request",
        text=text,
        title=str(payload.get("title", "")),
        priority="P2" if payload.get("urgent") else "P3",
    )
    if not recorded.handled:
        return {"ok": False, "message": recorded.message}
    result = recorded.result
    if not result.get("recorded"):
        return {"ok": False, "message": result.get("detail", "nothing was recorded")}

    if payload.get("record_only"):
        return {"ok": True, "result": result, "started": False}

    started = _start(result["id"])
    return {"ok": True, "result": result, "started": started["started"], **started}


@router.post("/features/{feature_id}/build")
def build_feature(feature_id: str) -> Dict[str, Any]:
    """Start (or resume) work on a recorded request."""
    return _start(feature_id)


@router.post("/features/{feature_id}/ship")
def ship_feature(
    feature_id: str, payload: Optional[Dict[str, Any]] = Body(None)
) -> Dict[str, Any]:
    """Merge a READY feature and prove it in production, or say why not.

    The one route on this authority: ``run`` (via ``/build``) never reaches
    past ``READY`` on its own — see ``FeaturePipeline.ship``'s own docstring
    — so without this endpoint nothing on any channel could ever call it.
    ``Authority.PRODUCTION_CHANGE`` is checked here for the same reason
    ``_start`` checks ``CODE_WRITE`` before ``/build``: a refusal belongs in
    the response the operator clicked for, not discovered later by polling.
    ``ship`` re-checks the same authority itself before it merges anything,
    so this is belt, not the only suspenders.
    """
    runtime = _runtime()
    if runtime.product is None:
        return {"started": False, "message": "feature work is not configured here"}

    with _RUNTIME_LOCK:
        existing = _RUNNING.get(feature_id)
        if existing is not None and existing.is_alive():
            return {"started": False, "message": "I am already working on that"}

    from openjarvis.wiz.authority import Authority, Channel
    from openjarvis.wiz.runtime import operator

    decision = runtime.policy.decide(
        operator(Channel.CONTROL_CENTER),
        Authority.PRODUCTION_CHANGE,
        capability="feature.ship",
    )
    if not decision.allowed:
        return {"started": False, "message": f"I am not allowed to: {decision.reason}"}

    pipeline = runtime.product.pipeline
    operator_approved = bool((payload or {}).get("operator_approved", False))

    def drive() -> None:
        try:
            pipeline.ship(feature_id, operator_approved=operator_approved)
        except Exception:  # pragma: no cover - the thread must not die silently
            logger.exception("shipping %s failed in the background", feature_id)
        finally:
            with _RUNTIME_LOCK:
                _RUNNING.pop(feature_id, None)

    thread = threading.Thread(target=drive, name=f"wiz-ship-{feature_id}", daemon=True)
    with _RUNTIME_LOCK:
        _RUNNING[feature_id] = thread
    thread.start()
    return {"started": True, "message": "shipping it"}


@router.post("/features/{feature_id}/approve")
def approve_feature(
    feature_id: str, payload: Optional[Dict[str, Any]] = Body(None)
) -> Dict[str, Any]:
    """Approve a HIGH-risk feature for building, bound to the plan shown.

    The approval names the plan the operator was shown. If the plan changes —
    because a re-plan produced a different one — the approval no longer matches
    anything, and the feature stops again rather than proceeding on consent
    given for something else.
    """
    runtime = _runtime()
    if runtime.product is None:
        raise HTTPException(status_code=404, detail="feature work is not configured")

    pipeline = runtime.product.pipeline
    feature = pipeline.store.get(feature_id)
    if feature is None:
        raise HTTPException(status_code=404, detail=f"no request {feature_id}")

    approvals = pipeline.approvals
    if approvals is None:
        raise HTTPException(
            status_code=409,
            detail="approvals are not configured, so I cannot record your consent",
        )

    from openjarvis.wiz.features.acceptance import contract_for
    from openjarvis.wiz.features.pipeline import _digest

    contract = contract_for(
        feature_id=feature.id,
        request=feature.operator_request,
        plan=feature.plan,
        gates=list(pipeline.profile.configured_gates),
    )
    approval = approvals.issue(
        capability="feature.build",
        subject=feature.id,
        parameters={
            "plan": _digest(feature.plan),
            "risk": feature.risk,
            "acceptance": contract.describe(),
        },
        actor_id="operator",
        channel="control_center",
        summary=(payload or {}).get("summary", f"approved {feature.id} for build"),
    )
    feature.metadata["approval_token"] = approval.token
    pipeline.store.save(feature)

    started = _start(feature_id)
    return {"ok": True, "approved": True, **started}


@router.post("/features/{feature_id}/cancel")
def cancel_feature(feature_id: str) -> Dict[str, Any]:
    """Stop work on a request. The worktree is kept for inspection.

    Delegates to :meth:`FeaturePipeline.cancel` rather than moving the state
    machine here a second time — the same reason `feature.build` calls into the
    pipeline instead of opening a worktree itself. The Telegram/voice path
    reaches the identical method through `feature.cancel`, so a stop from the
    dashboard and a stop from a phone are audited the same way.
    """
    runtime = _runtime()
    if runtime.product is None:
        raise HTTPException(status_code=404, detail="feature work is not configured")

    try:
        feature = runtime.product.pipeline.cancel(
            feature_id, reason="cancelled by the operator (Control Center)"
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no request {feature_id}")
    return {"ok": True, "state": feature.state.value}


# ---------------------------------------------------------------------------
# Running in the background
# ---------------------------------------------------------------------------


def _start(feature_id: str) -> Dict[str, Any]:
    """Drive *feature_id* in a background thread, unless it already is.

    The authority check happens here, on this thread, before anything is
    started — so a refusal is something the operator sees in the response
    rather than something that happens silently in a thread nobody is watching.
    """
    runtime = _runtime()
    if runtime.product is None:
        return {"started": False, "message": "feature work is not configured here"}

    with _RUNTIME_LOCK:
        existing = _RUNNING.get(feature_id)
        if existing is not None and existing.is_alive():
            return {"started": False, "message": "I am already working on that"}

    # Ask the brain whether this is allowed *before* spawning anything. The
    # handler itself is cheap when the answer is no.
    from openjarvis.wiz.authority import Authority, Channel
    from openjarvis.wiz.runtime import operator

    decision = runtime.policy.decide(
        operator(Channel.CONTROL_CENTER),
        Authority.CODE_WRITE,
        capability="feature.build",
    )
    if not decision.allowed:
        return {"started": False, "message": f"I am not allowed to: {decision.reason}"}

    pipeline = runtime.product.pipeline

    def drive() -> None:
        try:
            pipeline.run(feature_id)
        except Exception:  # pragma: no cover - the thread must not die silently
            logger.exception("feature %s failed in the background", feature_id)
        finally:
            with _RUNTIME_LOCK:
                _RUNNING.pop(feature_id, None)

    thread = threading.Thread(target=drive, name=f"wiz-{feature_id}", daemon=True)
    with _RUNTIME_LOCK:
        _RUNNING[feature_id] = thread
    thread.start()
    return {"started": True, "message": "working on it"}
