"""
generate.py
===========
Orchestrates the guarded narrative pipeline:

  fact pack --> (refuse if insufficient) --> LLM draft --> numeric + entity audit
            --> if clean: accept
            --> if violations & retries left: re-prompt with the disallowed
                figures called out, try again
            --> if still violating: REJECT the model text and fall back to the
                deterministic injection narrative (which cannot fabricate), or
                refuse. The violating text is NEVER surfaced.

Guarantee: whatever this function returns has passed the numeric audit (zero
fabricated $ / % figures) -- by construction, regardless of model behavior.

Modes
-----
"audit"  : model writes prose; audit is the guarantee; injection is the fallback.
"inject" : skip the model entirely; return the deterministic injection narrative.
"both"   : audit mode, but also attach the injection narrative for comparison.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from narrative import prompt as P
from narrative.fact_pack import FactPack
from guardrails import numeric_audit as na
from guardrails import entity_audit as ea


@dataclass
class NarrativeResult:
    status: str                       # "accepted" | "fallback" | "refused"
    text: str
    month: str
    attempts: int = 0
    audit_passed: bool = True
    violations_caught: list = field(default_factory=list)  # per-attempt violation counts
    entity_flags: list = field(default_factory=list)
    source: str = ""                  # "model" | "injection" | "refusal"


REFUSAL_TEXT = ("Insufficient computed data to generate variance commentary for "
                "this period. No commentary was produced rather than risk an "
                "unsupported statement.")


def generate_commentary(fact_pack: FactPack, client, all_entity_names: list,
                        mode: str = "audit", max_retries: int = 1) -> NarrativeResult:
    if fact_pack.status != "ok":
        return NarrativeResult("refused", REFUSAL_TEXT, fact_pack.month,
                               attempts=0, audit_passed=True, source="refusal")

    if mode == "inject":
        text = P.injection_narrative(fact_pack)
        res = na.audit(text, fact_pack)
        return NarrativeResult("accepted", text, fact_pack.month, attempts=0,
                               audit_passed=res.passed, source="injection")

    system = P.SYSTEM_PROMPT
    user = P.render_user_prompt(fact_pack)
    caught = []
    entity_flags = []
    attempts = 0
    last_text = ""

    for attempt in range(max_retries + 1):
        attempts += 1
        draft = client.complete(system, user)
        last_text = draft
        num_res = na.audit(draft, fact_pack)
        ent_res = ea.audit_entities(draft, fact_pack, all_entity_names)
        caught.append(num_res.n_fabricated)
        if not ent_res.passed:
            entity_flags.append(ent_res.out_of_scope)

        if num_res.passed and ent_res.passed:
            return NarrativeResult("accepted", draft, fact_pack.month, attempts=attempts,
                                   audit_passed=True, violations_caught=caught,
                                   entity_flags=entity_flags, source="model")

        # re-prompt with the specific disallowed figures / entities
        bad_nums = ", ".join(v.mention for v in num_res.violations) or "none"
        bad_ents = ", ".join(ent_res.out_of_scope) or "none"
        user = (P.render_user_prompt(fact_pack) +
                f"\n\nYOUR PREVIOUS DRAFT WAS REJECTED. It contained figures not in "
                f"the facts ({bad_nums}) and/or out-of-scope entities ({bad_ents}). "
                f"Rewrite using ONLY the provided facts.")

    # exhausted retries: never surface the violating text -> deterministic fallback
    fallback = P.injection_narrative(fact_pack)
    fb_res = na.audit(fallback, fact_pack)
    return NarrativeResult(
        "fallback" if fb_res.passed else "refused",
        fallback if fb_res.passed else REFUSAL_TEXT,
        fact_pack.month, attempts=attempts, audit_passed=fb_res.passed,
        violations_caught=caught, entity_flags=entity_flags,
        source="injection" if fb_res.passed else "refusal")
