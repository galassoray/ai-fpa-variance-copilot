"""
facts.py
========
Assembles the narrative fact pack **from the run ledger** -- from the values the
agent's tools actually returned -- rather than by recomputing from pandas.

WHY NOT JUST CALL build_fact_pack()
------------------------------------
``narrative.fact_pack.build_fact_pack`` takes the pandas ``outputs`` dict and
recomputes top drivers, comp decomposition, and revenue decomposition itself. It
is correct, and the copilot uses it. But wiring it into the agent would create a
**second path to every number**: the ledger would hold one set of figures and the
narrative would be audited against a separately computed set.

Two paths to a number can drift. When they do, the audit passes -- because the
prose matches the fact pack -- while the prose contradicts the package printed
beside it. That is a worse failure than an unaudited narrative, because it comes
with a certificate.

So the fact pack is built from ``ledger.results``. Every allowed value traces to
a specific step, and ``ledger_provenance`` records which. The audit's whitelist
and the package's figures are then the same numbers by construction, not by
agreement.

THE PROPERTY THIS BUYS
----------------------
Only what the agent retrieved is narratable. If a plan never calls
``get_arr_bridge``, no ARR figure enters the whitelist, so the model cannot state
one -- and if it tries, the audit catches it. **The plan determines the
narrative's permitted vocabulary.** A thin plan yields a thin, correct commentary
rather than a rich, partly-invented one.

``_add`` is imported from the copilot's fact_pack rather than reimplemented, so
the sign/magnitude handling (a model writes "$67,061" for a -67,061 variance)
stays in one place. Reimplementing it here would be a second path to the
*whitelist*, which is the same mistake one level down.
"""
from __future__ import annotations

from narrative.fact_pack import AllowedValue, FactPack, _add  # noqa: F401

#: Fields that are never narratable quantities: ids, labels, ordinals, flags.
#: Ranks and row indices are excluded deliberately -- "the number 1 driver" must
#: not license the literal 1 as a verified dollar figure.
_NON_FACT_FIELDS = {
    "month", "metric", "rank", "member", "name", "department_id",
    "department_name", "account_id", "account_name", "account_category",
    "statement_line", "favorable", "grp", "has_actuals", "has_budget",
    "has_forecast", "fiscal_year", "quarter",
}

#: field -> (kind, label suffix). Anything not listed is not whitelisted, so a
#: new tool's numeric fields are unverifiable until deliberately added. That is
#: the safe default: an unlisted field means the model may not state it.
_FIELD_KINDS = {
    # money
    "actual": ("dollar", "actual"), "base": ("dollar", "budget/base"),
    "variance": ("dollar", "variance"), "oi_impact": ("dollar", "OI impact"),
    "revenue": ("dollar", "revenue"), "cogs": ("dollar", "COGS"),
    "opex": ("dollar", "opex"), "gross_profit": ("dollar", "gross profit"),
    "operating_income": ("dollar", "operating income"),
    "ending_arr": ("dollar", "ending ARR"), "starting_arr": ("dollar", "starting ARR"),
    "new_arr": ("dollar", "new ARR"), "expansion_arr": ("dollar", "expansion ARR"),
    "contraction_arr": ("dollar", "contraction ARR"),
    "churned_arr": ("dollar", "churned ARR"), "arpa": ("dollar", "ARPA"),
    "arr_per_head": ("dollar", "ARR per head"),
    "revenue_per_head": ("dollar", "revenue per head"),
    "salary_budget": ("dollar", "salary budget"),
    "salary_actual": ("dollar", "salary actual"),
    "salary_variance": ("dollar", "salary variance"),
    "hc_impact": ("dollar", "headcount effect"),
    "rate_impact": ("dollar", "rate effect"),
    "rev_budget": ("dollar", "revenue budget"), "rev_actual": ("dollar", "revenue actual"),
    "rev_variance": ("dollar", "revenue variance"),
    "volume_impact": ("dollar", "volume effect"),
    "price_impact": ("dollar", "price effect"),
    "value": ("dollar", "trend value"),
    # ratios
    "variance_pct": ("percent", "% variance"),
    "gross_margin": ("percent", "gross margin"),
    "operating_margin": ("percent", "operating margin"),
    "opex_pct_revenue": ("percent", "opex % of revenue"),
    "share_of_total_oi_impact": ("percent", "share of total impact"),
    "share_of_department_oi_impact": ("percent", "share of department impact"),
    "nrr": ("percent", "NRR"), "grr": ("percent", "GRR"),
    "nrr_ttm": ("percent", "NRR (ttm)"), "grr_ttm": ("percent", "GRR (ttm)"),
    # counts
    "total_headcount": ("count", "headcount"),
    "actual_headcount": ("count", "headcount actual"),
    "budget_headcount": ("count", "headcount budget"),
    "forecast_headcount": ("count", "headcount forecast"),
    "hc_var_vs_budget": ("count", "headcount vs plan"),
    "hc_var_vs_forecast": ("count", "headcount vs forecast"),
    "customers_start": ("count", "customers start"),
    "customers_end": ("count", "customers end"),
    "new_customers": ("count", "new customers"),
    "churned_customers": ("count", "churned customers"),
}

#: Tools whose rows carry no narratable figures.
_ORIENTATION_TOOLS = {"list_periods", "list_dimensions"}

#: The prompt and the deterministic injection narrative in narrative/prompt.py
#: read a canonical fact schema: company, revenue_vs_budget, top_drivers,
#: comp_decomposition, revenue_decomposition, arr, headcount. Ledger rows are
#: mapped INTO that schema rather than emitted under tool names, so the agent
#: reuses the flagship's prompt and fallback narrative unchanged instead of
#: growing a parallel copy that would drift from it.
#:
#: Anything the canonical schema has no slot for still reaches the model under
#: "additional_analysis" -- and, more importantly, still enters the audit
#: whitelist. Coverage of the whitelist is driven by what was retrieved, never
#: by what the schema happens to name.


def _row_label(tool: str, row: dict, params: dict) -> str:
    """Human-readable provenance for one row's values."""
    for key in ("account_name", "department_name", "name", "statement_line", "member"):
        if row.get(key):
            return str(row[key])
    dept = params.get("department_id")
    return f"{tool}{f' ({dept})' if dept else ''}"


#: Raw `variance` is uninterpretable at department-rollup grain, because a
#: department can span revenue and expense lines whose "bad" directions have
#: opposite signs. CORP is exactly that: revenue plus cost of revenue, no opex
#: at all, raw variance -76,065 against an operating-income impact of -142,611.
#:
#: A live run took the -76,065 and wrote "Corporate / Company ($76,065.38 over
#: budget)" inside a paragraph about operating expenses. Every figure passed the
#: audit -- the magnitude is real -- but the sign was inverted and the category
#: was wrong. Exposing a number whose sign cannot be read at that grain is an
#: invitation to misread it.
#:
#: So at department grain the raw variance is not whitelisted, and the
#: `variance` key is re-pointed at `oi_impact`: same magnitude, unambiguous
#: sign, and both keys carry the SAME number so there is no wrong one to pick.
#: The key is kept rather than dropped because the deterministic injection
#: narrative indexes it, and because a missing key is a worse failure than a
#: correctly-restated one.
#:
#: This also fixes a latent inaccuracy in the deterministic narrative, which
#: was pairing the raw variance magnitude ($76.1K) with the word "unfavorable"
#: derived from OI impact -- correct direction, wrong magnitude.
_AMBIGUOUS_AT_ROLLUP = {"variance"}


def _drops_raw_variance(tool: str, params: dict) -> bool:
    return tool == "rank_variance_drivers" and params.get("dimension") == "department"


def _whitelist_row(allowed: list, provenance: list, sec: dict, row: dict,
                   params: dict, entities: list) -> dict:
    """Add every numeric field of one row to the audit whitelist.

    Runs for EVERY retrieved row, including rows the canonical schema has no
    slot for. The whitelist is what the audit enforces, so its coverage must
    follow what the agent retrieved rather than what the prompt schema names --
    otherwise a figure the model legitimately saw would be rejected as
    fabricated.
    """
    tool = sec["tool"]
    label_base = _row_label(tool, row, params)
    numeric = {}

    for key in ("department_name", "account_name", "name"):
        v = row.get(key)
        if isinstance(v, str) and v:
            entities.append(v)

    drop = _AMBIGUOUS_AT_ROLLUP if _drops_raw_variance(tool, params) else frozenset()

    for field, value in row.items():
        if field in _NON_FACT_FIELDS or field in drop or value is None:
            continue
        spec = _FIELD_KINDS.get(field)
        if spec is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        kind, suffix = spec
        label = f"{label_base} {suffix}"
        _add(allowed, value, kind, label)
        provenance.append({"step": sec["step"], "tool": tool, "field": field,
                           "value": value, "label": label})
        numeric[field] = value
    return numeric


def fact_pack_from_ledger(result, goal: dict, scope: str = "company") -> FactPack:
    """Build the audit whitelist and prompt facts from what the agent retrieved.

    Returns ``status == "insufficient_data"`` when the run produced nothing
    narratable, which routes into the existing refusal path in
    ``generate_commentary`` rather than a second refusal mechanism.
    """
    period = goal.get("period", "")
    allowed: list = []
    entities: list = []
    provenance: list = []
    facts: dict = {"period": period, "scope": scope,
                   "comparison": goal.get("comparison", "actual_vs_budget")}
    extra: dict = {}
    drivers: list = []          # rollup grain (department / statement line)
    account_drivers: list = []  # account grain, from decompositions

    for sec in sorted(result.sections.values(), key=lambda s: s["step"]):
        tool, rows, params = sec["tool"], sec["rows"], sec.get("params", {})
        if not rows or tool in _ORIENTATION_TOOLS:
            continue

        rows_numeric = [_whitelist_row(allowed, provenance, sec, r, params, entities)
                        for r in rows]

        if tool == "get_operating_metrics":
            # Every whitelisted field, plus the aliases the canonical prompt
            # schema and the injection narrative read. Hand-picking a subset
            # here produced a run where the package printed
            # "ending ARR $28,809,278" while the commentary said
            # "No retention or ARR figures were provided" -- the value was
            # whitelisted but invisible to the model. See
            # test_every_whitelisted_value_is_visible_to_the_model.
            facts["company"] = {
                **rows_numeric[0],
                "revenue_actual": rows[0].get("revenue"),
                "operating_income_actual": rows[0].get("operating_income"),
            }

        elif tool == "get_pl_summary":
            lines = []
            for r, n in zip(rows, rows_numeric):
                entry = {**n, "line": r.get("statement_line"),
                         "budget": r.get("base"),
                         "pct_variance": r.get("variance_pct"),
                         "favorable": r.get("favorable")}
                lines.append(entry)
                if r.get("statement_line") == "Revenue":
                    facts["revenue_vs_budget"] = {
                        "actual": r.get("actual"), "budget": r.get("base"),
                        "variance": r.get("variance"),
                        "favorable": bool(r.get("favorable")),
                    }
            facts["pl_summary"] = lines

        elif tool in ("rank_variance_drivers", "decompose_variance"):
            # Kept apart by grain. A department rollup and one of its own
            # accounts are both legitimate drivers, but ranking them in one list
            # puts a parent beside its child and implies they are comparable --
            # "Corporate / Company $76.1K unfavorable; Subscription Revenue
            # (CORP) $102.2K unfavorable" reads as two findings when it is one
            # finding at two grains. A finance reader would catch that
            # immediately.
            bucket = account_drivers if tool == "decompose_variance" else drivers
            for r, n in zip(rows, rows_numeric):
                # At rollup grain `variance` is restated as the OI impact; at
                # account grain the row carries its statement_line, so the raw
                # sign is readable and is kept as-is.
                variance = (r.get("oi_impact") if _drops_raw_variance(tool, params)
                            else r.get("variance"))
                bucket.append({
                    **n,
                    "variance": variance,
                    "direction": ("favorable" if r.get("favorable")
                                  else "unfavorable"),
                    "line_item": r.get("account_name") or r.get("name") or r.get("member"),
                    "department": (params.get("department_id")
                                   or r.get("department_name")
                                   or r.get("member") or scope),
                    "grain": ("account" if tool == "decompose_variance"
                              else params.get("dimension", "department")),
                    "budget": r.get("base"),
                    "pct_variance": r.get("variance_pct"),
                    "favorable": bool(r.get("favorable")),
                })

        elif tool == "get_comp_decomposition":
            facts["comp_decomposition"] = [
                {**n,
                 "department": r.get("department_name") or r.get("department_id"),
                 "headcount_effect": r.get("hc_impact"),
                 "rate_effect": r.get("rate_impact")}
                for r, n in zip(rows, rows_numeric)
                if r.get("salary_variance") is not None
            ]

        elif tool == "get_revenue_decomposition":
            facts["revenue_decomposition"] = {
                **rows_numeric[0],
                "variance": rows[0].get("rev_variance"),
                "volume_effect": rows[0].get("volume_impact"),
                "price_effect": rows[0].get("price_impact"),
            }

        elif tool == "get_arr_bridge":
            # The injection narrative indexes these keys directly, and TTM
            # retention is None for the first 12 periods (there is no trailing
            # year yet). Dropping a None-valued field would remove the key
            # entirely and raise KeyError on 2024 periods, so the schema keys
            # are always present -- absent values are explicitly None rather
            # than missing.
            facts["arr"] = {k: rows[0].get(k) for k in
                            ("starting_arr", "new_arr", "expansion_arr",
                             "contraction_arr", "churned_arr", "ending_arr",
                             "nrr_ttm", "grr_ttm")}
            facts["arr"].update(rows_numeric[0])

        elif tool == "get_headcount_movement":
            facts["headcount"] = [
                {**n,
                 "department": r.get("department_name") or r.get("department_id"),
                 "hc_vs_plan": r.get("hc_var_vs_budget")}
                for r, n in zip(rows, rows_numeric)
            ]

        else:
            qualifier = params.get("dimension") or params.get("metric")
            key = f"{tool}({qualifier})" if qualifier else tool
            extra[key] = [n for n in rows_numeric if n]

    # Largest operating-income impact first, so "the biggest driver" in the
    # prose refers to the same row the package ranked first.
    for bucket in (drivers, account_drivers):
        bucket.sort(key=lambda d: abs(d.get("oi_impact") or 0), reverse=True)
    # `top_drivers` is the canonical slot the injection narrative reads. Prefer
    # the rollup grain; fall back to accounts when the plan only decomposed.
    if drivers:
        facts["top_drivers"] = drivers[:10]
        facts["how_to_read_drivers"] = (
            "oi_impact is the signed effect on operating income: negative is "
            "unfavorable regardless of whether the line is revenue or expense. "
            "'direction' states this in words and is authoritative. A department "
            "rollup may span revenue and expense lines, so it is NOT necessarily "
            "an operating-expense item -- do not describe one as 'over budget on "
            "opex' unless its detail confirms that."
        )
        if account_drivers:
            facts["driver_detail_by_account"] = account_drivers[:10]
    elif account_drivers:
        facts["top_drivers"] = account_drivers[:10]
    if extra:
        facts["additional_analysis"] = extra

    if not allowed:
        return FactPack(period, scope, "insufficient_data",
                        reason=("the run retrieved no narratable figures; "
                                "refusing rather than writing unsupported prose"))

    pack = FactPack(period, scope, "ok", prompt_facts=facts,
                    allowed_values=allowed, allowed_entities=sorted(set(entities)))
    # Not part of FactPack's contract, but carried for the audit trace: every
    # whitelisted value can be pointed back at the step that produced it.
    pack.ledger_provenance = provenance  # type: ignore[attr-defined]
    return pack
