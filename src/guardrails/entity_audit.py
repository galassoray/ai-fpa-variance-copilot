"""
entity_audit.py
===============
No-new-entity guardrail. The model may only name departments and line items that
are in the fact pack. This catches the failure mode where a model imports a real
account or department that exists in the company but was NOT part of the facts it
was given (e.g. attributing a miss to "Recruiting" when Recruiting wasn't in the
pack), which is a subtler fabrication than a wrong number.

We check against the *canonical* list of all department and account names. A
canonical entity that appears in the narrative but is not in the pack's allowed
list is flagged. Fully invented names are not enumerable, so this check is scoped
to known-but-out-of-scope entities -- the meaningful, catchable case.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class EntityAuditResult:
    passed: bool
    out_of_scope: list = field(default_factory=list)


def audit_entities(text: str, fact_pack, all_entity_names: list) -> EntityAuditResult:
    allowed = {e.lower() for e in fact_pack.allowed_entities}
    low = text.lower()
    flagged = []
    for name in sorted(set(all_entity_names), key=len, reverse=True):
        nl = name.lower()
        if nl in allowed:
            continue
        # word-boundary-ish containment for multi-word names
        pat = r"(?<![a-z0-9])" + re.escape(nl) + r"(?![a-z0-9])"
        if re.search(pat, low):
            flagged.append(name)
    return EntityAuditResult(passed=len(flagged) == 0, out_of_scope=flagged)


def canonical_entity_names(tables: dict) -> list:
    """All department + account names, for the out-of-scope check."""
    depts = tables["dim_department"]["department_name"].tolist()
    accts = tables["dim_account"]["account_name"].tolist()
    # de-duplicate generic names like "Salaries"/"Travel & Entertainment" that
    # legitimately recur across departments -- those are ambiguous, so exclude
    # names that appear more than once from the strict check.
    from collections import Counter
    counts = Counter(accts)
    unique_accts = [a for a in accts if counts[a] == 1]
    return sorted(set(depts + unique_accts))
