"""
tools.py
========
The agent's complete permitted surface: parameterized SQL over the materialized
marts. Every query in this file is written and tested by hand. The model chooses
which one runs and supplies dimension arguments; it never authors SQL.

SIGN CONVENTION -- read this before adding a tool
-------------------------------------------------
``var_ab_amount`` is a raw difference (actual - budget). Its sign is meaningless
on its own: an expense line $200K over budget and a revenue line $200K over
budget have identical raw variance and opposite business meaning.

``oi_impact_ab = oi_sign * var_ab_amount`` is the operating-income impact, and
``favorable_ab = oi_impact_ab > 0``. **Ranking, decomposition, and any
"biggest driver" logic must use oi_impact, never raw variance.** A tool that
ranks on ``abs(var_ab_amount)`` will present an expense overrun as a favorable
result, and every downstream guardrail will agree with it, because the number
itself is correct.

Every tool below returns ``oi_impact_*`` alongside the raw figure for exactly
this reason: the model should never be in a position to infer favorability from
a sign it has to reason about.

WHAT IS DELIBERATELY ABSENT
---------------------------
* No writes. The connection is read-only at the driver.
* No calculator, no eval, no code execution. The agent has no arithmetic
  capability at all. If a figure is needed that no tool returns, the correct
  behavior is refusal and a request for a new tool -- not derivation.
* No filesystem, network, or send capability. Publication is a human action.
* No free-text SQL. See ``materialize.py`` for why the model does not get to
  compose computation.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from agent.registry import (T_ACCOUNT, T_ACCOUNT_CATEGORY, T_BOOL, T_COUNT,
                            T_DEPARTMENT, T_MONEY, T_PERIOD, T_RATIO,
                            T_STATEMENT_LINE, T_TEXT, DimParam, EnumParam,
                            IntParam, PeriodParam, tool)
from calculations.variance_engine import MATERIALITY_FLOOR

# Comparisons the dataset actually supports. Modeled as a single enum rather
# than two free scenario arguments so that an unsupported pair (budget vs
# forecast, which the marts do not compute) is unrepresentable rather than a
# runtime failure the agent has to discover.
COMPARISONS = ["actual_vs_budget", "actual_vs_forecast"]

_CMP = {
    "actual_vs_budget": {
        "base": "budget",
        "var": "var_ab_amount",
        "oi": "oi_impact_ab",
        "pct": "var_ab_pct",
        "fav": "favorable_ab",
    },
    "actual_vs_forecast": {
        "base": "forecast",
        "var": "var_af_amount",
        "oi": "oi_impact_af",
        "pct": "var_af_pct",
        "fav": "favorable_af",
    },
}

TREND_METRICS = [
    "revenue", "cogs", "opex", "gross_profit", "gross_margin",
    "operating_income", "operating_margin", "opex_pct_revenue",
    "total_headcount", "ending_arr", "arr_per_head", "revenue_per_head",
]

MAX_TREND_MONTHS = 24


def _clean(v):
    """Coerce a DuckDB value to something JSON-serializable.

    The model's context is JSON, so a ``datetime.date`` or ``Decimal`` leaking
    through would fail serialization at the worst possible moment -- mid-run,
    after the query already succeeded. Dates become ISO strings; Decimals become
    floats. No rounding happens here: rounding is the query's job, so that what
    the ledger records and what the model sees are the same figure.
    """
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _rows(con, sql: str, params: list) -> list:
    """Execute and return list-of-dicts. Values are already-computed figures."""
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [{c: _clean(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


# --------------------------------------------------------------------------
# orientation
# --------------------------------------------------------------------------
@tool(
    name="list_periods",
    description=(
        "List every period in the dataset with which comparisons are available. "
        "Call this first when a goal names a period, so that a missing period is "
        "a retrieved fact rather than an assumption."
    ),
    params={},
    returns="month, fiscal_year, quarter, has_actuals, has_budget, has_forecast",
    field_types={"month": T_PERIOD, "fiscal_year": T_COUNT, "quarter": T_TEXT,
                 "has_actuals": T_BOOL, "has_budget": T_BOOL, "has_forecast": T_BOOL},
)
def list_periods(con):
    return _rows(
        con,
        """
        SELECT CAST(d.month AS VARCHAR) AS month,
               d.fiscal_year,
               d.quarter,
               COUNT(*) FILTER (WHERE v.actual   IS NOT NULL) > 0 AS has_actuals,
               COUNT(*) FILTER (WHERE v.budget   IS NOT NULL) > 0 AS has_budget,
               COUNT(*) FILTER (WHERE v.forecast IS NOT NULL) > 0 AS has_forecast
        FROM dim_date d
        LEFT JOIN out_variance_detail v ON v.month = CAST(d.month AS VARCHAR)
        GROUP BY d.month, d.fiscal_year, d.quarter, d.month_number
        ORDER BY d.month
        """,
        [],
    )


@tool(
    name="list_dimensions",
    description=(
        "List the valid members of a dimension. Use this before filtering on a "
        "member you are not certain exists."
    ),
    params={
        "dimension": EnumParam(
            ["department", "account", "statement_line", "account_category"],
            "Which dimension to enumerate.",
        )
    },
    returns="member id and name",
    field_types=lambda p: {"member": p.get("dimension"), "name": T_TEXT, "grp": T_TEXT},
)
def list_dimensions(con, dimension):
    sql = {
        "department": """
            SELECT department_id AS member, department_name AS name, department_group AS grp
            FROM dim_department ORDER BY department_id
        """,
        "account": """
            SELECT account_id AS member, account_name AS name, statement_line AS grp
            FROM dim_account ORDER BY account_id
        """,
        "statement_line": """
            SELECT DISTINCT statement_line AS member, statement_line AS name, '' AS grp
            FROM dim_account ORDER BY 1
        """,
        "account_category": """
            SELECT DISTINCT account_category AS member, account_category AS name,
                   statement_line AS grp
            FROM dim_account ORDER BY 1
        """,
    }[dimension]
    return _rows(con, sql, [])


# --------------------------------------------------------------------------
# headline
# --------------------------------------------------------------------------
@tool(
    name="get_pl_summary",
    description=(
        "Statement-line P&L summary for one period: actual, comparison base, "
        "variance, and operating-income impact. The canonical top-level view; "
        "start a variance package here."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "comparison": EnumParam(COMPARISONS, "Which comparison to run. Defaults to actual_vs_budget.", required=False, default="actual_vs_budget"),
    },
    returns="statement_line, actual, base, variance, oi_impact, variance_pct, favorable",
    field_types={"statement_line": T_STATEMENT_LINE, "actual": T_MONEY, "base": T_MONEY,
                 "variance": T_MONEY, "oi_impact": T_MONEY,
                 "variance_pct": T_RATIO, "favorable": T_BOOL},
)
def get_pl_summary(con, period, comparison):
    c = _CMP[comparison]
    # Aggregated from the detail mart rather than read from the pre-rolled
    # statement-line mart. The pandas ``rollup()`` only carries actual-vs-budget
    # columns, so reading it would silently make actual_vs_forecast unavailable
    # at this grain. Summing already-computed line-level figures is aggregation,
    # not recomputation -- it is exactly what rollup() does -- and
    # ``test_pl_summary_matches_computation`` pins this SQL against the pandas
    # rollup on the budget path to prove the two agree.
    return _rows(
        con,
        f"""
        WITH agg AS (
            SELECT statement_line,
                   SUM(actual)      AS actual,
                   SUM({c['base']}) AS base,
                   SUM({c['var']})  AS variance,
                   SUM({c['oi']})   AS oi_impact
            FROM out_variance_detail
            WHERE month = ? AND {c['base']} IS NOT NULL
            GROUP BY statement_line
        )
        SELECT statement_line,
               ROUND(actual, 2)    AS actual,
               ROUND(base, 2)      AS base,
               ROUND(variance, 2)  AS variance,
               ROUND(oi_impact, 2) AS oi_impact,
               CASE WHEN ABS(base) >= ? THEN ROUND(variance / base, 5) END AS variance_pct,
               oi_impact > 0       AS favorable
        FROM agg
        ORDER BY CASE statement_line
                   WHEN 'Revenue' THEN 1
                   WHEN 'Cost of Revenue' THEN 2
                   ELSE 3 END
        """,
        [period, MATERIALITY_FLOOR],
    )


@tool(
    name="get_operating_metrics",
    description=(
        "Company operating metrics for one period: revenue, margins, operating "
        "income, headcount, ARR, and efficiency ratios."
    ),
    params={"period": PeriodParam("The reporting period.")},
    returns="revenue, cogs, opex, gross_profit, gross_margin, operating_income, operating_margin, opex_pct_revenue, total_headcount, ending_arr, arr_per_head, revenue_per_head",
    field_types={"month": T_PERIOD, "revenue": T_MONEY, "cogs": T_MONEY, "opex": T_MONEY,
                 "gross_profit": T_MONEY, "gross_margin": T_RATIO,
                 "operating_income": T_MONEY, "operating_margin": T_RATIO,
                 "opex_pct_revenue": T_RATIO, "total_headcount": T_COUNT,
                 "ending_arr": T_MONEY, "arr_per_head": T_MONEY,
                 "revenue_per_head": T_MONEY},
)
def get_operating_metrics(con, period):
    return _rows(
        con,
        """
        SELECT month, ROUND(revenue,2) AS revenue, ROUND(cogs,2) AS cogs,
               ROUND(opex,2) AS opex, ROUND(gross_profit,2) AS gross_profit,
               gross_margin, ROUND(operating_income,2) AS operating_income,
               operating_margin, opex_pct_revenue, total_headcount,
               ROUND(ending_arr,2) AS ending_arr,
               ROUND(arr_per_head,2) AS arr_per_head,
               ROUND(revenue_per_head,2) AS revenue_per_head
        FROM out_operating_metrics WHERE month = ?
        """,
        [period],
    )


# --------------------------------------------------------------------------
# ranking and decomposition
# --------------------------------------------------------------------------
@tool(
    name="rank_variance_drivers",
    description=(
        "Rank the largest variance drivers for a period by absolute "
        "operating-income impact. Ranking is computed in SQL, not inferred by "
        "reading a table."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "dimension": EnumParam(
            ["department", "statement_line", "account", "account_category"],
            "The grain to rank at.",
        ),
        "comparison": EnumParam(COMPARISONS, "Which comparison to rank. Defaults to actual_vs_budget.", required=False, default="actual_vs_budget"),
        "top_n": IntParam(1, 10, "How many drivers to return.", required=False, default=5),
    },
    returns="rank, member, name, actual, base, variance, oi_impact, share_of_total_oi_impact, favorable",
    field_types=lambda p: {"rank": T_COUNT, "member": p.get("dimension"), "name": T_TEXT,
                       "actual": T_MONEY, "base": T_MONEY, "variance": T_MONEY,
                       "oi_impact": T_MONEY,
                       "share_of_total_oi_impact": T_RATIO, "favorable": T_BOOL},
)
def rank_variance_drivers(con, period, dimension, comparison, top_n):
    c = _CMP[comparison]

    # `name` must be the *canonical entity name* (e.g. "Sales & Marketing"), not
    # the id ("SM"), because the entity audit whitelists department_name and
    # account_name. A tool that returns ids would hand the narrative layer
    # tokens its own guardrail rejects.
    if dimension == "department":
        src = "out_variance_detail v LEFT JOIN dim_department d USING (department_id)"
        key, name = "v.department_id", "d.department_name"
    elif dimension == "statement_line":
        src = "out_variance_detail v"
        key, name = "v.statement_line", "v.statement_line"
    elif dimension == "account":
        src = "out_variance_detail v"
        key, name = "v.account_id", "v.account_name"
    else:
        src = "out_variance_detail v"
        key, name = "v.account_category", "v.account_category"

    # share_of_total is a window function over the period, so the model never
    # divides one retrieved figure by another.
    return _rows(
        con,
        f"""
        WITH agg AS (
            SELECT {key} AS member,
                   MAX({name}) AS name,
                   SUM(v.actual)      AS actual,
                   SUM(v.{c['base']}) AS base,
                   SUM(v.{c['var']})  AS variance,
                   SUM(v.{c['oi']})   AS oi_impact
            FROM {src}
            WHERE v.month = ? AND v.{c['base']} IS NOT NULL
            GROUP BY {key}
        ), tot AS (
            SELECT SUM(ABS(oi_impact)) AS abs_total FROM agg
        )
        SELECT ROW_NUMBER() OVER (ORDER BY ABS(a.oi_impact) DESC, a.member) AS rank,
               a.member, a.name,
               ROUND(a.actual, 2)    AS actual,
               ROUND(a.base, 2)      AS base,
               ROUND(a.variance, 2)  AS variance,
               ROUND(a.oi_impact, 2) AS oi_impact,
               CASE WHEN t.abs_total > 0
                    THEN ROUND(ABS(a.oi_impact) / t.abs_total, 5) END
                                     AS share_of_total_oi_impact,
               a.oi_impact > 0       AS favorable
        FROM agg a CROSS JOIN tot t
        ORDER BY ABS(a.oi_impact) DESC, a.member
        LIMIT ?
        """,
        [period, top_n],
    )


@tool(
    name="decompose_variance",
    description=(
        "Break one department's variance down to the account level for a period, "
        "with each account's share of that department's total impact."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "department_id": DimParam("department", "The department to decompose."),
        "comparison": EnumParam(COMPARISONS, "Which comparison to decompose. Defaults to actual_vs_budget.", required=False, default="actual_vs_budget"),
        "top_n": IntParam(1, 10, "How many accounts to return.", required=False, default=5),
    },
    returns="rank, account_id, account_name, account_category, actual, base, variance, oi_impact, share_of_department_oi_impact, favorable",
    field_types={"rank": T_COUNT, "account_id": T_ACCOUNT, "account_name": T_TEXT,
                 "account_category": T_ACCOUNT_CATEGORY,
                 "statement_line": T_STATEMENT_LINE,
                 "department_name": T_TEXT, "actual": T_MONEY, "base": T_MONEY,
                 "variance": T_MONEY, "oi_impact": T_MONEY,
                 "share_of_department_oi_impact": T_RATIO, "favorable": T_BOOL},
)
def decompose_variance(con, period, department_id, comparison, top_n):
    c = _CMP[comparison]
    return _rows(
        con,
        f"""
        WITH d AS (
            SELECT v.account_id, v.account_name, v.account_category, v.statement_line,
                   dd.department_name,
                   v.actual, v.{c['base']} AS base, v.{c['var']} AS variance,
                   v.{c['oi']} AS oi_impact
            FROM out_variance_detail v
            LEFT JOIN dim_department dd USING (department_id)
            WHERE v.month = ? AND v.department_id = ? AND v.{c['base']} IS NOT NULL
        ), tot AS (
            SELECT SUM(ABS(oi_impact)) AS abs_total FROM d
        )
        SELECT ROW_NUMBER() OVER (ORDER BY ABS(d.oi_impact) DESC, d.account_id) AS rank,
               d.department_name,
               d.account_id, d.account_name, d.account_category, d.statement_line,
               ROUND(d.actual, 2)    AS actual,
               ROUND(d.base, 2)      AS base,
               ROUND(d.variance, 2)  AS variance,
               ROUND(d.oi_impact, 2) AS oi_impact,
               CASE WHEN t.abs_total > 0
                    THEN ROUND(ABS(d.oi_impact) / t.abs_total, 5) END
                                     AS share_of_department_oi_impact,
               d.oi_impact > 0       AS favorable
        FROM d CROSS JOIN tot t
        ORDER BY ABS(d.oi_impact) DESC, d.account_id
        LIMIT ?
        """,
        [period, department_id, top_n],
    )


@tool(
    name="get_comp_decomposition",
    description=(
        "Split a department's personnel-cost variance into the portion explained "
        "by headcount versus the portion explained by average compensation rate, "
        "for one period. Use this when personnel is a top driver."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "department_id": DimParam("department", "The department to decompose.", required=False),
    },
    returns="department_id, salary_budget, salary_actual, salary_variance, hc_impact, rate_impact, decomp_residual, favorable",
    field_types={"month": T_PERIOD, "department_id": T_DEPARTMENT, "department_name": T_TEXT,
                 "salary_budget": T_MONEY, "salary_actual": T_MONEY,
                 "salary_variance": T_MONEY, "hc_impact": T_MONEY,
                 "rate_impact": T_MONEY, "decomp_residual": T_MONEY,
                 "favorable": T_BOOL},
)
def get_comp_decomposition(con, period, department_id):
    sql = """
        SELECT c.month, c.department_id, d.department_name,
               ROUND(salary_budget, 2)   AS salary_budget,
               ROUND(salary_actual, 2)   AS salary_actual,
               ROUND(salary_variance, 2) AS salary_variance,
               ROUND(hc_impact, 2)       AS hc_impact,
               ROUND(rate_impact, 2)     AS rate_impact,
               ROUND(decomp_residual, 2) AS decomp_residual,
               favorable
        FROM out_comp_decomposition c
        LEFT JOIN dim_department d USING (department_id)
        WHERE c.month = ?
    """
    params = [period]
    if department_id:
        sql += " AND c.department_id = ?"
        params.append(department_id)
    sql += " ORDER BY ABS(salary_variance) DESC"
    return _rows(con, sql, params)


@tool(
    name="get_revenue_decomposition",
    description=(
        "Split total revenue variance for a period into volume (customer count) "
        "and price (ARPA) effects."
    ),
    params={"period": PeriodParam("The reporting period.")},
    returns="rev_budget, rev_actual, rev_variance, volume_impact, price_impact, decomp_residual, favorable",
    field_types={"month": T_PERIOD, "rev_budget": T_MONEY, "rev_actual": T_MONEY,
                 "rev_variance": T_MONEY, "volume_impact": T_MONEY,
                 "price_impact": T_MONEY, "decomp_residual": T_MONEY,
                 "favorable": T_BOOL},
)
def get_revenue_decomposition(con, period):
    return _rows(
        con,
        """
        SELECT month,
               ROUND(rev_b, 2)           AS rev_budget,
               ROUND(rev_a, 2)           AS rev_actual,
               ROUND(rev_variance, 2)    AS rev_variance,
               ROUND(volume_impact, 2)   AS volume_impact,
               ROUND(price_impact, 2)    AS price_impact,
               ROUND(decomp_residual, 2) AS decomp_residual,
               favorable
        FROM out_revenue_decomposition WHERE month = ?
        """,
        [period],
    )


# --------------------------------------------------------------------------
# SaaS and headcount
# --------------------------------------------------------------------------
@tool(
    name="get_arr_bridge",
    description=(
        "The ARR walk for one period: starting ARR, new, expansion, contraction, "
        "churn, ending ARR, plus customer counts and retention."
    ),
    params={"period": PeriodParam("The reporting period.")},
    returns="starting_arr, new_arr, expansion_arr, contraction_arr, churned_arr, ending_arr, bridge_diff, customers_start, customers_end, arpa, nrr, grr, nrr_ttm, grr_ttm",
    field_types={"month": T_PERIOD, "starting_arr": T_MONEY, "new_arr": T_MONEY,
                 "expansion_arr": T_MONEY, "contraction_arr": T_MONEY,
                 "churned_arr": T_MONEY, "ending_arr": T_MONEY,
                 "bridge_diff": T_MONEY, "customers_start": T_COUNT,
                 "new_customers": T_COUNT, "churned_customers": T_COUNT,
                 "customers_end": T_COUNT, "arpa": T_MONEY, "nrr": T_RATIO,
                 "grr": T_RATIO, "nrr_ttm": T_RATIO, "grr_ttm": T_RATIO},
)
def get_arr_bridge(con, period):
    return _rows(
        con,
        """
        SELECT month,
               ROUND(starting_arr, 2)    AS starting_arr,
               ROUND(new_arr, 2)         AS new_arr,
               ROUND(expansion_arr, 2)   AS expansion_arr,
               ROUND(contraction_arr, 2) AS contraction_arr,
               ROUND(churned_arr, 2)     AS churned_arr,
               ROUND(ending_arr, 2)      AS ending_arr,
               ROUND(bridge_diff, 2)     AS bridge_diff,
               customers_start, new_customers, churned_customers, customers_end,
               ROUND(arpa, 2)            AS arpa,
               nrr, grr, nrr_ttm, grr_ttm
        FROM out_saas_metrics WHERE month = ?
        """,
        [period],
    )


@tool(
    name="get_headcount_movement",
    description=(
        "Headcount for one period by department: actual, budget, forecast, and "
        "variance to each. NOTE: the dataset records headcount as a period-end "
        "stock, not a flow -- hires and attrition are not separately available, "
        "so this returns levels and net variance, not a hires/attrition bridge."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "department_id": DimParam("department", "Limit to one department.", required=False),
    },
    returns="department_id, actual_headcount, budget_headcount, forecast_headcount, hc_var_vs_budget, hc_var_vs_forecast",
    field_types={"month": T_PERIOD, "department_id": T_DEPARTMENT, "department_name": T_TEXT,
                 "actual_headcount": T_COUNT, "budget_headcount": T_COUNT,
                 "forecast_headcount": T_COUNT, "hc_var_vs_budget": T_COUNT,
                 "hc_var_vs_forecast": T_COUNT},
)
def get_headcount_movement(con, period, department_id):
    sql = """
        SELECT h.month, h.department_id, d.department_name,
               h.actual_headcount, h.budget_headcount,
               h.forecast_headcount, h.hc_var_vs_budget, h.hc_var_vs_forecast
        FROM out_headcount_vs_plan h
        LEFT JOIN dim_department d USING (department_id)
        WHERE h.month = ?
    """
    params = [period]
    if department_id:
        sql += " AND h.department_id = ?"
        params.append(department_id)
    sql += " ORDER BY h.department_id"
    return _rows(con, sql, params)


# --------------------------------------------------------------------------
# trend
# --------------------------------------------------------------------------
@tool(
    name="get_trend",
    description=(
        "A monthly time series for one company metric across a bounded window. "
        "Use for seasonality and run-rate context, not for computing variances."
    ),
    params={
        "metric": EnumParam(TREND_METRICS, "Which metric to trend."),
        "start_period": PeriodParam("First period in the window, inclusive."),
        "end_period": PeriodParam("Last period in the window, inclusive."),
    },
    returns="month, value",
    field_types={"month": T_PERIOD, "value": T_MONEY, "metric": T_TEXT},
)
def get_trend(con, metric, start_period, end_period):
    if start_period > end_period:
        raise ValueError(f"start_period {start_period} is after end_period {end_period}")
    n = con.execute(
        "SELECT COUNT(*) FROM dim_date WHERE CAST(month AS VARCHAR) BETWEEN ? AND ?",
        [start_period, end_period],
    ).fetchone()[0]
    if n > MAX_TREND_MONTHS:
        raise ValueError(
            f"window of {n} months exceeds the {MAX_TREND_MONTHS}-month limit; narrow the range"
        )
    # `metric` is an enum member validated against TREND_METRICS before this
    # body runs, so interpolating it as an identifier is safe. Values are still
    # bound as parameters.
    return _rows(
        con,
        f"""
        SELECT month, {metric} AS value, '{metric}' AS metric
        FROM out_operating_metrics
        WHERE month BETWEEN ? AND ?
        ORDER BY month
        """,
        [start_period, end_period],
    )


# ==========================================================================
# comparative and trend tools
# ==========================================================================
# Added because the original eleven tools all answered variations of "what
# happened this month against plan", so every question a planner was given
# landed on the same four or five calls and the plans looked canned. That was a
# TOOL SURFACE problem, not a prompt problem: a planner cannot produce a
# distinctive plan for "is this a one-off or a pattern?" when nothing in the
# registry can look across months.
#
# Each tool below maps to a question an FP&A analyst actually asks, and each
# reads a mart that the deterministic package never touched.

@tool(
    name="compare_periods",
    description=(
        "Compare actuals between two periods at a chosen grain, ranked by the "
        "size of the change. Use for 'how does this month compare with last "
        "month / the same month last year'."
    ),
    params={
        "period_a": PeriodParam("The later period (the one being explained)."),
        "period_b": PeriodParam("The earlier period to compare against."),
        "dimension": EnumParam(
            ["department", "statement_line", "account", "account_category"],
            "The grain to compare at."),
        "top_n": IntParam(1, 10, "How many movers to return.",
                          required=False, default=5),
    },
    returns="rank, member, name, actual_a, actual_b, change, change_pct, oi_impact_of_change",
    field_types=lambda p: {"rank": T_COUNT, "member": p.get("dimension"),
                           "name": T_TEXT, "actual_a": T_MONEY,
                           "actual_b": T_MONEY, "change": T_MONEY,
                           "change_pct": T_RATIO,
                           "oi_impact_of_change": T_MONEY},
)
def compare_periods(con, period_a, period_b, dimension, top_n):
    key = {"department": "department_id", "statement_line": "statement_line",
           "account": "account_id", "account_category": "account_category"}[dimension]
    name = {"department": "d.department_name", "statement_line": "v.statement_line",
            "account": "MAX(v.account_name)",
            "account_category": "v.account_category"}[dimension]
    join = ("LEFT JOIN dim_department d USING (department_id)"
            if dimension == "department" else "")
    grp = name if name.startswith("MAX(") else name

    return _rows(
        con,
        f"""
        WITH a AS (
            SELECT v.{key} AS member, {name if not name.startswith('MAX(') else 'MAX(v.account_name)'} AS name,
                   SUM(v.actual) AS actual_a,
                   SUM(v.oi_sign * v.actual) AS oi_a
            FROM out_variance_detail v {join}
            WHERE v.month = ?
            GROUP BY v.{key}{'' if name.startswith('MAX(') else ', ' + grp}
        ), b AS (
            SELECT {key} AS member, SUM(actual) AS actual_b,
                   SUM(oi_sign * actual) AS oi_b
            FROM out_variance_detail WHERE month = ?
            GROUP BY {key}
        )
        SELECT ROW_NUMBER() OVER (
                   ORDER BY ABS(COALESCE(a.actual_a,0) - COALESCE(b.actual_b,0)) DESC,
                            a.member) AS rank,
               a.member, a.name,
               ROUND(a.actual_a, 2) AS actual_a,
               ROUND(b.actual_b, 2) AS actual_b,
               ROUND(a.actual_a - b.actual_b, 2) AS change,
               CASE WHEN ABS(b.actual_b) >= ?
                    THEN ROUND((a.actual_a - b.actual_b) / b.actual_b, 5) END
                    AS change_pct,
               ROUND(a.oi_a - b.oi_b, 2) AS oi_impact_of_change
        FROM a LEFT JOIN b USING (member)
        ORDER BY ABS(COALESCE(a.actual_a,0) - COALESCE(b.actual_b,0)) DESC, a.member
        LIMIT ?
        """,
        [period_a, period_b, MATERIALITY_FLOOR, top_n],
    )


@tool(
    name="get_ytd_summary",
    description=(
        "Year-to-date actual versus budget by statement line, through the given "
        "period. Use for 'how are we tracking for the year', which a single "
        "month cannot answer."
    ),
    params={"period": PeriodParam("Period through which to accumulate.")},
    returns="statement_line, actual_ytd, budget_ytd, variance_ytd, oi_impact_ytd, variance_pct, favorable",
    field_types={"statement_line": T_STATEMENT_LINE, "actual_ytd": T_MONEY,
                 "budget_ytd": T_MONEY, "variance_ytd": T_MONEY,
                 "oi_impact_ytd": T_MONEY, "variance_pct": T_RATIO,
                 "favorable": T_BOOL},
)
def get_ytd_summary(con, period):
    return _rows(
        con,
        """
        WITH agg AS (
            SELECT statement_line,
                   SUM(actual_ytd)        AS actual_ytd,
                   SUM(budget_ytd)        AS budget_ytd,
                   SUM(var_ab_ytd_amount) AS variance_ytd,
                   SUM(oi_impact_ytd)     AS oi_impact_ytd
            FROM out_variance_detail
            WHERE month = ? AND budget_ytd IS NOT NULL
            GROUP BY statement_line
        )
        SELECT statement_line,
               ROUND(actual_ytd, 2)    AS actual_ytd,
               ROUND(budget_ytd, 2)    AS budget_ytd,
               ROUND(variance_ytd, 2)  AS variance_ytd,
               ROUND(oi_impact_ytd, 2) AS oi_impact_ytd,
               CASE WHEN ABS(budget_ytd) >= ?
                    THEN ROUND(variance_ytd / budget_ytd, 5) END AS variance_pct,
               oi_impact_ytd > 0 AS favorable
        FROM agg
        ORDER BY CASE statement_line
                   WHEN 'Revenue' THEN 1 WHEN 'Cost of Revenue' THEN 2
                   ELSE 3 END
        """,
        [period, MATERIALITY_FLOOR],
    )


@tool(
    name="rank_persistent_drivers",
    description=(
        "Rank drivers by how MANY of the last N months they were unfavourable, "
        "not by this month's size. Use to separate a one-off from a pattern -- "
        "a line that misses every month is a different problem from one that "
        "missed once."
    ),
    params={
        "period": PeriodParam("The most recent period in the window."),
        "months_back": IntParam(2, 12, "How many months to look back over.",
                                required=False, default=6),
        "dimension": EnumParam(["department", "account", "account_category"],
                               "The grain to rank at."),
        "top_n": IntParam(1, 10, "How many to return.",
                          required=False, default=5),
    },
    returns="rank, member, name, months_unfavorable, months_observed, cumulative_oi_impact, worst_month_oi_impact",
    field_types=lambda p: {"rank": T_COUNT, "member": p.get("dimension"),
                           "name": T_TEXT, "months_unfavorable": T_COUNT,
                           "months_observed": T_COUNT,
                           "cumulative_oi_impact": T_MONEY,
                           "worst_month_oi_impact": T_MONEY},
)
def rank_persistent_drivers(con, period, months_back, dimension, top_n):
    key = {"department": "v.department_id", "account": "v.account_id",
           "account_category": "v.account_category"}[dimension]
    name = {"department": "MAX(d.department_name)",
            "account": "MAX(v.account_name)",
            "account_category": "MAX(v.account_category)"}[dimension]
    join = ("LEFT JOIN dim_department d USING (department_id)"
            if dimension == "department" else "")

    return _rows(
        con,
        f"""
        WITH window_months AS (
            SELECT CAST(month AS VARCHAR) AS m FROM dim_date
            WHERE CAST(month AS VARCHAR) <= ?
            ORDER BY month DESC LIMIT ?
        ), agg AS (
            SELECT {key} AS member, {name} AS name,
                   COUNT(*) FILTER (WHERE v.oi_impact_ab < 0) AS months_unfavorable,
                   COUNT(DISTINCT v.month)                    AS months_observed,
                   SUM(v.oi_impact_ab)                        AS cumulative_oi_impact,
                   MIN(v.oi_impact_ab)                        AS worst_month_oi_impact
            FROM out_variance_detail v {join}
            WHERE v.month IN (SELECT m FROM window_months)
              AND v.budget IS NOT NULL
            GROUP BY {key}
        )
        SELECT ROW_NUMBER() OVER (
                   ORDER BY months_unfavorable DESC,
                            cumulative_oi_impact ASC, member) AS rank,
               member, name, months_unfavorable, months_observed,
               ROUND(cumulative_oi_impact, 2)  AS cumulative_oi_impact,
               ROUND(worst_month_oi_impact, 2) AS worst_month_oi_impact
        FROM agg
        WHERE months_unfavorable > 0
        ORDER BY months_unfavorable DESC, cumulative_oi_impact ASC, member
        LIMIT ?
        """,
        [period, months_back, top_n],
    )


@tool(
    name="get_account_trend",
    description=(
        "One account's actual and budget month by month across a bounded "
        "window. Use after a driver is identified, to see whether it is "
        "growing, one-off, or seasonal."
    ),
    params={
        "account_id": DimParam("account", "The account to trend."),
        "start_period": PeriodParam("First period, inclusive."),
        "end_period": PeriodParam("Last period, inclusive."),
    },
    returns="month, account_name, actual, budget, variance, oi_impact",
    field_types={"month": T_PERIOD, "account_name": T_TEXT, "actual": T_MONEY,
                 "budget": T_MONEY, "variance": T_MONEY, "oi_impact": T_MONEY},
)
def get_account_trend(con, account_id, start_period, end_period):
    if start_period > end_period:
        raise ValueError(f"start_period {start_period} is after {end_period}")
    n = con.execute(
        "SELECT COUNT(*) FROM dim_date WHERE CAST(month AS VARCHAR) BETWEEN ? AND ?",
        [start_period, end_period]).fetchone()[0]
    if n > MAX_TREND_MONTHS:
        raise ValueError(f"window of {n} months exceeds the "
                         f"{MAX_TREND_MONTHS}-month limit; narrow the range")
    return _rows(
        con,
        """
        SELECT month, MAX(account_name) AS account_name,
               ROUND(SUM(actual), 2)       AS actual,
               ROUND(SUM(budget), 2)       AS budget,
               ROUND(SUM(var_ab_amount), 2) AS variance,
               ROUND(SUM(oi_impact_ab), 2)  AS oi_impact
        FROM out_variance_detail
        WHERE account_id = ? AND month BETWEEN ? AND ?
        GROUP BY month ORDER BY month
        """,
        [account_id, start_period, end_period],
    )


@tool(
    name="get_opex_ratio_trend",
    description=(
        "A department's operating expense as a percentage of company revenue, "
        "month by month. Use for 'is this team's spend growing faster than the "
        "business', which an absolute variance cannot answer."
    ),
    params={
        "department_id": DimParam("department", "The department to trend."),
        "start_period": PeriodParam("First period, inclusive."),
        "end_period": PeriodParam("Last period, inclusive."),
    },
    returns="month, department_id, dept_opex, revenue, dept_opex_pct_revenue",
    field_types={"month": T_PERIOD, "department_id": T_DEPARTMENT,
                 "department_name": T_TEXT, "dept_opex": T_MONEY,
                 "revenue": T_MONEY, "dept_opex_pct_revenue": T_RATIO},
)
def get_opex_ratio_trend(con, department_id, start_period, end_period):
    if start_period > end_period:
        raise ValueError(f"start_period {start_period} is after {end_period}")
    return _rows(
        con,
        """
        SELECT o.month, o.department_id, d.department_name,
               ROUND(o.dept_opex, 2) AS dept_opex,
               ROUND(o.revenue, 2)   AS revenue,
               o.dept_opex_pct_revenue
        FROM out_dept_opex_pct_revenue o
        LEFT JOIN dim_department d USING (department_id)
        WHERE o.department_id = ? AND o.month BETWEEN ? AND ?
        ORDER BY o.month
        """,
        [department_id, start_period, end_period],
    )


@tool(
    name="rank_mom_movers",
    description=(
        "Rank the largest month-over-month changes in actuals for a period. "
        "Use for 'what changed since last month', which is a different question "
        "from 'what missed plan'."
    ),
    params={
        "period": PeriodParam("The reporting period."),
        "dimension": EnumParam(["department", "account", "account_category"],
                               "The grain to rank at."),
        "top_n": IntParam(1, 10, "How many movers to return.",
                          required=False, default=5),
    },
    returns="rank, member, name, actual, change_vs_prior_month, change_pct, oi_impact_of_change",
    field_types=lambda p: {"rank": T_COUNT, "member": p.get("dimension"),
                           "name": T_TEXT, "actual": T_MONEY,
                           "change_vs_prior_month": T_MONEY,
                           "change_pct": T_RATIO,
                           "oi_impact_of_change": T_MONEY},
)
def rank_mom_movers(con, period, dimension, top_n):
    key = {"department": "v.department_id", "account": "v.account_id",
           "account_category": "v.account_category"}[dimension]
    name = {"department": "MAX(d.department_name)",
            "account": "MAX(v.account_name)",
            "account_category": "MAX(v.account_category)"}[dimension]
    join = ("LEFT JOIN dim_department d USING (department_id)"
            if dimension == "department" else "")
    return _rows(
        con,
        f"""
        WITH agg AS (
            SELECT {key} AS member, {name} AS name,
                   SUM(v.actual)                            AS actual,
                   SUM(v.actual_mom_amount)                 AS change_amt,
                   SUM(v.oi_sign * v.actual_mom_amount)     AS oi_change,
                   SUM(v.actual) - SUM(v.actual_mom_amount) AS prior_actual
            FROM out_variance_detail v {join}
            WHERE v.month = ? AND v.actual_mom_amount IS NOT NULL
            GROUP BY {key}
        )
        SELECT ROW_NUMBER() OVER (ORDER BY ABS(change_amt) DESC, member) AS rank,
               member, name,
               ROUND(actual, 2)     AS actual,
               ROUND(change_amt, 2) AS change_vs_prior_month,
               CASE WHEN ABS(prior_actual) >= ?
                    THEN ROUND(change_amt / prior_actual, 5) END AS change_pct,
               ROUND(oi_change, 2)  AS oi_impact_of_change
        FROM agg
        ORDER BY ABS(change_amt) DESC, member
        LIMIT ?
        """,
        [period, MATERIALITY_FLOOR, top_n],
    )
