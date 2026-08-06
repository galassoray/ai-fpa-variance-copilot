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


PAGES = {
    "Overview": page_overview,
    "Variance": page_variance,
    "Forecast": page_forecast,
    "Commentary": page_commentary,
    "Guardrails & Eval": page_eval,
    "ROI": page_roi,
    "Decision log": page_decisions,
}
PAGES[page]()
