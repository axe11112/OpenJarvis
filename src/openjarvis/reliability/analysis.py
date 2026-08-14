"""Diagnostic-only Claude Code prompt — analysis, never modification.

Phase 10 validates that JARVIS can hand a coding agent *good context* before it
is allowed to hand it *write access*. This builds a prompt that asks for a
diagnosis and explicitly forbids editing, deploying or touching data, and it
asks the agent to state whether it believes its own proposed fix is safe to
apply automatically — which is the input the owner needs before enabling the
repair loop.

Reuses the sanitisation and injection fencing from
:mod:`openjarvis.reliability.briefing` rather than re-implementing it, so an
analysis prompt is exactly as safe as a repair prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from openjarvis.reliability.briefing import (
    STANDING_INSTRUCTION,
    Briefing,
    build_briefing,
)
from openjarvis.reliability.types import Incident

logger = logging.getLogger(__name__)

__all__ = ["ANALYSIS_TASK", "build_analysis_prompt"]

ANALYSIS_TASK = """\
## Constraints for this task

This is a DIAGNOSTIC request. You have read access to the repository.

- Do NOT modify any file.
- Do NOT create a branch, commit, or pull request.
- Do NOT deploy anything.
- Do NOT run migrations or touch production data.
- Do NOT run commands with side effects outside the checkout.

Reading, searching and reasoning about the code is exactly what is wanted.

## What to return

Answer these, in order, in markdown:

1. **Root cause** — what is actually broken, and why it produces the observed
   behaviour. If the evidence is insufficient to say, state that plainly and
   say what additional evidence would settle it. A confident wrong answer is
   worse than "I cannot tell from this".
2. **Relevant files** — the specific files and, where you can, the specific
   functions involved.
3. **Proposed fix** — what you would change, described precisely enough that
   someone could implement it without you.
4. **Tests required** — what must be true before anyone believes the fix works,
   including the reproduction above.
5. **Risks** — what the fix could break, and what it would not address.
6. **Safe to automate?** — whether you would trust this fix to be applied,
   verified and opened as a pull request without a human reading it first.
   Answer `yes` or `no` and give one sentence of reasoning. Answer `no` if the
   change touches authentication, authorisation, row-level security, payment,
   data migration, or CI configuration.
"""


@dataclass(slots=True)
class AnalysisPrompt:
    """A diagnostic-only prompt derived from an incident."""

    incident_id: str
    text: str
    injection_findings: List[str]

    @property
    def hash(self) -> str:
        """Stable hash of the prompt text, for the audit log."""
        import hashlib

        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


def build_analysis_prompt(
    incident: Incident,
    *,
    protected_paths: Optional[List[str]] = None,
    extra_context: str = "",
) -> AnalysisPrompt:
    """Render *incident* as a read-only analysis request.

    Raises
    ------
    BriefingRefusedError
        When a secret survives redaction — identical behaviour to the repair
        briefing, because the sanitisation is the same code.
    """
    briefing: Briefing = build_briefing(
        incident,
        attempt=1,
        max_attempts=1,
        protected_paths=protected_paths,
    )

    # Swap the repair instructions for analysis-only ones. The evidence,
    # redaction and fencing are reused untouched.
    text = briefing.text
    marker = "## Constraints"
    if marker in text:
        text = text.split(marker)[0]
    else:  # pragma: no cover - build_briefing always emits it
        logger.warning("briefing had no constraints section to replace")

    if extra_context:
        text += f"\n## Additional context\n\n{extra_context}\n"

    text += f"\n{STANDING_INSTRUCTION}\n{ANALYSIS_TASK}"

    return AnalysisPrompt(
        incident_id=incident.id,
        text=text,
        injection_findings=list(briefing.injection_findings),
    )
