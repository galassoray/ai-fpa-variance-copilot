"""
narrate.py
==========
The narrative stage and the publication gate.

THE STRONGEST FORM OF "THE AGENT CANNOT SKIP THE AUDIT"
-------------------------------------------------------
The obvious design exposes ``audit_narrative`` as a tool and checks that the
agent called it. That is weaker than it looks: it makes the guardrail depend on
the agent's cooperation, and turns "did it audit?" into a runtime question.

Narration and auditing are **orchestrator stages, not tools**. They are not in
the registry, so no plan can include them, omit them, or reorder them. The agent
plans *retrieval*; the orchestrator narrates, audits, and gates. The agent cannot
skip what it has no ability to invoke.

That is also why the tool surface has no ``publish`` verb. Publication is a human
action taken outside the agent (Gate 1, Phase 5). What this module produces is a
*candidate* carrying a verdict.

THE GATE
--------
``PublicationCandidate.publishable`` is False unless an audit ran here and
passed. Not "unless an audit failed" -- unless one *ran*. A missing audit and a
failed audit are the same answer, because the alternative is that forgetting to
check counts as a pass. ``assert_publishable()`` raises rather than returning a
flag, so a caller cannot ignore it by not looking.

WHAT IS AND IS NOT GUARANTEED
-----------------------------
Guaranteed: every dollar, percent, and count in accepted prose matches a value
the agent's tools returned, within the empirically derived tolerance
(MAX_REL_TOL = 0.005). Fabricated figures cannot survive.

Not guaranteed: that the *claims* are right. "Opex rose because the team hired
ahead of plan" can be causally invented while every figure in it is correct. The
audit verifies numbers, not reasoning. That residual is bounded by the human
gate, and it is stated here rather than papered over, because a design that
claimed to have automated judgment would be overclaiming.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.facts import fact_pack_from_ledger
from guardrails import entity_audit as ea
from guardrails import numeric_audit as na
from narrative.generate import generate_commentary

#: Narrative outcomes. Distinct from the run outcomes in ledger.py: a run can
#: COMPLETE and still refuse to narrate.
ACCEPTED = "accepted"     # model prose, audit clean
FALLBACK = "fallback"     # deterministic injection narrative after model failure
REFUSED = "refused"       # nothing narratable, or nothing survived the audit
NOT_RUN = "not_run"       # narration was never attempted


@dataclass
class PublicationCandidate:
    """A narrative plus the evidence for or against publishing it."""

    status: str = NOT_RUN
    text: str = ""
    source: str = ""                     # "model" | "injection" | "refusal"
    attempts: int = 0
    deterministic_by_choice: bool = False
    audit_ran: bool = False
    audit_passed: bool = False
    violations: list = field(default_factory=list)
    entity_flags: list = field(default_factory=list)
    matched: list = field(default_factory=list)      # (mention, value, provenance)
    fact_pack: object = None
    reason: str = ""

    @property
    def publishable(self) -> bool:
        """False unless an audit RAN here and passed.

        A missing audit and a failed audit are deliberately the same answer.
        Any other choice makes forgetting to check equivalent to passing.
        """
        return (self.status in (ACCEPTED, FALLBACK)
                and self.audit_ran and self.audit_passed and bool(self.text))

    def assert_publishable(self) -> None:
        """Raise rather than return a flag, so it cannot be ignored by omission."""
        if not self.publishable:
            raise NotPublishable(
                f"narrative is not publishable (status={self.status}, "
                f"audit_ran={self.audit_ran}, audit_passed={self.audit_passed}, "
                f"violations={len(self.violations)})"
            )

    def audit_trace(self, limit: int = 40) -> list:
        """Every figure in the prose, tied to the computed value behind it.

        The copilot's signature UI element, carried into the agent: a reader can
        point at any number in the commentary and see which step produced it.
        """
        return [{"mention": m, "value": v, "provenance": lbl}
                for m, v, lbl in self.matched[:limit]]


class NotPublishable(RuntimeError):
    pass


def narrate(result, goal: dict, client, all_entity_names: list,
            mode: str = "audit", max_retries: int = 1) -> PublicationCandidate:
    """Build the fact pack from the ledger, draft, audit, and gate.

    The audit here is the orchestrator's own, run on the returned text
    regardless of what happened inside ``generate_commentary``. That is
    redundant by design: the guarantee must not depend on a downstream module
    having done its job, and the independent run is what produces the audit
    trace tied to this run's ledger.
    """
    candidate = PublicationCandidate()

    pack = fact_pack_from_ledger(result, goal)
    candidate.fact_pack = pack

    if pack.status != "ok":
        candidate.status = REFUSED
        candidate.source = "refusal"
        candidate.reason = pack.reason
        candidate.audit_ran = True      # there was nothing to audit, and we checked
        candidate.audit_passed = False
        return candidate

    nr = generate_commentary(pack, client, all_entity_names,
                             mode=mode, max_retries=max_retries)

    # The copilot's deterministic narrative reads a fixed schema and produces
    # nothing for a run that filled none of those slots -- so a comparative or
    # trend question executed perfectly and then fell back to empty prose.
    # The briefing has already assembled the same material by reference, so it
    # can supply the fallback, and the audit below verifies it identically.
    if not (nr.text or "").strip():
        from agent.briefing import build_briefing, narrative_from_briefing

        substitute = narrative_from_briefing(build_briefing(result, goal))
        if substitute:
            nr.text = substitute
            nr.source = "injection"
            nr.status = "accepted"
    # generate_commentary reports source="injection" both when injection was
    # CHOSEN and when it was reached because the model's drafts were rejected.
    # Collapsing those would let a demo caption read "model output was rejected"
    # on a run where no model was ever called -- a caption that misrepresents
    # what happened is its own kind of fabrication.
    candidate.deterministic_by_choice = (mode == "inject")
    candidate.status = nr.status
    candidate.text = nr.text
    candidate.source = nr.source
    candidate.attempts = nr.attempts
    candidate.entity_flags = list(nr.entity_flags)

    if nr.status == "refused":
        candidate.audit_ran = True
        candidate.audit_passed = False
        candidate.reason = "no draft survived the audit; refused rather than publish"
        return candidate

    # Independent re-audit against this run's ledger-derived pack.
    num = na.audit(nr.text, pack)
    ent = ea.audit_entities(nr.text, pack, all_entity_names)
    candidate.audit_ran = True
    candidate.audit_passed = num.passed and ent.passed
    candidate.violations = [
        {"mention": v.mention, "value": v.value, "kind": v.kind, "reason": v.reason}
        for v in num.violations
    ]
    candidate.matched = list(num.matched)
    if not ent.passed:
        candidate.entity_flags = sorted(set(candidate.entity_flags) | set(ent.out_of_scope))

    if not candidate.audit_passed:
        # generate_commentary returned text its own audit accepted, but ours
        # rejects. That means the two are looking at different fact packs, which
        # is the drift this module exists to prevent -- so refuse loudly.
        candidate.status = REFUSED
        candidate.source = "refusal"
        candidate.reason = (
            "independent audit rejected text that passed upstream; the narrative "
            "and the ledger disagree"
        )
        candidate.text = ""
    return candidate


def render_narrative(candidate: PublicationCandidate, show_trace: bool = True) -> str:
    """Display block for a narrative candidate, including its verdict."""
    out = ["", "=" * 74, " COMMENTARY", "=" * 74]

    if not candidate.publishable:
        out.append(f"\n  NOT PUBLISHABLE - {candidate.status}")
        if candidate.reason:
            out.append(f"  {candidate.reason}")
        for v in candidate.violations[:10]:
            out.append(f"    rejected figure {v['mention']} - {v['reason']}")
        for e in candidate.entity_flags[:10]:
            out.append(f"    out-of-scope entity: {e}")
        out.append("\n  No commentary was produced rather than publish an "
                   "unsupported statement.")
        return "\n".join(out)

    out.append("")
    for para in candidate.text.strip().split("\n"):
        out.append(f"  {para}" if para.strip() else "")

    if candidate.source == "model":
        src = "model-written, audit-verified"
    elif candidate.deterministic_by_choice:
        src = "deterministic narrative (no model requested)"
    else:
        src = "deterministic fallback (the model's drafts failed the audit)"
    attempts = (f"{candidate.attempts} model attempt(s); "
                if candidate.attempts else "")
    out.append(f"\n  [{src}; {attempts}"
               f"{len(candidate.matched)} figure(s) verified against the ledger]")

    if show_trace and candidate.matched:
        out.append("\n  AUDIT TRACE - every figure tied to its computed source")
        for t in candidate.audit_trace(limit=12):
            out.append(f"    {t['mention']:>16}  <-  {t['provenance']}")
        if len(candidate.matched) > 12:
            out.append(f"    ... {len(candidate.matched) - 12} more")
    return "\n".join(out)
