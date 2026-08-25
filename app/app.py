"""
app.py — AI FP&A Variance Commentary Copilot
============================================
A finance-owned variance tool where code computes every number and the model
only explains. Safe to share as a public link: everything it shows is
deterministic and key-free. Live model generation and the analyst chat activate
only when an API key is present.

Run locally:   streamlit run app/app.py
Deploy:        Streamlit Community Cloud, entrypoint app/app.py (see DEPLOY.md)
"""
from __future__ import annotations
import os
import sys
import json
import html
import pandas as pd
import streamlit as st
import altair as alt

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(APP_DIR)
for p in (os.path.join(REPO, "src"), os.path.join(REPO, "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

import generate_synthetic_data as gen
import run_pipeline as rp
from scenario import Scenario, DEPTS
from calculations import driver_decomposition as dd
from calculations import projection as PJ
from narrative import fact_pack as fp
from narrative import prompt as P
from narrative import generate as G
from narrative import fact_index as FI
from narrative import chat as CH
from narrative.llm_client import AnthropicClient, OpenAIClient, ReplayClient
from guardrails import numeric_audit as na
from guardrails import entity_audit as ea

st.set_page_config(page_title="AI FP&A Variance Copilot", page_icon="▨",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
:root { --ink:#0f172a; --teal:#0f766e; --fav:#15803d; --unfav:#b91c1c;
        --amber:#b45309; --line:#e2e8f0; --mut:#64748b; }
html, body, [class*="css"] { color: var(--ink); }
.mono, .stMetric [data-testid="stMetricValue"] {
  font-family: "SFMono-Regular", ui-monospace, "JetBrains Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums; }
.eyebrow { font-size:.72rem; letter-spacing:.14em; text-transform:uppercase;
  color:var(--mut); font-weight:600; margin-bottom:.15rem; }
.headline { font-size:1.65rem; font-weight:700; line-height:1.15; margin:.1rem 0 .1rem; }
.rule { height:2px; background:var(--teal); width:44px; border:0; margin:.4rem 0 1rem; }
.chip { display:inline-block; font-family:ui-monospace,Menlo,monospace;
  font-size:.86rem; padding:2px 8px; margin:2px 4px 2px 0; border-radius:5px;
  border:1px solid #bbf7d0; background:#f0fdf4; color:#14532d; }
.chip .lbl { color:var(--mut); font-size:.72rem; margin-left:6px; }
.chip.bad { background:#fef2f2; color:#7f1d1d; border-color:#fecaca; }
.pill { display:inline-block; font-size:.72rem; font-weight:600; padding:2px 10px;
  border-radius:999px; }
.pill.ok { background:#ecfdf5; color:#065f46; }
.pill.warn { background:#fffbeb; color:#92400e; }
.pill.bad { background:#fef2f2; color:#991b1b; }
.small { color:var(--mut); font-size:.85rem; }
.narr { background:#fff; border:1px solid var(--line); border-left:3px solid var(--teal);
  padding:14px 18px; border-radius:6px; line-height:1.55; white-space:pre-wrap; }
.tracewrap { line-height:2.0; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Deterministic bootstrap, cached per scenario. The app computes from the SAME
# pipeline the tests validate -- no shadow logic in the UI layer.
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def bootstrap(scenario_json: str):
    sc = Scenario(**json.loads(scenario_json))
    tables = gen.build_dataset(sc)
    outputs = rp.compute(tables)
    names = ea.canonical_entity_names(tables)
    months = sorted(outputs["variance_detail"]["month"].unique())
    index = FI.build_fact_index(outputs, tables)
    return tables, outputs, names, months, index


@st.cache_data(show_spinner=False)
def eval_results():
    import eval_scenarios as ES
    import run_eval as RE
    scenarios, tables, _ = ES.build_scenarios()
    names = ea.canonical_entity_names(tables)
    return RE.run_generation_eval(scenarios, names), RE.run_adversarial_eval(scenarios, names)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def money(x, na_str="n/a", parens=True):
    """Display-only currency formatter, accounting convention.

    Negatives render as ($793K), not -$793K and certainly not $-793K. The naive
    f"${x:,.0f}" produces the last of those -- it glues the minus between the
    currency symbol and the digits, which is the most common formatting tell in
    a finance UI. Parentheses are what a P&L, a variance pack and every audited
    statement use, so a finance reader parses them without thinking.

    parens=False falls back to -$793K for the rare context where parentheses
    read badly (inside an already-parenthesised phrase, for instance).

    Display only. This function's output never reaches the numeric audit, which
    parses narrative text formatted by narrative/prompt.py::_m. The two
    formatters are deliberately separate: the UI can adopt a house convention
    without changing what the guardrail has to understand.
    """
    if x is None or pd.isna(x):
        return na_str
    ax = abs(x)
    if ax >= 1e6:
        body = f"${ax/1e6:,.2f}M"
    elif ax >= 1e3:
        body = f"${ax/1e3:,.0f}K"
    else:
        body = f"${ax:,.0f}"
    if x >= 0:
        return body
    return f"({body})" if parens else f"-{body}"


def pct(x, na_str="n/a", parens=True):
    """Percentage formatter, same accounting convention for negatives.

    An operating margin of -30% renders as (30.0%). This is consistent with the
    dollar formatter and with how a margin line appears on a statement.
    """
    if x is None or pd.isna(x):
        return na_str
    body = f"{abs(x)*100:.1f}%"
    if x >= 0:
        return body
    return f"({body})" if parens else f"-{body}"


def month_label(m):
    import datetime as dt
    return dt.date.fromisoformat(str(m)).strftime("%B %Y")


def esc(s):
    return html.escape(str(s))


def narr_box(text):
    st.html(f"<div class='narr'>{esc(text)}</div>")


def get_key(provider):
    env = "ANTHROPIC_API_KEY" if provider == "Anthropic" else "OPENAI_API_KEY"
    if st.session_state.get(env):
        return st.session_state[env]
    try:
        if env in st.secrets:
            return st.secrets[env]
    except Exception:
        pass
    return os.environ.get(env)


def make_client(provider, model):
    key = get_key(provider)
    env = "ANTHROPIC_API_KEY" if provider == "Anthropic" else "OPENAI_API_KEY"
    if key:
        os.environ[env] = key
    return AnthropicClient(model=model) if provider == "Anthropic" else OpenAIClient(model=model)


# ---------------------------------------------------------------------------
# Scenario inputs. ONLY inputs are editable; everything derived from them
# (salaries = headcount x comp, revenue = ARR/12, all variances) is recomputed.
# ---------------------------------------------------------------------------
def scenario_sidebar() -> Scenario:
    if "scenario" not in st.session_state:
        st.session_state.scenario = Scenario()
    sc: Scenario = st.session_state.scenario

    st.markdown("<div class='eyebrow'>Scenario inputs</div>", unsafe_allow_html=True)
    n_changed = len(sc.diff_from_default())
    st.markdown(
        f"<span class='pill {'warn' if n_changed else 'ok'}'>"
        f"{n_changed} input{'' if n_changed == 1 else 's'} changed</span>"
        f"<span class='small'> · derived values recompute</span>",
        unsafe_allow_html=True)

    changes = {}
    with st.expander("Growth & retention"):
        changes["starting_arr"] = st.number_input(
            "Starting ARR ($)", value=float(sc.starting_arr), step=500_000.0, format="%.0f")
        changes["base_new_arr"] = st.number_input(
            "New ARR per month ($)", value=float(sc.base_new_arr), step=10_000.0, format="%.0f")
        changes["base_churned_arr"] = st.number_input(
            "Churned ARR per month ($)", value=float(sc.base_churned_arr),
            step=10_000.0, format="%.0f")
        changes["actual_new_arr_mult"] = st.slider(
            "FY25 bookings vs plan (x)", 0.50, 1.50, float(sc.actual_new_arr_mult), 0.01)
        changes["actual_churn_mult"] = st.slider(
            "FY25 churn vs plan (x)", 0.50, 2.50, float(sc.actual_churn_mult), 0.01)
        changes["bookings_seasonality"] = st.slider(
            "Bookings seasonality (amplitude)", 0.00, 0.40,
            float(sc.bookings_seasonality), 0.05)
        changes["churn_seasonality"] = st.slider(
            "Renewal-date clustering (amplitude)", 0.00, 0.40,
            float(sc.churn_seasonality), 0.05)
        st.caption(
            "Seasonality shapes the ARR **flows** — Q4 push, summer trough, "
            "January/July renewal dates — in plan and actual alike, so it bends "
            "the revenue curve without masquerading as variance. Revenue stays "
            "exactly ARR ÷ 12, so the bridge still closes and the volume/price "
            "decomposition still ties. At 0.00 the dataset is the baseline.")

    with st.expander("Spend stories (FY25 actuals)"):
        changes["actual_marketing_mult"] = st.slider(
            "Paid marketing (x)", 0.50, 2.50, float(sc.actual_marketing_mult), 0.05)
        changes["actual_contractors_mult"] = st.slider(
            "R&D contractors (x)", 0.50, 2.50, float(sc.actual_contractors_mult), 0.05)
        changes["actual_legal_spike_mult"] = st.slider(
            "Legal spike, Jun/Jul (x)", 1.00, 6.00, float(sc.actual_legal_spike_mult), 0.10)
        changes["actual_hosting_mult"] = st.slider(
            "Hosting cost (x)", 0.80, 2.00, float(sc.actual_hosting_mult), 0.01)
        changes["actual_cs_software_mult"] = st.slider(
            "CS software (x)", 0.50, 2.50, float(sc.actual_cs_software_mult), 0.05)

    with st.expander("Spend stories (FY24 actuals)"):
        changes["actual_fy24_recruiting_mult"] = st.slider(
            "R&D recruiting, Jul–Dec (x)", 1.00, 2.50,
            float(sc.actual_fy24_recruiting_mult), 0.05)
        changes["actual_fy24_events_mult"] = st.slider(
            "S&M events, Sep–Nov (x)", 0.40, 1.00,
            float(sc.actual_fy24_events_mult), 0.02)
        st.caption(
            "FY2024 is the comparison year: revenue tracks plan, and the "
            "variance story sits on the cost side. Recruiting runs hot in H2 as "
            "the company hires ahead of the FY2025 headcount ramp — a variance "
            "explained by another table in the same dataset. The events "
            "underspend is the one **favorable** driver. At 1.00 / 1.00 FY2024 "
            "carries no story.")

    with st.expander("People"):
        st.caption("Headcount and comp are inputs. Salary lines are computed "
                   "as headcount x comp / 12 and stay tied to the penny.")
        hc_end = dict(sc.hc_budget_end)
        comp = dict(sc.avg_comp_budget)
        c1, c2 = st.columns(2)
        for i, d in enumerate(DEPTS):
            with (c1 if i % 2 == 0 else c2):
                hc_end[d] = st.number_input(f"{d} plan HC (end)", value=int(hc_end[d]),
                                            step=1, key=f"hc_{d}")
                comp[d] = st.number_input(f"{d} avg comp ($)", value=float(comp[d]),
                                          step=5_000.0, format="%.0f", key=f"comp_{d}")
        changes["hc_budget_end"] = hc_end
        changes["avg_comp_budget"] = comp
        changes["sm_ramp_late"] = st.number_input(
            "S&M hired ahead of plan (H2)", value=int(sc.sm_ramp_late), step=1)
        changes["cs_hc_shortfall"] = st.number_input(
            "CS headcount short of plan", value=int(sc.cs_hc_shortfall), step=1)

    new_sc = sc.with_changes(**changes)
    if new_sc != sc:
        st.session_state.scenario = new_sc
        sc = new_sc

    if st.button("Reset to baseline", width='stretch'):
        st.session_state.scenario = Scenario()
        st.cache_data.clear()
        st.rerun()
    return sc


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("<div class='eyebrow'>FP&A · synthetic SaaS</div>", unsafe_allow_html=True)
    st.markdown("<div class='headline'>Variance Copilot</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)
    st.caption("Code computes every number. The model only explains.")

    page = st.radio("View", ["Overview", "Variance", "Forecast", "Commentary",
                             "Close-cycle agent",
                             "Guardrails & Eval", "ROI", "Decision log"],
                    label_visibility="collapsed")
    SC = scenario_sidebar()

TABLES, OUTPUTS, NAMES, MONTHS, INDEX = bootstrap(SC.key())

with st.sidebar:
    default_ix = MONTHS.index("2025-09-01") if "2025-09-01" in MONTHS else len(MONTHS) - 1
    sel_month = st.selectbox("Reporting month", MONTHS, index=default_ix,
                             format_func=month_label)
    st.markdown("---")
    st.caption("100% synthetic data. No real-company information.")
    a_ok, o_ok = bool(get_key("Anthropic")), bool(get_key("OpenAI"))
    st.markdown(
        f"Live model: <span class='pill {'ok' if (a_ok or o_ok) else 'warn'}'>"
        f"{'key detected' if (a_ok or o_ok) else 'not set (deterministic mode)'}</span>",
        unsafe_allow_html=True)

PACK = fp.build_fact_pack(sel_month, OUTPUTS, TABLES)


def company_monthly() -> pd.DataFrame:
    """Company actual vs budget by month, from the computed statement rollup."""
    bs = OUTPUTS["variance_by_statement_line"]
    piv = bs.pivot_table(index="month", columns="statement_line",
                         values=["actual", "budget"], aggfunc="sum").fillna(0.0)
    df = pd.DataFrame({"month": piv.index})
    for label, col in [("Revenue", "Revenue"), ("Cost of Revenue", "Cost of Revenue"),
                       ("Operating Expenses", "Operating Expenses")]:
        for scen in ("actual", "budget"):
            key = (scen, col)
            df[f"{label}_{scen}"] = (piv[key].values if key in piv.columns else 0.0)
    for scen in ("actual", "budget"):
        df[f"Operating Income_{scen}"] = (df[f"Revenue_{scen}"]
                                          - df[f"Cost of Revenue_{scen}"]
                                          - df[f"Operating Expenses_{scen}"])
    return df.sort_values("month").reset_index(drop=True)


# ===========================================================================
def page_overview():
    st.markdown("<div class='eyebrow'>Company snapshot</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='headline'>{month_label(sel_month)}</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    op = OUTPUTS["operating_metrics"]
    row = op[op["month"] == sel_month]
    saas = OUTPUTS["saas_metrics_summary"]
    srow = saas[saas["month"] == sel_month]
    line = OUTPUTS["variance_detail"]
    rv = line[(line["month"] == sel_month) & (line["is_revenue"] == 1)]
    ra, rb = rv["actual"].sum(), rv["budget"].sum()
    if row.empty:
        st.info("No computed metrics for this month.")
        return
    r = row.iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Revenue", money(ra), f"{money(ra - rb)} vs budget", delta_color="off")
    c2.metric("Gross margin", pct(r["gross_margin"]))
    c3.metric("Operating income", money(r["operating_income"]),
              pct(r["operating_margin"]) + " margin", delta_color="off")
    if not srow.empty:
        c4.metric("Ending ARR", money(srow.iloc[0]["ending_arr"]),
                  f"NRR {pct(srow.iloc[0].get('nrr_ttm'))}", delta_color="off")

    st.markdown("<br>", unsafe_allow_html=True)
    left, right = st.columns([3, 2])
    with left:
        st.markdown("<div class='eyebrow'>P&amp;L bridge to operating income</div>",
                    unsafe_allow_html=True)
        pnl = pd.DataFrame({
            "Line": ["Revenue", "Cost of revenue", "Gross profit",
                     "Operating expenses", "Operating income"],
            "Amount": [money(r["revenue"]), money(-r["cogs"]), money(r["gross_profit"]),
                       money(-r["opex"]), money(r["operating_income"])]})
        st.dataframe(pnl, hide_index=True, width='stretch')
    with right:
        st.markdown("<div class='eyebrow'>Retention (trailing 12 mo)</div>",
                    unsafe_allow_html=True)
        if not srow.empty:
            s = srow.iloc[0]
            st.metric("Net revenue retention", pct(s.get("nrr_ttm")))
            st.metric("Gross revenue retention", pct(s.get("grr_ttm")))
            st.caption("Synthetic figures, engineered to show churn pressure — "
                       "not a benchmark of a healthy book.")


# ===========================================================================
def page_variance():
    st.markdown("<div class='eyebrow'>Actual vs budget · ranked by impact on operating income</div>",
                unsafe_allow_html=True)
    st.markdown(f"<div class='headline'>Top variance drivers — {month_label(sel_month)}</div>",
                unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    drivers = dd.top_drivers(OUTPUTS["variance_detail"], sel_month, n=8)
    if drivers.empty:
        st.info("No variance rows for this month.")
        return

    st.caption("Variance is actual − budget. OI impact flips the sign for expenses, "
               "because spending more reduces operating income — so favorability is "
               "read off OI impact, not the raw variance.")

    disp = drivers.copy()
    disp["Line item"] = disp["account_name"] + " (" + disp["department_id"] + ")"
    disp["Actual"] = disp["actual"].map(money)
    disp["Budget"] = disp["budget"].map(money)
    disp["Variance (act − bud)"] = disp["var_ab_amount"].map(
        lambda v: f"{money(abs(v))} {'over' if v > 0 else 'under'}")
    disp["Impact on OI"] = disp["oi_impact_ab"].map(money)
    disp["Effect"] = disp["favorable_ab"].map(lambda b: "Favorable" if b else "Unfavorable")

    styled = (disp[["Line item", "Actual", "Budget", "Variance (act − bud)",
                    "Impact on OI", "Effect"]]
              .style.map(lambda v: ("color:#15803d;font-weight:600" if v == "Favorable"
                                    else "color:#b91c1c;font-weight:600"), subset=["Effect"]))
    st.dataframe(styled, hide_index=True, width='stretch')

    st.markdown("<div class='eyebrow'>Impact on operating income</div>", unsafe_allow_html=True)
    cdf = drivers.copy()
    cdf["label"] = cdf["account_name"] + " (" + cdf["department_id"] + ")"
    cdf["Effect"] = cdf["favorable_ab"].map(lambda b: "Favorable" if b else "Unfavorable")
    # Height is derived from the row count rather than fixed. At a fixed 300px
    # Vega thins overlapping category labels, so 8 bars rendered with only 4
    # labels -- which reads as "the chart disagrees with the table above it".
    # labelLimit stops long account names being clipped to "Services Revenue...".
    cdf["OI impact"] = cdf["oi_impact_ab"].map(money)
    chart_height = max(240, 34 * len(cdf))
    st.altair_chart(alt.Chart(cdf).mark_bar().encode(
        x=alt.X("oi_impact_ab:Q", title="OI impact ($)"),
        y=alt.Y("label:N", sort="-x", title=None,
                axis=alt.Axis(labelLimit=260, labelOverlap=False)),
        color=alt.Color("Effect:N", scale=alt.Scale(
            domain=["Favorable", "Unfavorable"], range=["#15803d", "#b91c1c"]),
            legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("label:N", title="Line item"),
                 alt.Tooltip("OI impact:N"),
                 alt.Tooltip("Effect:N")]
    ).properties(height=chart_height), width='stretch')

    with st.expander("Department rollup"):
        bd = OUTPUTS["variance_by_department"]
        bdm = bd[bd["month"] == sel_month].copy()
        for c in ["actual", "budget", "var_ab_amount"]:
            bdm[c.title()] = bdm[c].map(money)
        st.dataframe(bdm[["department_id", "Actual", "Budget", "Var_Ab_Amount"]]
                     .rename(columns={"department_id": "Department",
                                      "Var_Ab_Amount": "Variance"}),
                     hide_index=True, width='stretch')


# ===========================================================================
def page_forecast():
    st.markdown("<div class='eyebrow'>Actual vs budget over time · with a projected continuation</div>",
                unsafe_allow_html=True)
    st.markdown("<div class='headline'>Forecast</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    cm = company_monthly()
    measure = st.selectbox("Measure", ["Revenue", "Operating Expenses",
                                       "Cost of Revenue", "Operating Income"])
    acol, bcol = f"{measure}_actual", f"{measure}_budget"

    tbl = cm[["month", acol, bcol]].copy()
    tbl["Variance"] = tbl[acol] - tbl[bcol]
    show = pd.DataFrame({
        "Month": tbl["month"].map(month_label),
        "Actual": tbl[acol].map(money),
        "Budget": tbl[bcol].map(money),
        "Variance": tbl["Variance"].map(
            lambda v: f"{money(abs(v))} {'over' if v > 0 else 'under'}"),
    })
    st.dataframe(show, hide_index=True, width='stretch', height=300)

    st.markdown("<div class='eyebrow'>Projected continuation</div>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        method = st.selectbox("Method", list(PJ.METHODS.keys()),
                              format_func=lambda k: PJ.METHODS[k])
    with c2:
        horizon = st.slider("Months forward", 3, 12, 6)
    with c3:
        lookback = st.slider("Trailing months used", 3, 24, 12)

    if method == "driver_arr" and measure != "Revenue":
        st.info("The driver-based method continues the ARR bridge, so it applies to "
                "Revenue only. Pick Revenue, or use a trend method for this measure.")
        return

    long_df, meta = PJ.build_projection_frame(
        cm, acol, bcol, method, horizon, lookback,
        saas_actual=OUTPUTS["saas_metrics_summary"],
        saas_budget=None if method != "driver_arr" else None)

    base = alt.Chart(long_df).encode(
        x=alt.X("month:T", title=None),
        y=alt.Y("value:Q", title=f"{measure} ($)"),
        color=alt.Color("series:N", scale=alt.Scale(
            domain=["Actual", "Budget"], range=["#0f766e", "#94a3b8"]),
            legend=alt.Legend(orient="bottom", title=None)),
        strokeDash=alt.StrokeDash("kind:N", scale=alt.Scale(
            domain=["history", "projected"], range=[[1, 0], [5, 4]]),
            legend=alt.Legend(orient="bottom", title=None)),
        tooltip=["month:T", "series:N", "kind:N", alt.Tooltip("value:Q", format=",.0f")])
    st.altair_chart(base.mark_line(point=False).properties(height=340), width='stretch')

    bits = [f"**Method:** {meta['method']}", f"**Assumption:** {meta['assumption']}"]
    if meta["r2"] is not None:
        # Never round a goodness-of-fit UP to a perfect 1.00. R^2 = 0.9996 is not
        # 1.00, and in a tool whose whole claim is precision, printing a perfect
        # fit that isn't one is the wrong kind of rounding to be caught doing.
        r2 = meta["r2"]
        r2_txt = "0.999+" if 0.999 <= r2 < 1 else f"{r2:.3f}"
        bits.append(f"**Fit (R²):** {r2_txt} on {meta['fitted_on']} months")
    st.markdown(" · ".join(bits))
    st.caption("The projection is computed in code from the actuals; the method is an "
               "explicit choice, not a hidden assumption. A trend is a baseline, not a "
               "plan — it ignores seasonality, step changes, and pipeline.")


# ===========================================================================
def render_trace(text, pack):
    res = na.audit(text, pack)
    ent = ea.audit_entities(text, pack, NAMES)
    chips = []
    for mention, value, label in res.matched:
        chips.append(f"<span class='chip'>{esc(mention)}"
                     f"<span class='lbl'>{esc(label)}</span></span>")
    for v in res.violations:
        chips.append(f"<span class='chip bad'>{esc(v.mention)} · unverified</span>")
    for e in ent.out_of_scope:
        chips.append(f"<span class='chip bad'>{esc(e)} · out of scope</span>")
    body = " ".join(chips) if chips else "<span class='small'>No figures.</span>"
    st.html(f"<div class='tracewrap'>{body}</div>")
    return res, ent


def page_commentary():
    st.markdown("<div class='eyebrow'>Guarded narrative · numbers injected, never invented</div>",
                unsafe_allow_html=True)
    st.markdown(f"<div class='headline'>Variance commentary — {month_label(sel_month)}</div>",
                unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    if PACK.status != "ok":
        st.warning("Insufficient computed data for this month — the tool refuses "
                   "rather than guess.")
        return

    st.markdown("<div class='eyebrow'>Deterministic narrative (no API key)</div>",
                unsafe_allow_html=True)
    det = P.injection_narrative(PACK)
    narr_box(det)
    st.markdown("<div class='small'>Every figure is tied to a computed value. "
                "Green = verified against the computed set.</div>", unsafe_allow_html=True)
    render_trace(det, PACK)

    st.markdown("---")
    st.markdown("<div class='eyebrow'>Live model narrative (optional)</div>",
                unsafe_allow_html=True)
    provider = st.radio("Provider", ["Anthropic", "OpenAI"], horizontal=True,
                        label_visibility="collapsed")
    key = get_key(provider)
    default_model = "claude-sonnet-5" if provider == "Anthropic" else "gpt-4.1"

    if not key:
        with st.expander("Add a key to generate live (kept only in this session)"):
            entered = st.text_input(f"{provider} API key", type="password",
                                    key=f"in_{provider}")
            if entered:
                env = "ANTHROPIC_API_KEY" if provider == "Anthropic" else "OPENAI_API_KEY"
                st.session_state[env] = entered
                os.environ[env] = entered
                st.rerun()
        st.info("No key set. The deterministic narrative above needs no key, and the "
                "Guardrails & Eval page proves the guarantee without one.")
        return

    model = st.text_input("Model", value=default_model)
    if st.button("Generate with live model", type="primary"):
        with st.spinner(f"Generating with {model} and auditing…"):
            try:
                res = G.generate_commentary(PACK, make_client(provider, model), NAMES,
                                            mode="audit", max_retries=1)
            except Exception as e:
                st.error(f"Live generation failed: {e}")
                return
        st.session_state["live_res"] = {
            "status": res.status, "source": res.source, "attempts": res.attempts,
            "caught": sum(res.violations_caught), "text": res.text}

    live = st.session_state.get("live_res")
    if live:
        pill = {"accepted": "ok", "fallback": "warn", "refused": "bad"}.get(live["status"], "warn")
        c = st.columns(3)
        c[0].markdown(f"Status <span class='pill {pill}'>{live['status']}</span> "
                      f"<span class='small'>({live['source']})</span>", unsafe_allow_html=True)
        c[1].markdown(f"Attempts <b>{live['attempts']}</b>", unsafe_allow_html=True)
        c[2].markdown(f"Fabrications caught before output <b>{live['caught']}</b>",
                      unsafe_allow_html=True)
        narr_box(live["text"])
        st.markdown("<div class='small'>Independent re-audit of the returned text:</div>",
                    unsafe_allow_html=True)
        ar, _ = render_trace(live["text"], PACK)
        if ar.passed:
            st.success(f"Re-audit passed — {len(ar.matched)} figures matched, 0 fabricated.")
        else:
            st.error("Re-audit found unverified figures — this text would not be surfaced.")

    # ---- analyst chat ----
    st.markdown("---")
    st.markdown("<div class='eyebrow'>Ask the data · guarded analyst chat</div>",
                unsafe_allow_html=True)
    st.caption("Questions are answered from the computed dataset — all 24 months, every "
               "department and account. Code selects which computed facts to send; the "
               "model never calculates. Every answer is audited, and it declines rather "
               "than estimate.")

    if not key:
        st.info("Add an API key above to use the chat.")
        return

    if "chat" not in st.session_state:
        st.session_state.chat = []
    for turn in st.session_state.chat:
        with st.chat_message(turn["role"]):
            st.write(turn["content"])
            if turn.get("trace"):
                st.html(f"<div class='tracewrap'>{turn['trace']}</div>")
            if turn.get("meta"):
                st.caption(turn["meta"])

    q = st.chat_input("Ask about any month, department, or line item…")
    if q:
        st.session_state.chat.append({"role": "user", "content": q})
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            with st.spinner("Selecting computed facts and auditing the answer…"):
                try:
                    ans = CH.answer_question(
                        q, INDEX, sel_month, make_client(provider, model), NAMES,
                        history=[t for t in st.session_state.chat if t["role"] in ("user", "assistant")])
                except Exception as e:
                    st.error(f"Chat failed: {e}")
                    return
            st.write(ans.text)
            chips = "".join(
                f"<span class='chip'>{esc(m)}<span class='lbl'>{esc(l)}</span></span>"
                for m, _, l in ans.matched)
            if chips:
                st.html(f"<div class='tracewrap'>{chips}</div>")
            meta = (f"{ans.facts_sent} computed facts sent · scope: {ans.scope_note} · "
                    f"attempts {ans.attempts} · fabrications caught "
                    f"{sum(ans.violations_caught)}")
            st.caption(meta)
        st.session_state.chat.append({"role": "assistant", "content": ans.text,
                                      "trace": chips, "meta": meta})


# ===========================================================================
def page_eval():
    st.markdown("<div class='eyebrow'>The proof · reproducible, offline</div>",
                unsafe_allow_html=True)
    st.markdown("<div class='headline'>Guardrails &amp; evaluation</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    with st.expander("What this page proves", expanded=False):
        st.markdown(
            "**One claim: a number the model invents can never reach the reader.** "
            "It is proven two ways.\n\n"
            "**A · End-to-end.** The tool is run several times while the model is "
            "deliberately made to misbehave on some runs — forced to fabricate a "
            "figure. *Caught pre-output* counts fabrications the audit intercepted; "
            "*Fabricated in output* counts any that survived to the reader. That last "
            "column is **0 on every row**: when the model slips, the audit catches it "
            "and either makes it retry or substitutes the deterministic version.\n\n"
            "**B · Adversarial.** The audit's report card. It is handed sentences with "
            "known planted fakes and must flag every one (100%) while passing clean "
            "controls. A pipeline recovers; a detector catches — you need both.\n\n"
            "The box at the bottom is that same detector, live, for you to poke at.")

    with st.spinner("Running the eval…"):
        gen_df, adv_df = eval_results()

    n_gen = int((gen_df["behaviour"] != "n/a").sum())
    fabricated = int(gen_df["fabricated_in_output"].sum())
    caught = int(gen_df["violations_caught_pre"].sum())
    refusals = int((gen_df["status"] == "refused").sum())
    adv_catch = adv_df[adv_df["expected_caught"]]
    catch_rate = adv_catch["actually_caught"].mean() if len(adv_catch) else 1.0

    c1, c2, c3 = st.columns(3)
    c1.metric("Fabricated numbers in output", fabricated,
              f"across {n_gen} generations", delta_color="off")
    c2.metric("Adversarial catch rate", f"{catch_rate*100:.0f}%",
              f"{len(adv_catch)} planted cases", delta_color="off")
    c3.metric("Model slips caught before output", caught,
              f"{refusals} refusal on missing data", delta_color="off")
    st.caption("The eval runs against the baseline dataset, so these figures are stable "
               "regardless of the scenario inputs you set in the sidebar.")

    st.markdown("<div class='eyebrow'>A · End-to-end under a misbehaving model</div>",
                unsafe_allow_html=True)
    st.dataframe(gen_df.rename(columns={
        "scenario": "Scenario", "behaviour": "Model behaviour", "status": "Outcome",
        "source": "Source", "attempts": "Attempts",
        "violations_caught_pre": "Caught pre-output",
        "fabricated_in_output": "Fabricated in output"}).drop(columns=["audit_passed"]),
        hide_index=True, width='stretch')

    st.markdown("<div class='eyebrow'>B · Adversarial audit</div>", unsafe_allow_html=True)
    adv_show = adv_df.copy()
    adv_show["OK"] = adv_show["correct"].map(lambda c: "✓" if c else "✗")
    st.dataframe(adv_show.rename(columns={
        "scenario": "Scenario", "case": "Case", "check": "Check",
        "expected_caught": "Should catch", "actually_caught": "Caught"})[
        ["Scenario", "Case", "Check", "Should catch", "Caught", "OK"]],
        hide_index=True, width='stretch')

    st.markdown("---")
    st.markdown("<div class='eyebrow'>Try it · audit any commentary</div>", unsafe_allow_html=True)
    st.caption("Edit the text — change or invent a number — and watch the audit verify "
               "each figure against the computed set for the selected month.")
    seed = P.injection_narrative(PACK) if PACK.status == "ok" else ""
    txt = st.text_area("Commentary to audit", value=seed, height=140)
    if st.button("Run audit"):
        if PACK.status != "ok":
            st.warning("Selected month has no computed facts to audit against.")
        else:
            res, ent = render_trace(txt, PACK)
            if res.passed and ent.passed:
                st.success(f"Passed — {len(res.matched)} figures verified, 0 fabricated.")
            else:
                st.error(f"{res.n_fabricated} unverified figure(s), "
                         f"{len(ent.out_of_scope)} out-of-scope entity(ies) flagged.")


# ===========================================================================
def page_roi():
    import roi as ROI

    st.markdown("<div class='eyebrow'>What it's worth &middot; measured where "
                "measurable, assumed where not</div>", unsafe_allow_html=True)
    st.markdown("<div class='headline'>Quantified ROI</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)

    st.markdown(
        "This tool's whole claim is that no decision-facing number is asserted "
        "without being computed and checked. An unsourced ROI headline would be "
        "the one fabricated figure in a project whose headline is *zero "
        "fabricated figures* &mdash; so the ROI is built under the same "
        "discipline as the commentary.")

    m = ROI.measure(OUTPUTS, TABLES)

    st.markdown("<div class='eyebrow'>Measured &middot; instrumented from this "
                "run</div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Figures verified", f"{m.figures_per_commentary:.0f}",
              "per commentary", delta_color="off")
    c2.metric("Machine time", f"{m.seconds_per_commentary*1000:.0f} ms",
              "per commentary", delta_color="off")
    c3.metric("Months measured", f"{m.months_measured}",
              f"{m.variance_rows_computed:,} variance rows", delta_color="off")
    c4.metric("Computed values", f"{m.allowed_values_per_fact_pack}",
              "available per fact pack", delta_color="off")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='eyebrow'>Assumed &middot; the manual baseline</div>",
                unsafe_allow_html=True)
    st.markdown(
        "These are **not measured**, and saying so is the point. Measuring them "
        "would mean timing real analysts on real close cycles at a real "
        "employer &mdash; which this project deliberately has no access to. "
        "Move them and the answer moves.")

    b1, b2 = st.columns(2)
    with b1:
        draft = st.slider("Drafting the narrative by hand (min)", 20, 180, 75, 5)
        tie = st.slider("Tie-out per figure (min)", 0.25, 4.0, 1.5, 0.25)
        cycles = st.slider("Review cycles", 0.0, 4.0, 2.0, 0.5)
    with b2:
        per_cycle = st.slider("Minutes per review cycle", 5, 45, 20, 5)
        rate = st.slider("Loaded cost per hour ($)", 40, 160, 85, 5)
        scope = st.slider("Commentaries per close", 1, 20, 1, 1)

    base = ROI.Baseline(drafting_minutes=draft, tie_out_minutes_per_figure=tie,
                        review_cycles=cycles, minutes_per_review_cycle=per_cycle,
                        loaded_cost_per_hour=rate, commentaries_per_close=scope)
    r = ROI.compute_roi(m, base)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='eyebrow'>Derived</div>", unsafe_allow_html=True)
    d1, d2, d3 = st.columns(3)
    d1.metric("By hand", f"{r.baseline_minutes:.0f} min", "per commentary",
              delta_color="off")
    d2.metric("With the tool", f"{r.tool_minutes:.0f} min",
              f"{r.pct_reduction*100:.0f}% reduction", delta_color="off")
    d3.metric("Redeployed", f"{r.annual_hours(base):.0f} hrs/yr",
              f"≈ {money(r.annual_dollars(base))} at ${rate}/hr", delta_color="off")

    left, right = st.columns(2)
    with left:
        st.markdown("<div class='eyebrow'>Where the time goes &mdash; by hand</div>",
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(
            {"Step": list(r.baseline_breakdown),
             "Minutes": [f"{v:.0f}" for v in r.baseline_breakdown.values()]}),
            hide_index=True, width='stretch')
    with right:
        st.markdown("<div class='eyebrow'>&mdash; with the tool</div>",
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(
            {"Step": list(r.tool_breakdown),
             "Minutes": [f"{v:.1f}" for v in r.tool_breakdown.values()]}),
            hide_index=True, width='stretch')

    st.caption(
        "The tie-out line is the core of the claim, and it is the only one with "
        "an honest shape: **measured** figure count × **assumed** rate per "
        "figure. It scales with something the tool actually produces rather "
        "than with a guess.")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='eyebrow'>Sensitivity &middot; the point estimate is "
                "not the deliverable</div>", unsafe_allow_html=True)
    sens = pd.DataFrame(ROI.sensitivity(m))
    sens.columns = ["Scenario", "By hand (min)", "With tool (min)", "Saved (min)",
                    "Reduction %", "Hours/yr", "$/yr"]
    st.dataframe(sens, hide_index=True, width='stretch')
    st.caption(
        "Under a skeptic's assumptions — fast analyst, light process — the "
        "saving is modest. That is deliberate. A model that wins big under "
        "every assumption is not a model.")

    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("What this deliberately does NOT claim"):
        st.markdown(
            "- **Not the analysis.** Deciding whether a salary variance is a "
            "hiring-timing problem or a forecasting problem is judgment, and "
            "the tool does not do it.\n"
            "- **Not the close.** Getting to a trial balance is upstream of "
            "anything here.\n"
            "- **Not headcount.** The saving is redeployed analyst hours, "
            "which is a different and more honest claim — and the one a "
            "finance leader will actually believe.\n"
            "- **Not annualised from one good month.** The unit is one "
            "commentary, scaled explicitly by a cadence you set above.")


# ===========================================================================
def page_decisions():
    st.markdown("<div class='eyebrow'>Why each choice was made</div>", unsafe_allow_html=True)
    st.markdown("<div class='headline'>Decision log</div>", unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)
    path = os.path.join(REPO, "decision_log.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            body = f.read()
        body = body.replace("$", "\\$")
        body = body.split("\n", 1)[1] if body.startswith("# ") else body
        st.markdown(body)
    else:
        st.info("decision_log.md not found.")


class _CachedCandidate:
    """Adapts a saved narrative to the shape the deck builder expects."""

    def __init__(self, narr: dict):
        n = narr or {}
        self.text = n.get("text", "")
        self.source = n.get("source", "")
        self.attempts = n.get("attempts", 0)
        self.matched = [tuple(m) for m in (n.get("matched") or [])]
        self.violations = list(n.get("violations") or [])
        self.entity_flags = list(n.get("entity_flags") or [])
        self.deterministic_by_choice = n.get("deterministic_by_choice", False)
        self.audit_ran = n.get("audit_ran", False)
        self.audit_passed = n.get("audit_passed", False)
        self.reason = n.get("reason", "")
        self.status = n.get("status", "")

    @property
    def publishable(self) -> bool:
        return self.audit_ran and self.audit_passed and bool(self.text)


def _agent_imports():
    """Imported lazily so the other pages carry no agent import cost."""
    from agent import materialize as agent_mz
    from agent import replay as agent_replay
    from agent import tools as _agent_tools  # noqa: F401  (registers the tools)
    from agent.gates import PublicationPacket, blocking_reasons, review_plan
    from agent.narrate import narrate as agent_narrate
    from agent.orchestrator import Orchestrator
    from agent.packages import build_goal, variance_package_plan
    from agent.planner import Planner, PlannerError, make_client as agent_client
    from agent.run_package import render as render_package
    return dict(mz=agent_mz, replay=agent_replay, Orchestrator=Orchestrator,
                build_goal=build_goal, variance_package_plan=variance_package_plan,
                narrate=agent_narrate, render_package=render_package,
                PublicationPacket=PublicationPacket, review_plan=review_plan,
                blocking_reasons=blocking_reasons, Planner=Planner,
                PlannerError=PlannerError, agent_client=agent_client)


def _section(title: str, why: str):
    """A titled section with one line on what it is and why it matters.

    Rendered in near-black rather than the muted grey used elsewhere in the
    app: this copy is the explanation a first-time reader depends on, and grey
    body text at this size is genuinely hard to read on a projector.
    """
    st.markdown(
        f"<div style='font-size:1.05rem;font-weight:700;color:#0F172A;"
        f"letter-spacing:-0.01em;margin:2px 0 4px'>{esc(title)}</div>",
        unsafe_allow_html=True)
    st.markdown(
        f"<div style='color:#0F172A;font-size:0.92rem;line-height:1.5;"
        f"margin-bottom:6px'>{why}</div>", unsafe_allow_html=True)


def _plan_table(summary):
    """Steps in EXECUTION order.

    The first version let the dataframe sort by whatever column the user last
    clicked, and it defaulted to section name -- so the plan rendered 3, 2, 4,
    1, 5. Sequence is the entire content of a plan; a plan you cannot read in
    order is not a plan.
    """
    rows = []
    for s in sorted(summary, key=lambda x: x["step"]):
        args = ",  ".join(f"{k}={v}" for k, v in s["params"].items())
        rows.append({"Step": s["step"], "Tool": s["tool"],
                     "Arguments": args,
                     "Produces": s["purpose"] or "—",
                     "Bound at run time":
                         ", ".join(s["resolved_at_runtime"]) or "—",
                     "Optional": "yes" if s["optional"] else ""})
    return pd.DataFrame(rows)


def _render_briefing(result, goal, key: str):
    """The 'where to look' block: computed prioritisation, no recommendation."""
    from agent.briefing import build_briefing, fmt

    b = build_briefing(result, goal)
    _section("Where to look",
             "Ranked by impact on operating income, computed in SQL — not "
             "chosen by the model. Every figure is a tool return value. This "
             "shows <b>where the money moved and what is underneath it</b>; it "
             "does not recommend an action, because a recommendation is not a "
             "number and could not be verified.")

    if not b.available:
        st.info(b.note or "This run did not rank drivers, so there is nothing "
                          "to prioritise.")
        return

    for a in b.areas:
        colour = "#B91C1C" if a.direction == "unfavorable" else "#15803D"
        share = (f" · {fmt(a.share, 'percent')} of total impact"
                 if a.share is not None else "")
        st.markdown(
            f"<div style='margin-top:14px'><b>{a.rank}. {esc(a.name)}</b> "
            f"<span style='color:{colour};font-weight:600'>"
            f"{esc(fmt(a.oi_impact, 'money'))} {a.direction}</span>"
            f"<span class='small'>{esc(share)}</span></div>",
            unsafe_allow_html=True)

        if a.detail:
            st.dataframe(pd.DataFrame([
                {"Account": e.label,
                 "Impact on operating income": fmt(e.value, e.kind)}
                for e in a.detail]), hide_index=True, width="stretch")
        cols = []
        if a.revenue_split:
            cols.append(("Revenue split (operating-income basis)",
                         a.revenue_split))
        if a.comp:
            cols.append(("Compensation (expense basis · + = spent above plan)",
                         a.comp))
        if a.headcount:
            cols.append(("Headcount", a.headcount))
        if cols:
            cc = st.columns(len(cols))
            for col, (label, items) in zip(cc, cols):
                col.markdown(f"<div class='small'><b>{esc(label)}</b></div>",
                             unsafe_allow_html=True)
                for e in items:
                    col.markdown(
                        f"<div class='small'>{esc(e.label)} &nbsp; "
                        f"<b>{esc(fmt(e.value, e.kind))}</b></div>",
                        unsafe_allow_html=True)
    st.caption("Bases are labelled because they differ: account detail is on an "
               "operating-income basis (negative is unfavourable) while "
               "compensation variance is on an expense basis (positive means "
               "spent above plan).")


def _ledger_table(ledger):
    rows = []
    for e in ledger.entries:
        src = ""
        for pname, v in (e.params_declared or {}).items():
            if isinstance(v, str) and v.startswith("$STEP_"):
                src = f"{v} → {e.params_resolved.get(pname)}"
        rows.append({"Step": e.step_idx, "Tool": e.tool,
                     "Outcome": e.outcome, "Rows": e.row_count,
                     "Ms": round(e.latency_ms, 1),
                     "Resolved from": src or "—",
                     "Result hash": e.result_hash or "—"})
    return pd.DataFrame(rows)


def _metric_row(items):
    """Lay out metric cards so a figure can never be truncated.

    Streamlit truncates a metric value that does not fit its column with an
    ellipsis -- silently, with no wrap and no error. That produced
    "$28,501,6\u2026" on a five-column ARR row: a cut-off number in a tool
    whose entire claim is that its figures are exact and traceable.

    The constraint is columns, not characters. A four-column row holds an
    eleven-character value comfortably and a five-column row does not, so the
    width is chosen from the LONGEST value in the row and the row is wrapped
    rather than squeezed. Abbreviating to $28.5M was the alternative and was
    rejected: it would have hidden the truncation rather than fixed it, in the
    one place precision is the point.

    `items` is a sequence of (label, value) pairs.
    """
    items = [(lab, val) for lab, val in items if val is not None]
    if not items:
        return
    longest = max(len(str(v)) for _lab, v in items)
    per_row = 4 if longest <= 12 else (3 if longest <= 16 else 2)
    per_row = min(per_row, len(items))

    for start in range(0, len(items), per_row):
        chunk = items[start:start + per_row]
        cols = st.columns(per_row)
        for col, (lab, val) in zip(cols, chunk):
            col.metric(lab, val)


def _render_package_visual(res):
    """Render the package as metrics and tables rather than a monospace dump.

    The CLI's text block was the right call while debugging and the wrong one
    for a page an interviewer looks at: it made a finished tool read like a
    terminal. Sections are keyed on the TOOL that produced them, so an
    agent-authored plan renders as fully as the deterministic one.
    """
    def by_tool(*tools):
        for name, sec in sorted(res.sections.items(), key=lambda kv: kv[1]["step"]):
            if sec["tool"] in tools and sec["rows"]:
                return sec
        return None

    def money(v):
        if v is None:
            return "—"
        return f"(${abs(v):,.0f})" if v < 0 else f"${v:,.0f}"

    head = by_tool("get_operating_metrics")
    if head:
        r = head["rows"][0]
        _metric_row([
            ("Revenue", money(r.get("revenue"))),
            ("Operating income", money(r.get("operating_income"))),
            ("Headcount", f"{int(r.get('total_headcount', 0)):,}"),
            ("Ending ARR", money(r["ending_arr"])
             if r.get("ending_arr") is not None else None),
        ])

    pl = by_tool("get_pl_summary")
    if pl:
        st.markdown("<div class='eyebrow'>Profit &amp; loss versus plan</div>",
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "Statement line": x["statement_line"],
            "Actual": money(x["actual"]), "Budget": money(x["base"]),
            "Impact on operating income": money(x["oi_impact"]),
            "Direction": "Favorable" if x["favorable"] else "Unfavorable",
        } for x in pl["rows"]]), hide_index=True, width="stretch")

    for name, sec in sorted(res.sections.items(), key=lambda kv: kv[1]["step"]):
        rows, tool, params = sec["rows"], sec["tool"], sec.get("params", {})
        if not rows:
            continue
        if tool == "rank_variance_drivers":
            dim = params.get("dimension", "")
            st.markdown(f"<div class='eyebrow'>Top drivers by operating-income "
                        f"impact — {esc(dim)}</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Rank": x["rank"], "Driver": x["name"],
                "Impact on operating income": money(x["oi_impact"]),
                "Share of total": (f"{x['share_of_total_oi_impact']:.1%}"
                                   if x.get("share_of_total_oi_impact") is not None
                                   else "—"),
                "Direction": "Favorable" if x["favorable"] else "Unfavorable",
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "decompose_variance":
            dept = rows[0].get("department_name") or params.get("department_id", "")
            st.markdown(f"<div class='eyebrow'>Decomposition — {esc(dept)}</div>",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Rank": x["rank"], "Account": x["account_name"],
                "Actual": money(x["actual"]),
                "Impact on operating income": money(x["oi_impact"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_comp_decomposition":
            st.markdown("<div class='eyebrow'>Compensation — headcount versus "
                        "rate</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Department": x.get("department_name"),
                "Salary variance": money(x["salary_variance"]),
                "Headcount effect": money(x["hc_impact"]),
                "Rate effect": money(x["rate_impact"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_revenue_decomposition":
            x = rows[0]
            st.markdown("<div class='eyebrow'>Revenue — volume versus price</div>",
                        unsafe_allow_html=True)
            _metric_row([
                ("Revenue variance", money(x["rev_variance"])),
                ("Volume effect", money(x["volume_impact"])),
                ("Price effect", money(x["price_impact"])),
            ])
        elif tool == "get_arr_bridge":
            x = rows[0]
            st.markdown("<div class='eyebrow'>ARR movement</div>",
                        unsafe_allow_html=True)
            # Balances and flows are laid out separately: they differ by two
            # orders of magnitude, so a row wide enough for $477,228 is far
            # too narrow for $28,501,685.
            _metric_row([("Starting ARR", money(x["starting_arr"])
                          if x.get("starting_arr") is not None else None),
                         ("Ending ARR", money(x["ending_arr"])
                          if x.get("ending_arr") is not None else None)])
            _metric_row([(lab, money(x[k]) if x.get(k) is not None else None)
                         for k, lab in [("new_arr", "New"),
                                        ("expansion_arr", "Expansion"),
                                        ("contraction_arr", "Contraction"),
                                        ("churned_arr", "Churn")]])
        elif tool == "compare_periods":
            st.markdown(f"<div class='eyebrow'>Change versus "
                        f"{esc(str(params.get('period_b',''))[:7])} — "
                        f"{esc(params.get('dimension',''))}</div>",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Rank": x["rank"], "Item": x["name"],
                "This period": money(x["actual_a"]),
                "Compared period": money(x["actual_b"]),
                "Change": money(x["change"]),
                "Impact on operating income": money(x["oi_impact_of_change"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "rank_mom_movers":
            st.markdown("<div class='eyebrow'>Largest movers since last "
                        "month</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Rank": x["rank"], "Item": x["name"],
                "Actual": money(x["actual"]),
                "Change": money(x["change_vs_prior_month"]),
                "Impact on operating income": money(x["oi_impact_of_change"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "rank_persistent_drivers":
            st.markdown("<div class='eyebrow'>Persistent drivers — how often, "
                        "not how big</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Rank": x["rank"], "Item": x["name"],
                "Months unfavorable": f"{x['months_unfavorable']} of "
                                      f"{x['months_observed']}",
                "Cumulative impact": money(x["cumulative_oi_impact"]),
                "Worst month": money(x["worst_month_oi_impact"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_ytd_summary":
            st.markdown("<div class='eyebrow'>Year to date versus plan</div>",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Statement line": x["statement_line"],
                "Actual YTD": money(x["actual_ytd"]),
                "Budget YTD": money(x["budget_ytd"]),
                "Impact on operating income": money(x["oi_impact_ytd"]),
                "Direction": "Favorable" if x["favorable"] else "Unfavorable",
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_account_trend":
            st.markdown(f"<div class='eyebrow'>"
                        f"{esc(str(rows[0].get('account_name','')))} — "
                        f"month by month</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Month": str(x["month"])[:7], "Actual": money(x["actual"]),
                "Budget": money(x["budget"]),
                "Impact on operating income": money(x["oi_impact"]),
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_opex_ratio_trend":
            st.markdown(f"<div class='eyebrow'>"
                        f"{esc(str(rows[0].get('department_name','')))} opex as "
                        f"a share of revenue</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Month": str(x["month"])[:7],
                "Department opex": money(x["dept_opex"]),
                "Company revenue": money(x["revenue"]),
                "Opex % of revenue": f"{x['dept_opex_pct_revenue']:.1%}",
            } for x in rows]), hide_index=True, width="stretch")
        elif tool == "get_headcount_movement":
            st.markdown("<div class='eyebrow'>Headcount versus plan</div>",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{
                "Department": x.get("department_name"),
                "Actual": x["actual_headcount"], "Plan": x["budget_headcount"],
                "Versus plan": f"{x['hc_var_vs_budget']:+d}",
            } for x in rows]), hide_index=True, width="stretch")

    if res.missing_sections:
        st.warning("Promised sections not produced: "
                   + ", ".join(res.missing_sections))


def _deliverables(res, goal, candidate, packet, key: str):
    """Every artifact a run can produce, in one place.

    The deck sat in one section and the Word documents in another, so a reader
    had to discover that the tool produced four different things. They are one
    idea -- what this run can hand you -- and they belong under one heading,
    each stating what it produces BEFORE the button is pressed.
    """
    from agent.reports import (build_flash, build_memo, build_packet,
                               department_ids)

    DOCX = ("application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document")
    PPTX = ("application/vnd.openxmlformats-officedocument."
            "presentationml.presentation")
    period = str(goal.get("period", ""))[:7]

    st.markdown(
        "<div style='font-size:1.2rem;font-weight:700;color:#0F172A;"
        "letter-spacing:-0.01em;margin:2px 0 4px'>Deliverables</div>",
        unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#0F172A;font-size:0.92rem;line-height:1.5;"
        "margin-bottom:6px'>Every figure in every artifact below is a value "
        "computed in SQL and verified against the run ledger. The generators "
        "cannot compute a number \u2014 there is exactly one way a figure "
        "reaches a page, and it is by having been retrieved.</div>",
        unsafe_allow_html=True)

    def block(title, what, button, state, builder, mime):
        st.markdown(f"<div style='color:#0F172A;font-weight:700;font-size:"
                    f"1.02rem;margin-top:16px'>{esc(title)}</div>",
                    unsafe_allow_html=True)
        st.markdown(f"<div style='color:#0F172A;font-size:0.9rem;"
                    f"line-height:1.45;margin-bottom:6px'>{what}</div>",
                    unsafe_allow_html=True)
        if st.button(button, key=f"{key}_{state}_go"):
            with st.spinner("Building\u2026"):
                st.session_state[f"{key}_{state}"] = builder()
        made = st.session_state.get(f"{key}_{state}")
        if not made:
            return
        for i, (fname, data, figures) in enumerate(made):
            c1, c2 = st.columns([2, 3])
            c1.download_button(fname, data=data, file_name=fname, mime=mime,
                               key=f"{key}_{state}_dl_{i}")
            c2.markdown(f"<div class='small' style='padding-top:8px'>"
                        f"{figures} traced figures</div>",
                        unsafe_allow_html=True)

    def _deck():
        from agent.deck import deck_bytes
        data, prov = deck_bytes(res, goal, candidate, packet)
        return [(f"variance-review-{period}.pptx", data, len(prov))]

    block("Board deck (PowerPoint)",
          "Eleven slides: executive summary, where to look, the driver "
          "ranking, account detail, compensation, revenue split, ARR and "
          "headcount \u2014 plus an appendix carrying the full run ledger. "
          "Charts are native PowerPoint objects rather than images, so a "
          "reader can click a bar and see the numbers behind it.",
          "Build the deck", "deck", _deck, PPTX)

    def _flash():
        r = build_flash(res, goal, candidate, packet)
        return [(f"flash-{period}.docx", r.to_bytes(), len(r.provenance))]

    block("Flash results (Word)",
          "Half a page for the CFO on day three or four, before the full "
          "close package exists: headline results, performance against plan, "
          "and the single largest driver. Deliberately short \u2014 the "
          "reader is deciding in thirty seconds whether anything needs "
          "attention today.",
          "Build the flash", "flash", _flash, DOCX)

    def _memo():
        r = build_memo(res, goal, candidate, packet)
        return [(f"memo-{period}.docx", r.to_bytes(), len(r.provenance))]

    block("Monthly variance commentary (Word)",
          "The full memo: results against plan, where the variance came from, "
          "the compensation and revenue decompositions, headcount, and the "
          "audited commentary. Ends with an <b>intentionally blank</b> "
          "assessment section \u2014 the tool assembles every fact and you "
          "supply the judgment.",
          "Build the memo", "memo", _memo, DOCX)

    depts = department_ids(res)

    def _packets():
        out = []
        for dept in depts:
            r = build_packet(res, goal, dept, candidate, packet)
            out.append((f"packet-{period}-{dept}.docx", r.to_bytes(),
                        len(r.provenance)))
        return out

    block(f"Budget owner packets (Word \u2014 {len(depts)} documents)",
          "One document per department, each containing <b>only that owner's "
          "numbers</b>: their account detail, their headcount against plan, "
          "space for their explanation and a sign-off block. Writing these by "
          "hand every month is the highest-volume, lowest-judgment task in "
          "the close \u2014 this is the one the automation actually removes.",
          f"Build all {len(depts)} packets", "packets", _packets, DOCX)


def _render_narrative_block(narr: dict, key_prefix: str):
    """Narrative + audit trace, from either a live candidate or a cached run."""
    text = narr.get("text") or ""
    if not text:
        st.warning("No commentary was produced. Nothing is published rather than "
                   "risk an unsupported statement.")
        if narr.get("reason"):
            st.caption(narr["reason"])
        return

    narr_box(text)

    src = narr.get("source")
    if src == "model":
        label = "model-written, every figure verified against the run ledger"
    elif narr.get("deterministic_by_choice"):
        label = "deterministic narrative — no model was called"
    else:
        label = "deterministic fallback — the model's drafts failed the audit"
    st.caption(f"{label} · {len(narr.get('matched') or [])} figure(s) verified"
               + (f" · {narr['attempts']} model attempt(s)" if narr.get("attempts") else ""))

    matched = narr.get("matched") or []
    if matched:
        st.markdown("<div class='eyebrow'>Audit trace</div>", unsafe_allow_html=True)
        st.markdown("<div class='small'>Every figure in the prose, tied to the "
                    "computed value behind it. Nothing else was permitted.</div>",
                    unsafe_allow_html=True)
        chips = [f"<span class='chip'>{esc(m[0])}"
                 f"<span class='lbl'>{esc(m[2])}</span></span>" for m in matched]
        st.html(f"<div class='tracewrap'>{' '.join(chips)}</div>")

    for v in (narr.get("violations") or []):
        st.error(f"rejected: {v['mention']} — {v['reason']}")


def _gate1_panel(packet, blocking, state_key: str):
    """The human sign-off. Nothing is published without it.

    Reworded from "Gate 1 · pre-publication approval" because that is internal
    vocabulary: a reader outside the project cannot tell what a gate is, or
    what they are being asked to do. The instruction is now the heading.
    """
    _section("Sign off before this is used",
             "The figures are already verified against the data. What a human "
             "confirms here is that the <b>reading</b> is right — a conclusion "
             "can be wrong even when every number in it is correct. Nothing is "
             "published, and no deck can be produced, until someone signs.")

    if not packet.audit_ok:
        st.error("**Blocked — a figure failed verification.** This cannot be "
                 "signed off. Sign-off is not an override: a number that does "
                 "not trace back to the data must never be publishable by "
                 "anyone.")
        for r in blocking:
            st.markdown(f"<div class='small'>· {esc(r)}</div>", unsafe_allow_html=True)
        return

    if packet.published:
        a = packet.approval
        st.success(f"**Signed off** by {a.approver or 'reviewer'} — "
                   f"this package is cleared for use.")
        st.caption(f"Recorded against this exact package (`{a.artifact_hash}`). "
                   f"If the figures or the commentary change, the sign-off no "
                   f"longer applies and it must be reviewed again.")
        if a.note:
            st.caption(a.note)
        return

    if packet.approval.is_approval and not packet.approval_is_current:
        st.warning("This changed after it was signed off, so the earlier "
                   "sign-off no longer applies. Please review it again.")

    approver = st.text_input("Your name", value="", placeholder="e.g. R. Galasso",
                             key=f"{state_key}_who")
    note = st.text_input("Note (optional)", value="", key=f"{state_key}_note")
    c1, c2, c3 = st.columns(3)
    if c1.button("Sign off", type="primary", key=f"{state_key}_ok",
                 disabled=not approver):
        packet.approve(approver, note)
        st.rerun()
    if c2.button("Send back for revision", key=f"{state_key}_rev",
                 disabled=not approver):
        packet.reject(approver, note, revision=True)
        st.rerun()
    if c3.button("Reject", key=f"{state_key}_no", disabled=not approver):
        packet.reject(approver, note)
        st.rerun()

    if not approver:
        st.caption("A name is required: a sign-off with nobody attached to it "
                   "is not a sign-off.")
    if packet.approval.decision in ("REJECTED", "REVISION_REQUESTED"):
        st.info(f"Recorded: {packet.approval.decision} by "
                f"{packet.approval.approver}"
                + (f" — {packet.approval.note}" if packet.approval.note else ""))


def page_agent():
    A = _agent_imports()

    st.markdown("<div class='eyebrow'>Close-cycle agent</div>", unsafe_allow_html=True)
    st.markdown("<div class='headline'>Plan → validate → execute → audit → approve</div>",
                unsafe_allow_html=True)
    st.markdown("<hr class='rule'/>", unsafe_allow_html=True)
    st.markdown(
        "<div class='small'>A model proposes a <b>plan</b>; it never executes "
        "anything and never states a figure it did not retrieve. Static "
        "validation is a hard barrier before any query runs. Every number in the "
        "commentary is a tool return value, verified against the run ledger. "
        "Nothing is published without a human decision.</div>",
        unsafe_allow_html=True)

    if SC.diff_from_default():
        st.info(
            "**The agent runs against the materialized baseline marts, not your "
            "scenario edits.** The marts are a build artifact — in production "
            "they would be a scheduled dbt run, not a per-user recomputation — "
            "so the agent's figures here reflect the committed dataset. The "
            "other pages reflect your edits.")

    # Build the marts if this server has never built them. First call on a
    # cold deploy takes a few seconds; every call after it is a hash check.
    try:
        with st.spinner("Preparing the agent's data\u2026"):
            _agent_marts_ready()
    except Exception as e:  # noqa: BLE001
        st.error(f"**The agent's data could not be prepared.** "
                 f"{type(e).__name__}: {e}")
        st.caption("The other pages are unaffected: they compute from the "
                   "synthetic CSVs in memory and do not use this database.")
        return

    period = str(sel_month)[:7]
    st.markdown(f"<div class='small'>Analyzing <b>{esc(month_label(sel_month))}"
                f"</b>, the reporting month selected in the sidebar.</div>",
                unsafe_allow_html=True)

    st.markdown("---")
    # Two modes, deliberately. A third "verified replay" tab served a committed
    # run and rendered the SAME package as "Run now" with an integrity banner
    # most readers could not interpret -- three modes where two looked
    # identical cost more in confusion than the point was worth. The replay
    # machinery stays (replay.py, the hash checks, verify_decks.py, the tamper
    # tests): it is what the claim rests on, it just does not need a tab.
    mode = st.radio(
        "Mode",
        ["Standard monthly close", "Ask a question"],
        horizontal=True, label_visibility="collapsed",
        help="The standard close runs the same fixed plan every month. "
             "Ask a question lets a model plan for something the fixed plan "
             "does not cover.")

    if mode.startswith("Standard"):
        _agent_now(A, period)
    else:
        _agent_live(A, period)


@st.cache_resource(show_spinner=False)
def _agent_marts_ready() -> str:
    """Build the agent's marts if this machine has never built them.

    data/processed/*.duckdb is gitignored -- correctly, it is a build artifact
    derived from the committed CSVs. A fresh deploy therefore has no database,
    and the agent opens it READ-ONLY, which cannot create one. Every other page
    computes from the CSVs in memory and never touches DuckDB, so the deployed
    app failed on the agent page alone with "database does not exist".

    Cached, so the build happens once per server rather than once per rerun.
    """
    from agent import materialize as agent_mz

    return agent_mz.ensure_ready(verbose=False)


@st.cache_resource(show_spinner=False)
def _agent_run_for(period: str, _stamp: str):
    """Deterministic package + commentary for one month.

    Cheap enough to run on demand -- eleven tool calls take about 40ms and no
    credential -- which is why the agent page can follow the sidebar month
    instead of being pinned to whatever happened to be cached. `_stamp` is the
    mart build hash, so the cache invalidates when the data underneath moves.

    cache_resource rather than cache_data: these are live objects with methods
    the page calls, not picklable frames.
    """
    from agent import materialize as agent_mz
    from agent import tools as _t  # noqa: F401
    from agent.gates import PublicationPacket, review_plan, summarize_plan
    from agent.narrate import narrate
    from agent.orchestrator import Orchestrator
    from agent.packages import build_goal, variance_package_plan

    con = agent_mz.connect_readonly()
    try:
        goal = build_goal(con, period)
        plan = variance_package_plan(goal)
        result = Orchestrator(con).run(plan, goal)
        candidate = narrate(result, goal, client=None, all_entity_names=NAMES,
                            mode="inject")
    finally:
        con.close()

    # The PACKET is deliberately not returned. cache_resource is shared across
    # every session on the server, so a cached packet would mean one visitor's
    # sign-off appearing as signed to everyone else -- an approval attached to
    # no one, on the artifact whose whole point is that a named human accepted
    # it. The run is shared; the decision is not.
    review = review_plan(plan, reviewer="deterministic run",
                         note="hand-written canonical plan")
    return result, goal, candidate, review, summarize_plan(plan)


def _agent_now(A, period: str):
    """The agent, executed now, for the month selected in the sidebar."""
    from agent import materialize as agent_mz
    from agent.gates import blocking_reasons

    try:
        result, goal, candidate, review, plan_summary = _agent_run_for(
            period, agent_mz.build_hash())
    except agent_mz.StaleMartError as e:
        st.error(f"**The agent marts are stale.** {e}")
        return
    except Exception as e:  # noqa: BLE001
        st.error(f"{type(e).__name__}: {e}")
        return

    # Built per session, and keyed on the month so that changing months cannot
    # carry a sign-off onto a different period's package.
    from agent.gates import PublicationPacket

    pk_key = f"agent_packet_{period}"
    packet = st.session_state.setdefault(
        pk_key, PublicationPacket(result, candidate, plan_review=review))

    c = result.ledger.cost_summary()
    cols = st.columns(4)
    cols[0].metric("Tool calls", c["steps"])
    cols[1].metric("Wall clock", f"{c['wall_clock_s']:.3f}s")
    cols[2].metric("Tokens", f"{c['tokens_in'] + c['tokens_out']:,}")
    cols[3].metric("Figures verified", len(candidate.matched))
    st.caption("Hand-written plan, no model in the loop. Every figure below is "
               "a tool return value.")
    st.markdown("<div class='small'>This is the <b>fixed monthly close</b>: "
                "the same eleven steps every period, run against the month "
                "selected in the sidebar. Use <b>Plan with a model</b> when the "
                "question is not the standard one.</div>",
                unsafe_allow_html=True)

    st.markdown("---")
    _section("Financial performance overview",
             "The full month: profit and loss against plan, the ranked "
             "drivers, and the account-level detail behind them. Every figure "
             "is computed in SQL — the model never performs arithmetic.")
    _render_package_visual(result)
    with st.expander("Plain-text package (what the CLI prints)"):
        st.code(A["render_package"](result), language=None)

    st.markdown("---")
    _section("Commentary",
             "Generated from the computed figures above. Every number was "
             "verified against the run ledger before it could appear.")
    _render_narrative_block({
        "text": candidate.text, "source": candidate.source,
        "attempts": candidate.attempts,
        "matched": [list(m) for m in candidate.matched],
        "violations": candidate.violations,
        "deterministic_by_choice": candidate.deterministic_by_choice,
        "reason": candidate.reason,
    }, f"now_{period}")

    st.markdown("---")
    _gate1_panel(packet, blocking_reasons(packet), f"now_gate1_{period}")

    if packet.published:
        st.markdown("---")
        _deliverables(result, goal, candidate, packet, f"now_{period}")
    else:
        st.markdown("<div class='small'>The deck becomes available once this "
                    "package is <b>signed off above</b> — an unsigned deck "
                    "is exactly the artifact that should not be leaving the "
                    "building.</div>", unsafe_allow_html=True)

    with st.expander("Run ledger — every tool call, argument, and result hash"):
        st.dataframe(_ledger_table(result.ledger), hide_index=True,
                     width="stretch")
        st.caption("`resolved from` shows a symbolic reference and the value "
                   "the orchestrator bound to it. Data moves between steps by "
                   "reference — the model never re-reads a result and retypes "
                   "a figure.")
        st.json(c)


# The saved-run viewer was removed with its tab. It rendered the same package
# as the standard close with an integrity banner most readers could not
# interpret, so keeping it as dead UI code would invite it back. The replay
# MACHINERY is untouched -- src/agent/replay.py, the hash verification, the
# tamper tests, verify_decks.py -- because that is what the "these numbers are
# real" claim actually rests on. It simply does not need a tab.


def _agent_live(A, period_default: str = ""):
    _section(
        "Plan with a model",
        "The deterministic run above answers <b>one fixed question</b>. This "
        "asks the model to plan for a question it has not seen — that is the "
        "capability an agent actually adds. It costs a fraction of a cent and "
        "several seconds; the free run above is instant, so use this when the "
        "question changes, not when it does not. Your key lives in this "
        "session only: never logged, never written to the ledger.")

    provider = st.radio("Provider", ["OpenAI", "Anthropic"], horizontal=True,
                        key="agent_provider")
    key = get_key(provider)
    env = "OPENAI_API_KEY" if provider == "OpenAI" else "ANTHROPIC_API_KEY"

    if not key:
        entered = st.text_input(f"{provider} API key", type="password",
                                key="agent_key_in")
        if entered:
            st.session_state[env] = entered
            os.environ[env] = entered
            st.rerun()
        st.info("No key set. **The cached runs above need no key** and show the "
                "same architecture end to end.")
        return
    os.environ[env] = key

    # Example goals that show RANGE. The generic "prepare the variance package"
    # produces a thinner copy of the free deterministic run, which makes the
    # model path look redundant -- it is the wrong demonstration of the thing
    # the model is actually for.
    EXAMPLES = [
        "why did operating expenses miss plan this month?",
        "is this month's miss a one-off or has it been building?",
        "is the revenue miss a volume problem or a price problem?",
        "how does this month compare with last month?",
        "how are we tracking year to date?",
        "is Sales & Marketing spend growing faster than revenue?",
        "what is our cash runway?",          # the tools cannot answer this
        "prepare the full variance package for this period",
    ]

    c1, c2 = st.columns([3, 2])
    example = c1.selectbox("Start from an example question", EXAMPLES,
                           key="agent_example")
    _ix = next((i for i, m in enumerate(MONTHS)
                if str(m).startswith(period_default)), len(MONTHS) - 1)
    period = c2.selectbox("Period", MONTHS, index=_ix,
                          format_func=month_label, key="agent_period")

    goal_text = st.text_input("Goal — edit freely", value=example,
                              key=f"agent_goal_{EXAMPLES.index(example)}")

    # The plan-review gate is deliberately NOT surfaced here. It exists in the
    # orchestrator and the decision log, and it matters for a write-capable
    # registry -- but on a read-only surface it adds inspectability rather than
    # safety, and a plan-approval table is unreadable to anyone outside the
    # project. The plan is still shown after the run, as evidence rather than
    # as a control the reader is asked to operate.
    gate0_on = False
    with st.expander("Run controls"):
        max_steps = st.slider("Maximum steps (hard cap)", 4, 20, 14)

    thread = st.session_state.get("agent_thread", [])
    if thread:
        _section("This session",
                 "Each question below was planned with the previous answers in "
                 "view, so a follow-up retrieves only what it still needs "
                 "rather than starting again.")
        for i, t in enumerate(thread, start=1):
            st.markdown(f"<div style='color:#0F172A'>{i}. "
                        f"<b>{esc(t['goal_text'])}</b> — "
                        f"{len(t['sections'])} result set(s)</div>",
                        unsafe_allow_html=True)
        if st.button("Start a new session"):
            for k in ("agent_thread", "agent_packet", "agent_pending",
                      "agent_refusal"):
                st.session_state.pop(k, None)
            st.rerun()
        st.markdown("---")

    # A follow-up re-enters the same planning path with the session thread in
    # context, so there is one code path for "ask" and "ask again" rather than
    # two that can drift.
    followup = st.session_state.pop("agent_followup_pending", None)
    if st.button("Plan and run", type="primary") or followup:
        if followup:
            goal_text = followup
        st.session_state.pop("agent_packet", None)
        st.session_state.pop("agent_refusal", None)
        st.session_state.pop("agent_pending_plan", None)
        try:
            con = A["mz"].connect_readonly()
            goal = A["build_goal"](con, str(period)[:7])
            client = A["agent_client"](provider.lower())
            prior = st.session_state.get("agent_thread", [])
            with st.spinner(f"Planning with {client.model}…"):
                pr = A["Planner"](client).propose(goal_text, goal,
                                                  prior_runs=prior)
            if pr.refused:
                st.session_state["agent_refusal"] = {
                    "goal": goal_text, "reason": pr.refusal,
                    "model": client.model,
                    "tokens": pr.tokens_in + pr.tokens_out,
                    "cost": pr.cost_usd if pr.pricing_known else None,
                }
                st.session_state.pop("agent_pending", None)
                st.rerun()
            st.session_state["agent_last_goal"] = goal_text
            st.session_state["agent_pending"] = {
                "plan": pr.plan, "goal": goal, "pr": pr,
                "period": str(period)[:7], "gate0": gate0_on,
                "max_steps": max_steps, "client_model": client.model,
            }
        except A["PlannerError"] as e:
            st.error("The planner could not produce a valid plan.")
            for a in e.attempts:
                st.caption(f"attempt {a['attempt']}: {a['problems']}")
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"{type(e).__name__}: {e}")
            return
        st.rerun()

    refusal = st.session_state.get("agent_refusal")
    if refusal:
        st.markdown("---")
        _section("The agent declined this question",
                 "The tool surface is deliberately narrow. When a question "
                 "needs data these tools do not expose, the correct behaviour "
                 "is to say so — <b>not</b> to answer a different question that "
                 "happens to be answerable and present it as though it were the "
                 "one asked.")
        st.warning(f"**{esc(refusal['goal'])}**\n\n{esc(refusal['reason'])}")
        cost = (f"${refusal['cost']:.4f}" if refusal.get("cost") is not None
                else "unpriced")
        st.caption(f"{refusal['model']} · {refusal['tokens']:,} tokens · {cost} "
                   f"· no query was run")
        return

    pending = st.session_state.get("agent_pending")
    packet = st.session_state.get("agent_packet")

    if pending and not packet:
        from agent.gates import review_plan, summarize_plan

        pr = pending["pr"]
        st.markdown("---")
        st.markdown("<div class='eyebrow'>Proposed plan</div>", unsafe_allow_html=True)
        st.caption(f"{pending['client_model']} · {len(pr.attempts)} attempt(s) · "
                   f"{pr.tokens_in + pr.tokens_out} tokens · "
                   + (f"${pr.cost_usd:.4f}" if pr.pricing_known else "unpriced"))
        for a in pr.attempts:
            if not a["accepted"]:
                st.warning(f"attempt {a['attempt']} rejected by static "
                           f"validation: {a['problems']}")
        st.dataframe(_plan_table(summarize_plan(pr.plan)), hide_index=True, width="stretch")


        with st.spinner("Executing and auditing…"):
            packet = _execute_live(A, pending, pr)
        st.session_state["agent_packet"] = packet
        # Summaries only, never the rows: replaying full results into the next
        # prompt would grow without bound and invite the model to quote a
        # figure from context instead of retrieving it.
        st.session_state.setdefault("agent_thread", []).append({
            "goal_text": st.session_state.get("agent_last_goal", ""),
            "sections": {k: {"step": v["step"], "tool": v["tool"],
                             "params": v["params"], "rows": v["rows"][:3]}
                         for k, v in packet.result.sections.items()},
        })
        st.rerun()

    if packet:
        _render_live_packet(A, packet)


def _execute_live(A, pending, pr):
    from agent.ledger import Budget

    con = A["mz"].connect_readonly()
    goal = pending["goal"]
    orch = A["Orchestrator"](con, budget=Budget(max_steps=pending["max_steps"]))
    result = orch.run(pr.plan, goal)
    result.ledger.record_planning(
        tokens_in=pr.tokens_in, tokens_out=pr.tokens_out,
        cost_usd=pr.cost_usd if pr.pricing_known else None,
        pricing_known=pr.pricing_known, latency_ms=pr.latency_ms,
        model=pr.model, attempts=len(pr.attempts))

    client = A["agent_client"]()
    candidate = A["narrate"](result, goal, client, NAMES, mode="audit",
                             max_retries=1)
    review = A["review_plan"](pr.plan, reviewer="live session",
                              note="plan review not surfaced in the UI")
    return A["PublicationPacket"](result, candidate, plan_review=review)


def summarize_plan_safe(result):
    """Plan summary reconstructed from the ledger when none was recorded."""
    return [{"step": e.step_idx, "tool": e.tool,
             "params": e.params_declared, "purpose": "",
             "optional": False,
             "resolved_at_runtime": sorted(
                 k for k, v in (e.params_declared or {}).items()
                 if isinstance(v, str) and v.startswith("$"))}
            for e in result.ledger.entries]


def _render_live_packet(A, packet):
    from agent.gates import summarize_plan  # noqa: F401

    result = packet.result
    goal = result.ledger.goal
    c = result.ledger.cost_summary()

    st.markdown("---")
    _section("What the model was asked, and what it did",
             "The plan was authored for <b>your question</b>, not from a fixed "
             "template — which is the whole reason to spend a model call here. "
             "Compare the steps below with the deterministic run: a different "
             "question produces a different plan.")
    st.markdown(f"> {esc(st.session_state.get('agent_last_goal', ''))}")
    cols = st.columns(4)
    cols[0].metric("Tool calls", c["steps"])
    cols[1].metric("Planning time", f"{c['planning_latency_ms'] / 1000:.1f}s")
    cols[2].metric("Tokens", f"{c['tokens_in'] + c['tokens_out']:,}")
    cols[3].metric("Cost",
                   f"${c['cost_usd']:.4f}" if c.get("pricing_known")
                   else "unpriced")

    with st.expander("Steps the model chose (execution order)"):
        st.dataframe(_plan_table(packet.plan_review.plan_summary
                                 or summarize_plan_safe(result)),
                     hide_index=True, width="stretch")

    st.markdown("---")
    _render_briefing(result, goal, "live")

    st.markdown("---")
    _section("Supporting detail",
             "Everything the plan retrieved, in full. This is the evidence the "
             "prioritisation above rests on.")
    _render_package_visual(result)
    with st.expander("Plain-text package (what the CLI prints)"):
        st.code(A["render_package"](result), language=None)

    c = packet.candidate
    cand = packet.candidate
    narr = {
        "text": cand.text, "source": cand.source, "attempts": cand.attempts,
        "matched": [list(m) for m in cand.matched],
        "violations": cand.violations,
        "deterministic_by_choice": cand.deterministic_by_choice,
        "reason": cand.reason,
    }
    st.markdown("---")
    _section("Commentary",
             "Written by the model over the figures above and nothing else. "
             "Every number in it was checked against the run ledger before it "
             "could appear; a figure that failed verification would have "
             "blocked publication entirely.")
    _render_narrative_block(narr, "live")

    st.markdown("---")
    _gate1_panel(packet, A["blocking_reasons"](packet), "live_gate1")

    # Deliverables are gated on sign-off in this path too. An unsigned deck or
    # packet is exactly the artifact that should not be leaving the building.
    if packet.published:
        st.markdown("---")
        _deliverables(packet.result, packet.result.ledger.goal,
                      packet.candidate, packet, "live")
    else:
        st.markdown("<div class='small'>The deck and the Word documents become "
                    "available once this package is <b>signed off above</b>."
                    "</div>", unsafe_allow_html=True)

    with st.expander("Run ledger"):
        st.dataframe(_ledger_table(packet.result.ledger), hide_index=True, width="stretch")
        st.json(packet.result.ledger.cost_summary())

    st.markdown("---")
    _section("Ask a follow-up",
             "The next question is planned with everything above already in "
             "view, so it retrieves only the delta. This is a conversation "
             "with a bounded warehouse — every follow-up still produces a plan "
             "that is validated before a single query runs.")
    follow = st.text_input("Follow-up question", value="",
                           placeholder="e.g. break that down by account",
                           key="agent_followup")
    if st.button("Ask", disabled=not follow, key="agent_followup_go"):
        st.session_state["agent_followup_pending"] = follow
        st.session_state.pop("agent_packet", None)
        st.rerun()

    if packet.published and st.button("Save this run to the replay store"):
        from agent.replay import save_run
        path = save_run(packet.result, packet.candidate, packet,
                        label=f"live-{packet.result.ledger.run_id}")
        st.success(f"Saved {os.path.basename(path)}")


PAGES = {
    "Overview": page_overview,
    "Variance": page_variance,
    "Forecast": page_forecast,
    "Commentary": page_commentary,
    "Close-cycle agent": page_agent,
    "Guardrails & Eval": page_eval,
    "ROI": page_roi,
    "Decision log": page_decisions,
}
PAGES[page]()
