"""
fact_index.py
=============
A flat, precomputed index of EVERY number in the tool -- all 24 months, every
department and account, YTD figures, decompositions, retention, headcount -- so
the chat can answer any question about the dataset.

Two things are deliberately separate:

  * ACCESS   -- the index holds everything. Nothing is off-limits to the chat.
  * DELIVERY -- for a given question, deterministic code selects the relevant
                slice and sends only that.

Why not send everything every turn? The numeric audit verifies each figure the
model writes against a whitelist of computed values. If the whitelist is the
entire dataset (thousands of numbers), a fabricated figure has a much better
chance of coincidentally landing near *some* value, and "verified" degrades from
"this is the number we computed" to "this resembles a number we computed
somewhere". Slicing keeps the whitelist tight, so the guarantee keeps its
meaning -- and it is far cheaper per turn.

The slice is chosen by code, never by the model, so the compute/explain boundary
holds: the model still cannot decide which numbers exist.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import pandas as pd

from narrative.fact_pack import AllowedValue

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

DEPT_ALIASES = {
    "SM": ["s&m", "sm", "sales", "marketing", "sales & marketing", "go-to-market", "gtm"],
    "RND": ["r&d", "rnd", "rd", "research", "development", "engineering", "product"],
    "GA": ["g&a", "ga", "general", "administrative", "admin", "back office"],
    "CS": ["cs", "customer success", "success", "support team"],
    "CORP": ["corp", "corporate", "company", "consolidated"],
}

METRIC_ALIASES = {
    "arr": ["arr", "annual recurring", "bridge"],
    "retention": ["nrr", "grr", "retention", "churn", "churned"],
    "margin": ["margin", "gross margin", "operating margin", "profitability"],
    "revenue": ["revenue", "sales", "bookings", "top line"],
    "opex": ["opex", "operating expense", "spend", "cost"],
    "headcount": ["headcount", "hc", "hiring", "hires", "people", "staff", "fte"],
    "comp": ["salary", "salaries", "compensation", "comp", "wage"],
}


@dataclass(frozen=True)
class FactRow:
    month: str
    department: str
    account: str
    metric: str
    value: float
    kind: str          # dollar | percent | count
    label: str


def _add(rows, month, dept, account, metric, value, kind, label):
    if value is None or pd.isna(value):
        return
    rows.append(FactRow(str(month), dept, account, metric, float(value), kind, label))


def build_fact_index(outputs: dict, tables: dict) -> list:
    """Flatten every computed table into one list of FactRow."""
    rows: list = []

    op = outputs["operating_metrics"]
    for _, r in op.iterrows():
        m = r["month"]
        for col, metric, kind in [
            ("revenue", "revenue", "dollar"), ("cogs", "cogs", "dollar"),
            ("gross_profit", "gross_profit", "dollar"), ("opex", "opex", "dollar"),
            ("operating_income", "operating_income", "dollar"),
            ("gross_margin", "margin", "percent"),
            ("operating_margin", "margin", "percent"),
            ("opex_pct_revenue", "opex", "percent"),
            ("total_headcount", "headcount", "count"),
            ("arr_per_head", "arr", "dollar"),
            ("revenue_per_head", "revenue", "dollar"),
        ]:
            if col in op.columns:
                _add(rows, m, "CORP", "", metric, r[col], kind,
                     f"{col.replace('_', ' ')} ({m})")

    saas = outputs["saas_metrics_summary"]
    for _, r in saas.iterrows():
        m = r["month"]
        for col, metric, kind in [
            ("starting_arr", "arr", "dollar"), ("new_arr", "arr", "dollar"),
            ("expansion_arr", "arr", "dollar"), ("contraction_arr", "arr", "dollar"),
            ("churned_arr", "retention", "dollar"), ("ending_arr", "arr", "dollar"),
            ("customers_end", "arr", "count"), ("nrr_ttm", "retention", "percent"),
            ("grr_ttm", "retention", "percent"), ("nrr", "retention", "percent"),
            ("grr", "retention", "percent"), ("logo_churn_rate", "retention", "percent"),
        ]:
            if col in saas.columns:
                _add(rows, m, "CORP", "", metric, r.get(col), kind,
                     f"{col.replace('_', ' ')} ({m})")

    line = outputs["variance_detail"]
    for _, r in line.iterrows():
        m, d, acct = r["month"], r["department_id"], r["account_name"]
        base = f"{acct} ({d}, {m})"
        _add(rows, m, d, acct, "actual", r["actual"], "dollar", f"{base} actual")
        _add(rows, m, d, acct, "budget", r["budget"], "dollar", f"{base} budget")
        _add(rows, m, d, acct, "variance", r["var_ab_amount"], "dollar", f"{base} variance")
        _add(rows, m, d, acct, "variance", r["oi_impact_ab"], "dollar", f"{base} OI impact")
        _add(rows, m, d, acct, "variance", r["var_ab_pct"], "percent", f"{base} % variance")
        if "actual_ytd" in line.columns:
            _add(rows, m, d, acct, "ytd", r["actual_ytd"], "dollar", f"{base} actual YTD")
            _add(rows, m, d, acct, "ytd", r["budget_ytd"], "dollar", f"{base} budget YTD")
            _add(rows, m, d, acct, "ytd", r["var_ab_ytd_amount"], "dollar",
                 f"{base} variance YTD")

    bd = outputs["variance_by_department"]
    for _, r in bd.iterrows():
        m, d = r["month"], r["department_id"]
        base = f"{d} total ({m})"
        _add(rows, m, d, "", "opex", r["actual"], "dollar", f"{base} actual")
        _add(rows, m, d, "", "opex", r["budget"], "dollar", f"{base} budget")
        _add(rows, m, d, "", "variance", r["var_ab_amount"], "dollar", f"{base} variance")

    comp = outputs["comp_decomposition"]
    for _, r in comp.iterrows():
        m, d = r["month"], r["department_id"]
        base = f"{d} salary ({m})"
        _add(rows, m, d, "Salaries", "comp", r["salary_variance"], "dollar", f"{base} variance")
        _add(rows, m, d, "Salaries", "comp", r["hc_impact"], "dollar", f"{base} headcount effect")
        _add(rows, m, d, "Salaries", "comp", r["rate_impact"], "dollar", f"{base} rate effect")

    rev = outputs["revenue_decomposition"]
    for _, r in rev.iterrows():
        m = r["month"]
        _add(rows, m, "CORP", "Subscription Revenue", "revenue", r["rev_variance"],
             "dollar", f"subscription revenue variance ({m})")
        _add(rows, m, "CORP", "Subscription Revenue", "revenue", r["volume_impact"],
             "dollar", f"revenue volume effect ({m})")
        _add(rows, m, "CORP", "Subscription Revenue", "revenue", r["price_impact"],
             "dollar", f"revenue price effect ({m})")

    hcp = outputs["headcount_vs_plan"]
    for _, r in hcp.iterrows():
        m, d = r["month"], r["department_id"]
        _add(rows, m, d, "", "headcount", r["actual_headcount"], "count",
             f"{d} headcount actual ({m})")
        _add(rows, m, d, "", "headcount", r["budget_headcount"], "count",
             f"{d} headcount budget ({m})")
        _add(rows, m, d, "", "headcount", r["hc_var_vs_budget"], "count",
             f"{d} headcount vs plan ({m})")

    return rows


# ---------------------------------------------------------------------------
# deterministic slice selection
# ---------------------------------------------------------------------------
def _months_in(question: str, all_months: list) -> set:
    q = question.lower()
    hits = set()
    for m in all_months:
        if m in q:                      # explicit 2025-09-01
            hits.add(m)
        y, mo, _ = m.split("-")
        if f"{y}-{mo}" in q:
            hits.add(m)
    for name, num in MONTH_NAMES.items():
        if re.search(rf"(?<![a-z]){name}(?![a-z])", q):
            for m in all_months:
                y, mo, _ = m.split("-")
                if int(mo) == num and (y in q or not re.search(r"20\d\d", q)):
                    hits.add(m)
    # fiscal year / quarter
    for y in re.findall(r"(?:fy)?\s?(20\d\d)", q):
        for m in all_months:
            if m.startswith(y):
                hits.add(m)
    for qtr in re.findall(r"q([1-4])", q):
        lo = (int(qtr) - 1) * 3 + 1
        for m in all_months:
            mo = int(m.split("-")[1])
            if lo <= mo <= lo + 2:
                hits.add(m)
    return hits


def _depts_in(question: str) -> set:
    q = question.lower()
    hits = set()
    for dept, aliases in DEPT_ALIASES.items():
        for a in aliases:
            if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", q):
                hits.add(dept)
                break
    return hits


def _accounts_in(question: str, all_accounts: list) -> set:
    q = question.lower()
    hits = set()
    for a in all_accounts:
        if a.lower() in q:
            hits.add(a)
    # common shorthands
    short = {"marketing": "Paid Marketing", "contractors": "Contractors",
             "legal": "Legal & Professional Fees", "hosting": "Hosting / Infrastructure",
             "commissions": "Commissions", "events": "Events",
             "salaries": "Salaries", "recruiting": "Recruiting", "facilities": "Facilities"}
    for k, v in short.items():
        if re.search(rf"(?<![a-z]){k}(?![a-z])", q) and v in all_accounts:
            hits.add(v)
    return hits


def _metrics_in(question: str) -> set:
    q = question.lower()
    hits = set()
    for metric, aliases in METRIC_ALIASES.items():
        for a in aliases:
            if a in q:
                hits.add(metric)
                break
    return hits


def select_facts(question: str, index: list, focus_month: str,
                 max_rows: int = 320) -> tuple:
    """
    Deterministically choose the facts relevant to `question`.
    Returns (selected_rows, scope_note).
    """
    all_months = sorted({r.month for r in index})
    all_accounts = sorted({r.account for r in index if r.account})

    months = _months_in(question, all_months)
    depts = _depts_in(question)
    accounts = _accounts_in(question, all_accounts)
    metrics = _metrics_in(question)

    scope_bits = []
    if months:
        scope_bits.append(f"{len(months)} month(s)")
    if depts:
        scope_bits.append("/".join(sorted(depts)))
    if accounts:
        scope_bits.append(", ".join(sorted(accounts)))
    if metrics:
        scope_bits.append("/".join(sorted(metrics)))

    def score(r: FactRow) -> int:
        s = 0
        if months and r.month in months:
            s += 3
        if not months and r.month == focus_month:
            s += 2
        if depts and r.department in depts:
            s += 2
        if accounts and r.account in accounts:
            s += 3
        if metrics and r.metric in metrics:
            s += 2
        # company headline is always mildly relevant
        if r.department == "CORP" and r.metric in ("revenue", "operating_income", "margin"):
            s += 1
        return s

    scored = [(score(r), i, r) for i, r in enumerate(index)]
    # if the question named nothing at all, fall back to a company-wide view
    if not (months or depts or accounts or metrics):
        picked = [r for r in index
                  if r.department == "CORP" or r.month == focus_month]
        picked = picked[:max_rows]
        return picked, "company overview + selected month (no specific scope detected)"

    scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1]))
    picked = [t[2] for t in scored[:max_rows]]
    note = "; ".join(scope_bits) if scope_bits else "selected month"
    return picked, note


def rows_to_payload(rows: list) -> dict:
    """Compact, labeled dict for the prompt."""
    out: dict = {}
    for r in rows:
        key = r.month
        out.setdefault(key, [])
        who = r.department + (f" / {r.account}" if r.account else "")
        out[key].append({"scope": who, "metric": r.metric,
                         "value": round(r.value, 5), "kind": r.kind})
    return out


def rows_to_allowed(rows: list) -> list:
    """Whitelist for the numeric audit: only what was actually sent."""
    allowed = []
    for r in rows:
        allowed.append(AllowedValue(r.value, r.kind, r.label))
        if r.value < 0:
            allowed.append(AllowedValue(abs(r.value), r.kind, r.label + " (magnitude)"))
    return allowed
