"""
chat.py
=======
Guarded analyst Q&A over the computed dataset.

Same contract as the commentary layer, with one deliberate difference:

  commentary : if the model misbehaves twice -> fall back to the deterministic
               injection narrative (there is always a correct thing to say).
  chat       : if the model misbehaves twice -> REFUSE. There is no deterministic
               answer to an arbitrary question, and a wrong number is worse than
               no answer. Declining is the correct behaviour, not a limitation.

Flow:
  question -> deterministic slice of the fact index (code chooses, not the model)
           -> model answers from that slice only
           -> numeric + entity audit on the answer
           -> clean: accept | dirty: re-prompt once | still dirty: refuse

The model never computes. If the answer requires a number that was not computed,
the correct output is a refusal.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json

from narrative.fact_pack import FactPack
from narrative import fact_index as FI
from guardrails import numeric_audit as na
from guardrails import entity_audit as ea

CHAT_SYSTEM_PROMPT = """You are an FP&A analyst's assistant answering questions about a company's financial results.

You are given FACTS that have already been computed. They are the only numbers that exist to you.

STRICT RULES:
- Use ONLY the numbers in the FACTS. Never state a figure that is not there.
- Do NOT perform arithmetic. Do not add, subtract, average, annualize, or derive any new number. If the answer needs a number that is not in the FACTS, say plainly that it was not computed and say what you would need.
- Only name departments and line items that appear in the FACTS.
- You may explain, compare, and interpret the given numbers, and suggest likely business explanations -- but mark any explanation you cannot support from the FACTS as a hypothesis to verify.
- Be concise and direct, the way an analyst answers a colleague. No preamble.

FORMATTING:
- Percentages get a percent sign (92.3%), never a bare decimal (0.923).
- Dollar amounts written plainly with AT LEAST 3 significant figures ($2.60M, $247.1K, $59,926). Never 1-2 significant figures (not "$2M", not "$1.7M") -- that is too coarse to verify and will be rejected.
- No math notation, no LaTeX, no backticks, no markdown.
"""

REFUSAL = ("I can't answer that from the computed figures without estimating, so "
           "I'm not going to guess. The numbers needed weren't in the computed set "
           "for this question.")


@dataclass
class ChatAnswer:
    status: str                    # accepted | refused
    text: str
    scope_note: str = ""
    facts_sent: int = 0
    attempts: int = 0
    violations_caught: list = field(default_factory=list)
    entity_flags: list = field(default_factory=list)
    matched: list = field(default_factory=list)


def build_chat_pack(question: str, index: list, focus_month: str,
                    entity_names: list, max_rows: int = 320) -> tuple:
    rows, note = FI.select_facts(question, index, focus_month, max_rows=max_rows)
    allowed = FI.rows_to_allowed(rows)
    payload = FI.rows_to_payload(rows)
    entities = sorted({r.account for r in rows if r.account} |
                      {r.department for r in rows if r.department})
    # map department ids to names as well, so the model may use either
    pack = FactPack(month=focus_month, scope="chat", status="ok" if rows else "insufficient_data",
                    reason="" if rows else "no facts matched the question",
                    prompt_facts=payload, allowed_values=allowed,
                    allowed_entities=list(entities) + entity_names)
    return pack, rows, note


def render_chat_prompt(question: str, pack: FactPack, history: list) -> str:
    facts = json.dumps(pack.prompt_facts, indent=1, default=str)
    convo = ""
    if history:
        turns = []
        for h in history[-6:]:
            turns.append(f"{h['role'].upper()}: {h['content']}")
        convo = "CONVERSATION SO FAR:\n" + "\n".join(turns) + "\n\n"
    return (f"{convo}FACTS (the only numbers that exist):\n{facts}\n\n"
            f"QUESTION: {question}\n")


def answer_question(question: str, index: list, focus_month: str, client,
                    all_entity_names: list, history: list | None = None,
                    max_retries: int = 1, max_rows: int = 320) -> ChatAnswer:
    history = history or []
    pack, rows, note = build_chat_pack(question, index, focus_month,
                                       all_entity_names, max_rows=max_rows)
    if pack.status != "ok":
        return ChatAnswer("refused", REFUSAL, note, 0, 0)

    user = render_chat_prompt(question, pack, history)
    caught, ent_flags = [], []
    attempts = 0

    for _ in range(max_retries + 1):
        attempts += 1
        draft = client.complete(CHAT_SYSTEM_PROMPT, user)
        num_res = na.audit(draft, pack)
        ent_res = ea.audit_entities(draft, pack, all_entity_names)
        caught.append(num_res.n_fabricated)
        if not ent_res.passed:
            ent_flags.append(ent_res.out_of_scope)

        if num_res.passed and ent_res.passed:
            return ChatAnswer("accepted", draft, note, len(rows), attempts,
                              caught, ent_flags, num_res.matched)

        bad = ", ".join(v.mention for v in num_res.violations) or "none"
        bad_e = ", ".join(ent_res.out_of_scope) or "none"
        user = (render_chat_prompt(question, pack, history) +
                f"\nYOUR PREVIOUS ANSWER WAS REJECTED: it contained figures not in "
                f"the FACTS ({bad}) and/or out-of-scope items ({bad_e}). "
                f"Answer again using ONLY the FACTS, or say the number was not computed.")

    # never surface an answer that failed the audit
    return ChatAnswer("refused", REFUSAL, note, len(rows), attempts, caught, ent_flags)
