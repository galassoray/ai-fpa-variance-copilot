"""
prompt.py
=========
System prompt + user-prompt rendering for the narrative layer, plus a
deterministic INJECTION-mode narrative that is built entirely from the fact pack
(no model numbers at all) and is used both as a strict no-LLM mode and as the
safe fallback when a model's output fails the audit.
"""
from __future__ import annotations
import json

SYSTEM_PROMPT = """You are an FP&A analyst writing the monthly variance commentary for finance leadership.

STRICT RULES:
- You are given a set of ALREADY-COMPUTED facts. Use ONLY those facts.
- Do NOT perform any arithmetic. Do NOT compute, sum, average, or derive any new number.
- Every dollar figure or percentage you write MUST appear in the facts you were given. If it is not in the facts, do not write it.
- Only name departments and line items that appear in the facts.
- Do not speculate about causes as if they were established. You may suggest a likely business explanation only if you clearly mark it as a hypothesis to verify.
- If the facts are insufficient to comment, say so plainly rather than guessing.
- Write concise, executive-ready prose: a short headline read, then the top drivers, then one line on retention/ARR. No tables.

FORMATTING (so the commentary is clean plain text):
- Write percentages with a percent sign, e.g. 92.3%. Never write a retention or margin as a bare decimal like 0.923.
- Write dollar amounts plainly with AT LEAST 3 significant figures, e.g. $2.60M, $247.1K, $59,926. Never round to 1-2 significant figures (not "$2M", not "$1.7M") -- a figure that coarse cannot be verified and will be rejected.
- Do NOT wrap numbers in math notation, dollar-delimited math, LaTeX, asterisks, or backticks.
- Use plain sentences. No markdown, no headers, no bullet characters.
"""


def render_user_prompt(fact_pack) -> str:
    facts = json.dumps(fact_pack.prompt_facts, indent=2, default=str)
    return (
        f"Here are the computed facts for {fact_pack.month} ({fact_pack.scope}). "
        f"Write the variance commentary using ONLY these facts.\n\n"
        f"FACTS:\n{facts}\n"
    )


# ---------------------------------------------------------------------------
# Deterministic injection-mode narrative: numbers come straight from the pack.
# ---------------------------------------------------------------------------
def _m(x):
    """Format money at >= 3 significant figures.

    Coarse figures ("$43K", "$2M") cannot be meaningfully audited: their rounding
    window is wide enough that a fabricated number would match a computed value
    most of the time, so the audit rejects them (see guardrails/numeric_audit.py,
    MAX_REL_TOL). Writing at 3+ significant figures keeps commentary readable and
    checkable at once.
    """
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e6:
        return f"${x/1e6:.2f}M"      # $2.60M
    if ax >= 1e4:
        return f"${x/1e3:.1f}K"      # $247.1K
    return f"${x:,.0f}"              # $9,812


def _p(x):
    return "n/a" if x is None else f"{x*100:.1f}%"


def _period_label(month: str) -> str:
    try:
        import datetime as _dt
        return _dt.date.fromisoformat(month).strftime("%B %Y")
    except Exception:
        return month


def injection_narrative(fact_pack) -> str:
    """Build commentary purely from computed values (guaranteed audit-clean)."""
    f = fact_pack.prompt_facts
    period = _period_label(fact_pack.month)
    parts = []

    rvb = f.get("revenue_vs_budget")
    comp = f.get("company", {})
    if rvb:
        dirn = "ahead of" if rvb["favorable"] else "below"
        line = (f"For {period}, revenue of {_m(rvb['actual'])} came in {dirn} "
                f"plan by {_m(abs(rvb['variance']))} against a {_m(rvb['budget'])} budget.")
        if comp.get("gross_margin") is not None:
            line += f" Gross margin was {_p(comp['gross_margin'])}."
        parts.append(line)

    drivers = f.get("top_drivers", [])
    if drivers:
        chunks = []
        for d in drivers[:5]:
            fav = "favorable" if d["favorable"] else "unfavorable"
            chunks.append(f"{d['line_item']} ({d['department']}) {_m(abs(d['variance']))} "
                          f"{fav}")
        parts.append("The largest drivers versus budget were: " + "; ".join(chunks) + ".")

    cd = f.get("comp_decomposition", [])
    if cd:
        c = cd[0]
        parts.append(
            f"In {c['department']}, the {_m(abs(c['salary_variance']))} salary variance splits into "
            f"{_m(abs(c['headcount_effect']))} from headcount versus plan and "
            f"{_m(abs(c['rate_effect']))} from rate.")

    arr = f.get("arr")
    if arr:
        parts.append(
            f"Ending ARR was {_m(arr['ending_arr'])}, with {_m(arr['new_arr'])} new and "
            f"{_m(arr['churned_arr'])} churned; trailing-twelve-month NRR was "
            f"{_p(arr['nrr_ttm'])} and GRR {_p(arr['grr_ttm'])}.")

    return " ".join(parts)
