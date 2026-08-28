"""What the Control Center shows about Wiz's own half.

Lives on the Wiz side of the reliability/wiz boundary on purpose:
:mod:`openjarvis.reliability.dashboard.service` must never import
``openjarvis.wiz`` (a bug in an optional convenience feature must never be
able to stop the thing that notices the site is down — see
``test_dependency_direction.py``), so this module builds the payload and the
caller that *can* see both sides (``jarvis wiz dashboard``, in
``wiz_cmd.py``) hands it in as a plain callable.

Three sections, matching what an operator glancing at the screen actually
wants: Wiz's own health (the same report ``jarvis wiz doctor`` prints),
honest autonomy metrics (sample size included, see
:mod:`openjarvis.wiz.features.metrics`), and the short list of features
genuinely waiting on a person or still in progress. Nothing here reads an
incident or the site's own uptime — that boundary belongs to
:mod:`openjarvis.wiz.health` and this module does not re-derive it.
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["build_engineering_snapshot"]


def build_engineering_snapshot(runtime: Any) -> Dict[str, Any]:
    """Assemble the Control Center's engineering section from a WizRuntime.

    Never raises: every read is defensive, the same discipline
    :mod:`~openjarvis.wiz.briefing` and :mod:`~openjarvis.wiz.health` hold
    to, because this feeds a page that must keep rendering the Wize half
    even when the Wiz half cannot answer.
    """
    if runtime is None:
        return {"available": False, "detail": "no engineering target configured"}

    from openjarvis.wiz.authority import Actor, Channel
    from openjarvis.wiz.brain import Request
    from openjarvis.wiz.features.metrics import summarize_features

    payload: Dict[str, Any] = {"available": True}

    try:
        payload["health"] = runtime.describe_health(
            Request(
                text="",
                actor=Actor(
                    actor_id="control_center",
                    channel=Channel.CONTROL_CENTER,
                    authenticated=True,
                ),
            )
        )
    except Exception:  # noqa: BLE001
        payload["health"] = {"overall": "UNKNOWN"}

    pipeline = getattr(getattr(runtime, "product", None), "pipeline", None)
    if pipeline is None:
        payload["metrics"] = {"sample_size": 0}
        payload["needs_you"] = []
        payload["engineering"] = []
        return payload

    store = pipeline.store
    payload["metrics"] = summarize_features(store).to_dict()
    payload["needs_you"] = _needs_you(store)
    payload["engineering"] = _in_progress(store)
    return payload


def _needs_you(store: Any) -> List[Dict[str, Any]]:
    from openjarvis.wiz.features.model import FeatureState

    blocked = store.list(states=[FeatureState.HUMAN_REQUIRED], limit=10)
    return [
        {
            "id": f.id,
            "title": f.title,
            "risk": f.risk,
            "reason": str((f.history[-1] or {}).get("reason", "")) if f.history else "",
        }
        for f in blocked
    ]


def _in_progress(store: Any) -> List[Dict[str, Any]]:
    active = [f for f in store.active(limit=10) if not f.terminal]
    return [
        {
            "id": f.id,
            "title": f.title,
            "state": f.state.value,
            "risk": f.risk,
            "branch": f.branch,
            "attempts": f.attempts_used,
            "pr_url": f.pr_url,
        }
        for f in active
    ]
