"""
generate_synthetic_data.py
===========================
Generates the synthetic SaaS financial dataset for the AI FP&A Variance
Commentary Copilot.

Every INPUT lives in scenario.py; this module turns a Scenario into a dataset.
Passing the default Scenario reproduces the baseline dataset bit-for-bit (there
is a test asserting exactly that), so scenario editing cannot silently drift the
company out from under the portfolio narrative.

Design rules (see decision_log.md):
  * 100% synthetic. No real-company data of any kind.
  * Amounts are stored as POSITIVE MAGNITUDES. Favorability is decided by
    account metadata (favorable_direction), never by the raw sign of a number.
  * Salary lines are generated AS headcount x avg_comp so that the compensation
    variance decomposition reconciles exactly to the GL salary line.
  * Revenue and COGS are carried at the company (CORP) level; the four operating
    departments (S&M, R&D, G&A, CS) carry OpEx.
  * Business stories (marketing overspend, bookings shortfall, contractor
    overrun, legal spike, CS under-hiring, hosting margin pressure) are
    deliberately engineered in so the variance engine has a real story to find.
  * The seed is held fixed across scenarios, so a change in the output is
    attributable to the input you moved rather than to random noise.

Output: CSV files in data/synthetic/ (or an in-memory dict via build_dataset).
"""

from __future__ import annotations
import os
import numpy as np
import pandas as pd

from scenario import Scenario, DEFAULT

SEED = 20260625  # baseline seed (Scenario.seed overrides per run)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "data", "synthetic")

# ---------------------------------------------------------------------------
# 1. Calendar : 24 months of actuals, FY2024 + FY2025. Budget for all 24
#    months (an annual plan existed each year). One forecast version covering
#    FY2025 (a mid-prior-year reforecast).
# ---------------------------------------------------------------------------
MONTHS = pd.date_range("2024-01-01", "2025-12-01", freq="MS")
FORECAST_MONTHS = pd.date_range("2025-01-01", "2025-12-01", freq="MS")


def build_dim_date() -> pd.DataFrame:
    rows = []
    for m in MONTHS:
        rows.append(
            {
                "month": m.date().isoformat(),
                "quarter": f"{m.year}-Q{((m.month - 1) // 3) + 1}",
                "fiscal_year": m.year,
                "month_number": m.month,
                "is_actual_period": True,
                "is_budget_period": True,
                "is_forecast_period": m in FORECAST_MONTHS,
            }
        )
    return pd.DataFrame(rows)


def build_dim_department() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("CORP", "Corporate / Company", "Company"),
            ("SM", "Sales & Marketing", "Go-To-Market"),
            ("RND", "Research & Development", "Product"),
            ("GA", "General & Administrative", "Operations"),
            ("CS", "Customer Success", "Go-To-Market"),
        ],
        columns=["department_id", "department_name", "department_group"],
    )


ACCOUNTS = [
    ("REV_SUB", "Subscription Revenue", "CORP", "Subscription", "Revenue", "higher", 1, 0, 0),
    ("REV_SVC", "Services Revenue", "CORP", "Services", "Revenue", "higher", 1, 0, 0),
    ("COGS_HOST", "Hosting / Infrastructure", "CORP", "Infrastructure", "Cost of Revenue", "lower", 0, 1, 0),
    ("COGS_SUPPORT", "Customer Support COGS", "CORP", "Support", "Cost of Revenue", "lower", 0, 1, 0),
    ("COGS_PAY", "Payment Processing", "CORP", "Payments", "Cost of Revenue", "lower", 0, 1, 0),
    ("SM_SAL", "Salaries", "SM", "Personnel", "Operating Expenses", "lower", 0, 0, 1),
    ("SM_COMM", "Commissions", "SM", "Personnel", "Operating Expenses", "lower", 0, 0, 1),
    ("SM_MKT", "Paid Marketing", "SM", "Marketing", "Operating Expenses", "lower", 0, 0, 1),
    ("SM_EVENTS", "Events", "SM", "Marketing", "Operating Expenses", "lower", 0, 0, 1),
    ("SM_SW", "Sales Software", "SM", "Software", "Operating Expenses", "lower", 0, 0, 1),
    ("SM_TE", "Travel & Entertainment", "SM", "Travel", "Operating Expenses", "lower", 0, 0, 1),
    ("RND_SAL", "Salaries", "RND", "Personnel", "Operating Expenses", "lower", 0, 0, 1),
    ("RND_CONTRACT", "Contractors", "RND", "Contractors", "Operating Expenses", "lower", 0, 0, 1),
    ("RND_CLOUD", "Cloud Development Tools", "RND", "Software", "Operating Expenses", "lower", 0, 0, 1),
    ("RND_SW", "Software", "RND", "Software", "Operating Expenses", "lower", 0, 0, 1),
    ("RND_RECRUIT", "Recruiting", "RND", "Recruiting", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_SAL", "Salaries", "GA", "Personnel", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_LEGAL", "Legal & Professional Fees", "GA", "Legal & Professional", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_SW", "Accounting / Finance Software", "GA", "Software", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_INS", "Insurance", "GA", "Insurance", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_FAC", "Facilities", "GA", "Facilities", "Operating Expenses", "lower", 0, 0, 1),
    ("GA_ADMIN", "Office / Admin", "GA", "Office", "Operating Expenses", "lower", 0, 0, 1),
    ("CS_SAL", "Salaries", "CS", "Personnel", "Operating Expenses", "lower", 0, 0, 1),
    ("CS_SW", "Customer Success Software", "CS", "Software", "Operating Expenses", "lower", 0, 0, 1),
    ("CS_TRAIN", "Training / Enablement", "CS", "Training", "Operating Expenses", "lower", 0, 0, 1),
    ("CS_TE", "Travel & Entertainment", "CS", "Travel", "Operating Expenses", "lower", 0, 0, 1),
]


def build_dim_account() -> pd.DataFrame:
    df = pd.DataFrame(
        ACCOUNTS,
        columns=[
            "account_id", "account_name", "department_id", "account_category",
            "statement_line", "favorable_direction",
            "is_revenue", "is_cogs", "is_opex",
        ],
    )
    df["normal_sign"] = 1
    df["is_expense"] = ((df["is_cogs"] == 1) | (df["is_opex"] == 1)).astype(int)
    df["oi_sign"] = np.where(df["is_revenue"] == 1, 1, -1)
    return df


def _linterp(a, b, n):
    return np.round(np.linspace(a, b, n)).astype(int)


def build_headcount(sc: Scenario, rng) -> pd.DataFrame:
    n = len(MONTHS)
    rows = []
    for dept in ["SM", "RND", "GA", "CS"]:
        bud_hc = _linterp(sc.hc_budget_start[dept], sc.hc_budget_end[dept], n)
        act_hc = bud_hc.copy()
        fc_hc = bud_hc.copy()

        # --- engineered headcount stories (apply to FY2025 = months 12..23) ---
        for i, m in enumerate(MONTHS):
            y2025 = (m.year == 2025)
            if dept == "SM" and y2025 and m.month >= 3:
                act_hc[i] = bud_hc[i] + (sc.sm_ramp_early if m.month < 7 else sc.sm_ramp_late)
            if dept == "CS" and y2025:
                act_hc[i] = max(bud_hc[i] - sc.cs_hc_shortfall, 0)
            if dept == "RND" and y2025 and m.month >= 5:
                act_hc[i] = bud_hc[i] - sc.rnd_hc_shortfall

        for i, m in enumerate(MONTHS):
            if m.year == 2025:
                if dept == "SM" and m.month >= 3:
                    fc_hc[i] = bud_hc[i] + (sc.sm_ramp_early_forecast if m.month < 7
                                            else sc.sm_ramp_late_forecast)
                if dept == "CS":
                    fc_hc[i] = max(bud_hc[i] - sc.cs_hc_shortfall_forecast, 0)

        comp_b = sc.avg_comp_budget[dept]
        for i, m in enumerate(MONTHS):
            drift = sc.comp_drift[dept]
            comp_a = comp_b * (1 + drift * (i / n)) * (1 + rng.normal(0, 0.004))
            comp_f = comp_b * (1 + 0.5 * drift * (i / n))
            rows.append(
                {
                    "month": m.date().isoformat(),
                    "department_id": dept,
                    "actual_headcount": int(act_hc[i]),
                    "budget_headcount": int(bud_hc[i]),
                    "forecast_headcount": int(fc_hc[i]),
                    "avg_comp_actual": round(float(comp_a), 2),
                    "avg_comp_budget": round(float(comp_b), 2),
                    "avg_comp_forecast": round(float(comp_f), 2),
                }
            )
    return pd.DataFrame(rows)


def build_saas_metrics(sc: Scenario, rng):
    def arr_bridge(scen_name):
        starting = float(sc.starting_arr)
        cust_start = int(sc.starting_customers)
        new_arr, exp_arr, con_arr, chu_arr = [], [], [], []
        new_c, chu_c = [], []
        start_arr_list, end_arr_list = [], []
        start_c_list, end_c_list = [], []

        s_arr = starting
        s_c = cust_start
        for i, m in enumerate(MONTHS):
            y2025 = (m.year == 2025)
            # Seasonal shape on the FLOWS. Applied to budget, actual and
            # forecast alike, because a planner plans for the Q4 push -- if it
            # were applied to actuals only, seasonality would masquerade as
            # variance and swamp the engineered story. Cosine peaks in December
            # and troughs in June; churn clusters on the January and July
            # renewal dates. At amplitude 0.0 both factors are exactly 1.0, so
            # the baseline dataset is untouched.
            season = 1.0
            churn_season = 1.0
            if sc.bookings_seasonality:
                season = 1.0 + sc.bookings_seasonality * np.cos(
                    2 * np.pi * (m.month - 12) / 12)
            if sc.churn_seasonality:
                churn_season = 1.0 + sc.churn_seasonality * (
                    1.0 if m.month in (1, 7) else -0.25)

            base_new = (sc.base_new_arr + sc.new_arr_growth * i) * season
            base_exp = (sc.base_expansion_arr + sc.expansion_arr_growth * i) * season
            base_con = sc.base_contraction_arr + sc.contraction_arr_growth * i
            base_chu = (sc.base_churned_arr + sc.churned_arr_growth * i) * churn_season
            base_new_c = sc.base_new_customers
            base_chu_c = sc.base_churned_customers

            new = base_new; exp = base_exp; con = base_con; chu = base_chu
            nc = base_new_c; cc = base_chu_c

            if scen_name == "actual":
                noise = lambda s: 1 + rng.normal(0, s)
                new *= noise(0.03); exp *= noise(0.03); con *= noise(0.04); chu *= noise(0.04)
                if y2025:
                    new *= sc.actual_new_arr_mult
                    chu *= sc.actual_churn_mult
                    nc = int(round(base_new_c * sc.actual_new_cust_mult))
                    cc = int(round(base_chu_c * sc.actual_churn_cust_mult))
            elif scen_name == "forecast":
                if y2025:
                    new *= sc.forecast_new_arr_mult
                    chu *= sc.forecast_churn_mult
                    nc = int(round(base_new_c * sc.forecast_new_cust_mult))
                    cc = int(round(base_chu_c * sc.forecast_churn_cust_mult))

            new = round(new, 2); exp = round(exp, 2); con = round(con, 2); chu = round(chu, 2)
            end = s_arr + new + exp - con - chu
            ec = s_c + nc - cc

            start_arr_list.append(round(s_arr, 2)); end_arr_list.append(round(end, 2))
            new_arr.append(new); exp_arr.append(exp); con_arr.append(con); chu_arr.append(chu)
            start_c_list.append(s_c); end_c_list.append(ec); new_c.append(nc); chu_c.append(cc)

            s_arr = end
            s_c = ec
        return {
            "starting_arr": start_arr_list, "new_arr": new_arr, "expansion_arr": exp_arr,
            "contraction_arr": con_arr, "churned_arr": chu_arr, "ending_arr": end_arr_list,
            "customers_start": start_c_list, "new_customers": new_c,
            "churned_customers": chu_c, "customers_end": end_c_list,
        }

    scen = {s: arr_bridge(s) for s in ["budget", "actual", "forecast"]}

    def to_frame(s):
        d = scen[s]
        df = pd.DataFrame(d)
        df.insert(0, "month", [m.date().isoformat() for m in MONTHS])
        df["arpa"] = (df["ending_arr"] / df["customers_end"]).round(2)
        df["logo_churn_rate"] = (df["churned_customers"] / df["customers_start"]).round(5)
        df["nrr"] = ((df["starting_arr"] + df["expansion_arr"] - df["contraction_arr"]
                      - df["churned_arr"]) / df["starting_arr"]).round(5)
        df["grr"] = ((df["starting_arr"] - df["contraction_arr"]
                      - df["churned_arr"]) / df["starting_arr"]).round(5)
        return df

    return {s: to_frame(s) for s in ["budget", "actual", "forecast"]}


def build_facts(sc: Scenario, rng, dim_account, headcount, saas):
    months_iso = [m.date().isoformat() for m in MONTHS]
    hc = headcount.set_index(["month", "department_id"])

    def salary_amount(scen_name, month, dept):
        r = hc.loc[(month, dept)]
        h = r[f"{scen_name}_headcount"]
        c = r[f"avg_comp_{scen_name}"]
        return float(h) * float(c) / 12.0

    def sub_revenue(scen_name, i):
        return float(saas[scen_name].iloc[i]["ending_arr"]) / 12.0

    def make(scen_name):
        recs = []
        for i, month in enumerate(months_iso):
            m = MONTHS[i]
            y2025 = (m.year == 2025)
            sub_rev = sub_revenue(scen_name, i)
            svc_rev = sub_rev * sc.services_pct_of_subscription

            recs.append((month, "CORP", "REV_SUB", round(sub_rev, 2)))
            recs.append((month, "CORP", "REV_SVC", round(svc_rev, 2)))

            host_base = sub_rev * sc.hosting_pct_of_revenue
            if scen_name == "actual" and y2025:
                host_base *= sc.actual_hosting_mult
            elif scen_name == "forecast" and y2025:
                host_base *= sc.forecast_hosting_mult
            host = host_base * (1 + (rng.normal(0, 0.02) if scen_name == "actual" else 0))
            support = sub_rev * sc.support_pct_of_revenue * (
                1 + (rng.normal(0, 0.02) if scen_name == "actual" else 0))
            pay = (sub_rev + svc_rev) * sc.payments_pct_of_revenue
            recs.append((month, "CORP", "COGS_HOST", round(float(host), 2)))
            recs.append((month, "CORP", "COGS_SUPPORT", round(float(support), 2)))
            recs.append((month, "CORP", "COGS_PAY", round(float(pay), 2)))

            for dept in ["SM", "RND", "GA", "CS"]:
                sal_acct = f"{dept}_SAL"
                recs.append((month, dept, sal_acct,
                             float(np.round(salary_amount(scen_name, month, dept), 2))))

            comm = sub_rev * sc.commission_pct_of_revenue
            mkt = sc.base_marketing + sc.marketing_growth * i
            events = sc.base_events + sc.events_growth * i
            sm_sw = sc.base_sm_software + sc.sm_software_growth * i
            sm_te = sc.base_sm_te + sc.sm_te_growth * i
            if scen_name == "actual":
                if y2025:
                    mkt *= sc.actual_marketing_mult
                    comm *= sc.actual_commission_mult
                # FY2024 story: user conference scaled back in the autumn, so
                # events land UNDER plan -- the one favourable driver in the
                # dataset. Applied before the noise draw so the draw order (and
                # therefore the baseline at multiplier 1.0) is unchanged.
                if m.year == 2024 and m.month in (9, 10, 11):
                    events *= sc.actual_fy24_events_mult
                mkt *= (1 + rng.normal(0, 0.03)); events *= (1 + rng.normal(0, 0.05))
            elif scen_name == "forecast" and y2025:
                mkt *= sc.forecast_marketing_mult
            recs += [
                (month, "SM", "SM_COMM", round(float(comm), 2)),
                (month, "SM", "SM_MKT", round(float(mkt), 2)),
                (month, "SM", "SM_EVENTS", round(float(events), 2)),
                (month, "SM", "SM_SW", round(float(sm_sw), 2)),
                (month, "SM", "SM_TE", round(float(sm_te), 2)),
            ]

            contract = sc.base_contractors + sc.contractors_growth * i
            cloud = sc.base_rnd_cloud + sc.rnd_cloud_growth * i
            rnd_sw = sc.base_rnd_software + sc.rnd_software_growth * i
            recruit = sc.base_rnd_recruiting + sc.rnd_recruiting_growth * i
            if scen_name == "actual":
                if y2025:
                    contract *= sc.actual_contractors_mult
                # FY2024 story: agency fees run hot in H2 as the company hires
                # ahead of the FY2025 headcount ramp already in fact_headcount
                # (R&D 38 -> 52, S&M 40 -> 58). The variance is explained by
                # another table in the same dataset, which is the point.
                if m.year == 2024 and m.month >= 7:
                    recruit *= sc.actual_fy24_recruiting_mult
                contract *= (1 + rng.normal(0, 0.04)); cloud *= (1 + rng.normal(0, 0.03))
            elif scen_name == "forecast" and y2025:
                contract *= sc.forecast_contractors_mult
            recs += [
                (month, "RND", "RND_CONTRACT", round(float(contract), 2)),
                (month, "RND", "RND_CLOUD", round(float(cloud), 2)),
                (month, "RND", "RND_SW", round(float(rnd_sw), 2)),
                (month, "RND", "RND_RECRUIT", round(float(recruit), 2)),
            ]

            legal = sc.base_legal + sc.legal_growth * i
            ga_sw = sc.base_ga_software + sc.ga_software_growth * i
            ins = sc.base_ga_insurance + sc.ga_insurance_growth * i
            fac = sc.base_ga_facilities + sc.ga_facilities_growth * i
            admin = sc.base_ga_admin + sc.ga_admin_growth * i
            if scen_name == "actual" and y2025 and m.month in (6, 7):
                legal *= sc.actual_legal_spike_mult
            if scen_name == "actual":
                legal *= (1 + rng.normal(0, 0.02))
            elif scen_name == "forecast" and y2025 and m.month in (6, 7):
                legal *= sc.forecast_legal_spike_mult
            recs += [
                (month, "GA", "GA_LEGAL", round(float(legal), 2)),
                (month, "GA", "GA_SW", round(float(ga_sw), 2)),
                (month, "GA", "GA_INS", round(float(ins), 2)),
                (month, "GA", "GA_FAC", round(float(fac), 2)),
                (month, "GA", "GA_ADMIN", round(float(admin), 2)),
            ]

            cs_sw = sc.base_cs_software + sc.cs_software_growth * i
            cs_train = sc.base_cs_training + sc.cs_training_growth * i
            cs_te = sc.base_cs_te + sc.cs_te_growth * i
            if scen_name == "actual" and y2025:
                cs_sw *= sc.actual_cs_software_mult
                cs_train *= sc.actual_cs_training_mult
            elif scen_name == "forecast" and y2025:
                cs_sw *= sc.forecast_cs_software_mult
            recs += [
                (month, "CS", "CS_SW", round(float(cs_sw), 2)),
                (month, "CS", "CS_TRAIN", round(float(cs_train), 2)),
                (month, "CS", "CS_TE", round(float(cs_te), 2)),
            ]

        return pd.DataFrame(recs, columns=["month", "department_id", "account_id", "amount"])

    return {s: make(s) for s in ["budget", "actual", "forecast"]}


def build_dataset(sc: Scenario | None = None) -> dict:
    """Build every table in memory for a scenario. No disk I/O."""
    sc = sc or DEFAULT
    rng = np.random.default_rng(sc.seed)

    dim_date = build_dim_date()
    dim_department = build_dim_department()
    dim_account = build_dim_account()
    headcount = build_headcount(sc, rng)
    saas = build_saas_metrics(sc, rng)
    facts = build_facts(sc, rng, dim_account, headcount, saas)

    fc = facts["forecast"]
    saas_fc = saas["forecast"]
    return {
        "dim_date": dim_date,
        "dim_department": dim_department,
        "dim_account": dim_account,
        "fact_headcount": headcount,
        "fact_actuals": facts["actual"],
        "fact_budget": facts["budget"],
        "fact_forecast": fc[fc["month"] >= "2025-01-01"],
        "fact_saas_metrics": saas["actual"],
        "fact_saas_metrics_budget": saas["budget"],
        "fact_saas_metrics_forecast": saas_fc[saas_fc["month"] >= "2025-01-01"],
    }


def main(sc: Scenario | None = None):
    os.makedirs(OUT_DIR, exist_ok=True)
    writes = build_dataset(sc)
    for name, df in writes.items():
        path = os.path.join(OUT_DIR, f"{name}.csv")
        df.to_csv(path, index=False)
        print(f"wrote {name:28s} rows={len(df):5d} -> {os.path.relpath(path)}")


if __name__ == "__main__":
    main()
