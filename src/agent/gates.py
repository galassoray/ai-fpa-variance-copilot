"""
gates.py
========
The two human checkpoints, and the rule that makes an approval mean something.

GATE 0 -- PLAN REVIEW (optional, off by default)
-------------------------------------------------
A human sees the validated plan before any query runs and may reject it.

Being honest about what this is worth: on a read-only tool surface it adds
almost no safety. Every action the agent can take is already side-effect-free,
so approving the plan prevents nothing that execution would have caused. Its
real justifications are two:

  * **Inspectability.** A reviewer sees intent before results, which is a
    different and more useful thing than reading a ledger afterwards.
  * **The seam exists.** The same orchestrator pointed at a write-capable
    registry would *require* this gate. Building it now, against a surface
    where it is cheap, means the control point is already there rather than
    being retrofitted under pressure later.

Claiming a read-only plan gate is a security control would be overclaiming, and
the decision log says so.

GATE 1 -- PRE-PUBLICATION APPROVAL (mandatory)
-----------------------------------------------
Nothing is published without a human decision. This is the gate that bounds the
residual Phase 4 could not close: the audit verifies that every figure is real,
but not that the *claims* about those figures are sound. A causal assertion --
"opex rose because the team hired ahead of plan" -- can be invented with every
number correct. No automated check catches that. A reviewer does.

APPROVAL IS BOUND TO WHAT WAS REVIEWED
--------------------------------------
An approval carries the hash of the exact artifact the approver saw: the
package's section hashes plus the narrative text. If either changes afterwards,
the approval no longer applies and ``published`` returns False.

Without that binding, "approved" degrades into a flag someone set once, and the
obvious failure -- approve draft A, publish draft B -- is invisible in the
record. With it, an approval is a statement about a specific artifact rather
than about a session.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# ---- decisions -----------------------------------------------------------
APPROVED = "APPROVED"
REJECTED = "REJECTED"
REVISION_REQUESTED = "REVISION_REQUESTED"
PENDING = "PENDING"

DECISIONS = (APPROVED, REJECTED, REVISION_REQUESTED)


class NotApproved(RuntimeError):
    """Publication attempted without a valid, current approval."""


def artifact_hash(result, candidate) -> str:
    """Fingerprint of exactly what a reviewer would see.

    Section result hashes (not the rows, which are already hashed) plus the
    narrative text. Deliberately excludes run_id, timestamps, and latency: those
    change on every run without changing what was reviewed, and an approval that
    expired because a run was faster would be noise.
    """
    payload = {
        "period": result.ledger.goal.get("period"),
        "comparison": result.ledger.goal.get("comparison"),
        "outcome": result.outcome,
        "sections": {name: sec["result_hash"]
                     for name, sec in sorted(result.sections.items())},
        "missing_sections": sorted(result.missing_sections),
        "narrative": (candidate.text or "") if candidate else "",
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---- Gate 0 --------------------------------------------------------------
@dataclass
class PlanReview:
    """A human decision on a plan, taken before any query runs."""

    decision: str = PENDING
    reviewer: str = ""
    note: str = ""
    timestamp: str = ""
    plan_summary: list = field(default_factory=list)

    @property
    def cleared(self) -> bool:
        return self.decision == APPROVED


def summarize_plan(plan) -> list:
    """Compact, reviewable rendering of a plan: tool, arguments, purpose."""
    out = []
    for s in plan.steps:
        out.append({
            "step": s.idx,
            "tool": s.tool,
            "params": dict(s.params),
            "purpose": s.purpose,
            "optional": s.optional,
            # Surfaced explicitly: a reviewer should see which arguments are
            # bound at execution time rather than fixed in the plan.
            "resolved_at_runtime": sorted(
                k for k, v in s.params.items()
                if isinstance(v, str) and v.startswith("$")
            ),
        })
    return out


def review_plan(plan, decision: str = APPROVED, reviewer: str = "",
                note: str = "") -> PlanReview:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    return PlanReview(decision=decision, reviewer=reviewer, note=note,
                      timestamp=datetime.now(timezone.utc).isoformat(),
                      plan_summary=summarize_plan(plan))


class PlanRejected(RuntimeError):
    """Gate 0 declined the plan. No query was run."""


# ---- Gate 1 --------------------------------------------------------------
@dataclass
class Approval:
    decision: str = PENDING
    approver: str = ""
    note: str = ""
    timestamp: str = ""
    #: The artifact this decision refers to. An approval that does not match
    #: the current artifact does not authorize publishing it.
    artifact_hash: str = ""

    @property
    def is_approval(self) -> bool:
        return self.decision == APPROVED


@dataclass
class PublicationPacket:
    """A run, its narrative, and the human decision about publishing it."""

    result: object
    candidate: object
    approval: Approval = field(default_factory=Approval)
    plan_review: PlanReview = field(default_factory=PlanReview)

    @property
    def artifact_hash(self) -> str:
        return artifact_hash(self.result, self.candidate)

    @property
    def audit_ok(self) -> bool:
        """The machine's verdict: every figure traced to a computed value."""
        return bool(self.candidate) and self.candidate.publishable

    @property
    def approval_is_current(self) -> bool:
        """False if the artifact changed after it was approved."""
        return (self.approval.is_approval
                and self.approval.artifact_hash == self.artifact_hash)

    @property
    def published(self) -> bool:
        """Both verdicts required: the audit's and a human's.

        The audit cannot judge whether a claim is sound; a human cannot verify
        180 figures by eye. Neither is sufficient, so neither is optional.
        """
        return self.audit_ok and self.approval_is_current

    def approve(self, approver: str, note: str = "") -> Approval:
        """Record approval of the CURRENT artifact.

        Refuses when the audit did not pass. A human cannot override a failed
        audit -- that would make the guarantee advisory, and the whole point is
        that fabricated figures cannot be published by anyone.
        """
        if not self.audit_ok:
            raise NotApproved(
                "the audit did not pass, so this cannot be approved for "
                "publication. Fix the narrative or refuse; approval is not an "
                "override."
            )
        self.approval = Approval(
            decision=APPROVED, approver=approver, note=note,
            timestamp=datetime.now(timezone.utc).isoformat(),
            artifact_hash=self.artifact_hash,
        )
        return self.approval

    def reject(self, approver: str, note: str = "",
               revision: bool = False) -> Approval:
        self.approval = Approval(
            decision=REVISION_REQUESTED if revision else REJECTED,
            approver=approver, note=note,
            timestamp=datetime.now(timezone.utc).isoformat(),
            artifact_hash=self.artifact_hash,
        )
        return self.approval

    def assert_published(self) -> None:
        """Raise unless both gates are satisfied for THIS artifact."""
        if not self.audit_ok:
            raise NotApproved("audit did not pass")
        if not self.approval.is_approval:
            raise NotApproved(f"no approval on record (decision="
                              f"{self.approval.decision})")
        if self.approval.artifact_hash != self.artifact_hash:
            raise NotApproved(
                f"the approval refers to a different artifact "
                f"({self.approval.artifact_hash} != {self.artifact_hash}); "
                "the package or narrative changed after it was approved"
            )

    def to_dict(self) -> dict:
        return {
            "run_id": self.result.ledger.run_id,
            "artifact_hash": self.artifact_hash,
            "audit_ok": self.audit_ok,
            "published": self.published,
            "plan_review": asdict(self.plan_review),
            "approval": asdict(self.approval),
        }


def blocking_reasons(packet: PublicationPacket) -> list:
    """Everything standing between this packet and publication.

    Surfaced at the top of the review UI rather than buried, so a reviewer sees
    what is wrong before reading prose that may be about to be discarded.
    """
    reasons = []
    c = packet.candidate
    if c is None:
        reasons.append("no narrative was produced")
        return reasons
    if not c.audit_ran:
        reasons.append("the audit did not run")
    elif not c.audit_passed:
        reasons.append(f"the audit rejected {len(c.violations)} figure(s)")
    for v in c.violations[:5]:
        reasons.append(f"  {v['mention']}: {v['reason']}")
    for e in c.entity_flags[:5]:
        reasons.append(f"  out-of-scope entity: {e}")
    if packet.result.missing_sections:
        reasons.append("promised sections missing: "
                       + ", ".join(packet.result.missing_sections))
    if packet.audit_ok and not packet.approval.is_approval:
        reasons.append("awaiting human approval")
    if (packet.approval.is_approval
            and packet.approval.artifact_hash != packet.artifact_hash):
        reasons.append("the artifact changed after approval; re-review required")
    return reasons
