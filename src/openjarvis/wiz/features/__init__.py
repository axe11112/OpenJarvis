"""Feature requests — the product-development half of Wiz.

An incident and a feature request are both "work Wiz does to a repository", and
that is where the similarity ends. An incident is *discovered*, is urgent by
construction, and succeeds when the site stops being broken. A feature request
is *asked for*, has no inherent urgency, and succeeds when it does what the
operator meant. Their lifecycles differ at almost every step — a feature has a
planning phase and an acceptance contract, an incident has a reproduction and a
severity — so they get separate state machines rather than one machine with
enough optional fields to serve both.
"""

from __future__ import annotations

__all__ = ["__doc__"]
