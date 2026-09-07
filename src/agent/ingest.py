"""
ingest.py
=========
Loading a new period from uploaded CSVs, with the same refusal discipline the
rest of the tool applies to everything else.

WHY VALIDATION IS THE FEATURE
------------------------------
A blind append is a liability. If an upload can silently introduce an unknown
department, a duplicate period, or a column of text where amounts belong, then
every guarantee downstream -- the audit, the traced figures, the reconciled
bridges -- is resting on data nobody checked.

So ingestion refuses, loudly and specifically, and the refusal names the row and
the reason. "It rejected my file and told me exactly which column was wrong" is
a better demonstration than a file that loads without comment, and it is the
same behaviour the planner already has when a question falls outside its tools.

WHAT AN UPLOAD IS ALLOWED TO DO
--------------------------------
Add a period that does not yet exist. It may not modify one that does. That
restriction is deliberate: silently rewriting a closed month is how a reporting
tool loses the trust it spent the rest of its design earning, and there is no
audit trail here that would make it recoverable.

THE ORDER OF OPERATIONS
-----------------------
    parse -> validate -> PREVIEW -> approve -> merge

The preview is the point. It states exactly what will change -- which periods,
how many rows, which departments, what is missing and what that costs -- before
anything is committed. The same shape as the publication gate: nothing lands
without someone seeing what lands.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

#: The files an upload may contain. Actuals alone produce a partial period --
#: real, but without a budget there is no variance to report -- so the preview
#: says so rather than letting the user discover it in an empty package.
SCHEMAS = {
    "fact_actuals": ["month", "department_id", "account_id", "amount"],
    "fact_budget": ["month", "department_id", "account_id", "amount"],
    "fact_forecast": ["month", "department_id", "account_id", "amount"],
    "fact_headcount": ["month", "department_id", "actual_headcount",
                       "budget_headcount", "forecast_headcount",
                       "avg_comp_actual", "avg_comp_budget",
                       "avg_comp_forecast"],
    "fact_saas_metrics": ["month", "starting_arr", "new_arr", "expansion_arr",
                          "contraction_arr", "churned_arr", "ending_arr",
                          "customers_start", "new_customers",
                          "churned_customers", "customers_end", "arpa",
                          "logo_churn_rate", "nrr", "grr"],
    "fact_saas_metrics_budget": ["month", "starting_arr", "new_arr", "expansion_arr",
                          "contraction_arr", "churned_arr", "ending_arr",
                          "customers_start", "new_customers",
                          "churned_customers", "customers_end", "arpa",
                          "logo_churn_rate", "nrr", "grr"],
    "fact_saas_metrics_forecast": ["month", "starting_arr", "new_arr", "expansion_arr",
                          "contraction_arr", "churned_arr", "ending_arr",
                          "customers_start", "new_customers",
                          "churned_customers", "customers_end", "arpa",
                          "logo_churn_rate", "nrr", "grr"],
}

#: Without these two a period cannot produce a variance package at all.
ESSENTIAL = ("fact_actuals", "fact_budget")

NUMERIC = {
    "amount", "actual_headcount", "budget_headcount", "forecast_headcount",
    "avg_comp_actual", "avg_comp_budget", "avg_comp_forecast",
    "starting_arr", "new_arr", "expansion_arr", "contraction_arr",
    "churned_arr", "ending_arr", "customers_start", "new_customers",
    "churned_customers", "customers_end", "arpa", "logo_churn_rate",
    "nrr", "grr",
}

MAX_ROWS = 20_000


@dataclass
class Finding:
    """One reason an upload cannot be accepted, or one thing worth knowing."""

    level: str          # "error" | "warning"
    table: str
    message: str
    rows: list = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.level == "error"


@dataclass
class Preview:
    """Exactly what an upload would change, stated before it changes anything."""

    frames: dict = field(default_factory=dict)          # table -> DataFrame
    findings: list = field(default_factory=list)
    new_periods: list = field(default_factory=list)
    row_counts: dict = field(default_factory=dict)
    departments: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    missing_tables: list = field(default_factory=list)

    @property
    def errors(self) -> list:
        return [f for f in self.findings if f.blocking]

    @property
    def warnings(self) -> list:
        return [f for f in self.findings if not f.blocking]

    @property
    def acceptable(self) -> bool:
        return bool(self.frames) and not self.errors and bool(self.new_periods)


def _normalise_month(value) -> str | None:
    """Accept YYYY-MM or YYYY-MM-DD, return the canonical first-of-month key."""
    import re

    if value is None:
        return None
    m = re.match(r"^\s*(\d{4})-(\d{2})(?:-(\d{2}))?\s*$", str(value))
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{m.group(1)}-{m.group(2)}-01"


#: Tables whose columns are IDENTICAL, so the schema cannot tell them apart.
#: The filename is the only signal, and if it does not carry one the upload is
#: refused rather than guessed at -- loading budget rows as actuals would be a
#: silent, total corruption of every variance in the tool.
AMBIGUOUS = {
    "fact_actuals": ("actual", "act"),
    "fact_budget": ("budget", "plan", "bud", "bdgt"),
    "fact_forecast": ("forecast", "fcst", "fcast"),
    "fact_saas_metrics": ("saas", "arr", "metric"),
    "fact_saas_metrics_budget": ("saas budget", "arr budget", "saas plan",
                                 "metrics budget"),
    "fact_saas_metrics_forecast": ("saas forecast", "arr forecast",
                                   "metrics forecast"),
}


def classify(filename: str, header: list):
    """Identify which table a file is. Returns (table, reason) or (None, why).

    Columns are the contract wherever they are distinctive. But actuals,
    budget and forecast share one schema exactly, so for those the filename
    decides -- and a filename that says nothing is an error, not a default.
    Defaulting would mean a budget file silently becoming actuals.
    """
    cols = {c.strip().lower() for c in header}
    candidates = [t for t, expected in SCHEMAS.items() if set(expected) <= cols]
    if not candidates:
        return None, ("Columns do not match any table this tool accepts. "
                      "Expected one of: " + ", ".join(sorted(SCHEMAS)))

    if len(candidates) == 1:
        return candidates[0], "matched by columns"

    low = filename.lower().replace("-", " ").replace("_", " ")
    hits = [t for t in candidates
            if t in AMBIGUOUS and any(k in low for k in AMBIGUOUS[t])]
    # A file named "saas metrics budget" matches both the actuals-shaped and
    # the budget-shaped table. The most SPECIFIC hit wins, so a longer keyword
    # match beats a shorter one rather than the tie being an error.
    if len(hits) > 1:
        scored = sorted(
            hits,
            key=lambda t: max(len(k) for k in AMBIGUOUS[t] if k in low),
            reverse=True)
        top = max(len(k) for k in AMBIGUOUS[scored[0]] if k in low)
        if sum(1 for t in scored
               if max(len(k) for k in AMBIGUOUS[t] if k in low) == top) == 1:
            return scored[0], f"matched by filename ({filename})"
    if len(hits) == 1:
        return hits[0], f"matched by filename ({filename})"
    if len(hits) > 1:
        return None, (f"The filename '{filename}' mentions more than one of "
                      + ", ".join(h.replace("fact_", "") for h in hits)
                      + ". Rename it so it names exactly one.")
    return None, (
        f"'{filename}' has the columns shared by "
        + ", ".join(c.replace("fact_", "") for c in sorted(candidates))
        + ", which are identical. Put 'actuals', 'budget' or 'forecast' in the "
          "filename so the tool knows which it is.")


def _with_located_header(raw_df, expected: list, scan: int = 12):
    """Find the row that holds the column headers and reframe on it."""
    want = {c.lower() for c in expected}
    for i in range(min(scan, len(raw_df))):
        row = [str(v).strip().lower() for v in raw_df.iloc[i].tolist()]
        if want <= set(row):
            out = raw_df.iloc[i + 1:].copy()
            out.columns = [str(v).strip().lower() for v in raw_df.iloc[i]]
            return out.reset_index(drop=True)
    return None


def _expand_workbooks(files: list, p: "Preview") -> list:
    """Turn each .xlsx into one CSV-shaped entry per recognised sheet."""
    out = []
    for fname, raw in files:
        if not str(fname).lower().endswith((".xlsx", ".xlsm")):
            out.append((fname, raw))
            continue
        try:
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, dtype=str,
                                   header=None)
        except Exception as e:  # noqa: BLE001
            p.findings.append(Finding(
                "error", fname, f"Could not read the workbook: {e}"))
            continue

        found = 0
        for sheet, df in sheets.items():
            key = str(sheet).strip().lower().replace(" ", "_")
            if key not in SCHEMAS:
                continue
            # The header row is LOCATED, not assumed. The template carries a
            # purpose line and a REQUIRED marker above its headers, and a
            # hand-made sheet may have a title row or none at all -- reading
            # row 1 as the header turns the description into a column name and
            # the sheet is rejected for having the wrong columns.
            df = _with_located_header(df, SCHEMAS[key])
            if df is None:
                p.findings.append(Finding(
                    "error", f"{fname} / {sheet}",
                    "Could not find a header row containing: "
                    + ", ".join(SCHEMAS[key])))
                continue
            df = df.dropna(how="all")
            if df.empty:
                continue
            # Drop the template's example row. It is tagged in a marker column
            # outside every schema, so an unfilled template cannot load its own
            # illustration as data -- which it did before this existed.
            if EXAMPLE_MARKER in df.columns:
                df = df[~df[EXAMPLE_MARKER].astype(str).str.strip()
                        .str.upper().eq("EXAMPLE")]
                df = df.drop(columns=[EXAMPLE_MARKER])
                # Dropped AGAIN after the marker column is removed: the
                # template's instruction line lives in that column, so the row
                # is only all-empty once the column is gone. Without this it
                # survived as a blank row and was reported as a malformed
                # date -- a refusal caused by the instructions, not the data.
                df = df.dropna(how="all")
            if df.empty:
                continue
            out.append((f"{key}.csv", df.to_csv(index=False).encode()))
            found += 1

        if not found:
            recognised = any(str(sh).strip().lower().replace(" ", "_")
                             in SCHEMAS for sh in sheets)
            p.findings.append(Finding(
                "error", fname,
                "The template has not been filled in - every sheet contains "
                "only the example row. Replace row 5 on each sheet with your "
                "own data."
                if recognised else
                "No recognised sheets. Sheets must be named for the table "
                "they hold: " + ", ".join(sorted(SCHEMAS))
                + ". Download the template to get them."))
    return out


def parse_upload(files: list, existing_tables: dict) -> Preview:
    """Parse and validate uploaded CSVs against the current dataset.

    `files` is a sequence of (filename, bytes). `existing_tables` is the live
    table dict, used to check dimension membership and to reject periods that
    already exist.
    """
    p = Preview()
    if not files:
        p.findings.append(Finding("error", "-", "No files were uploaded."))
        return p

    known_depts = set(existing_tables["dim_department"]["department_id"])
    known_accts = set(existing_tables["dim_account"]["account_id"])
    existing_months = set(
        existing_tables["fact_actuals"]["month"].astype(str).map(
            lambda m: str(m)[:10]))

    seen_periods: set = set()

    # A workbook is expanded into one pseudo-file per sheet, with the SHEET
    # name carrying the table identity. That removes the ambiguity loose CSVs
    # have to resolve by filename: actuals, budget and forecast share one
    # schema exactly, so a file called "close final.csv" cannot be placed while
    # a sheet called fact_budget always can.
    files = _expand_workbooks(files, p)

    for fname, raw in files:
        try:
            text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError:
            p.findings.append(Finding(
                "error", fname,
                "The file is not UTF-8 text. Re-export it as CSV UTF-8."))
            continue

        try:
            df = pd.read_csv(io.StringIO(text), dtype=str,
                             keep_default_na=False)
        except Exception as e:  # noqa: BLE001
            p.findings.append(Finding("error", fname,
                                      f"Could not read as CSV: {e}"))
            continue

        df.columns = [c.strip().lower() for c in df.columns]
        table, why = classify(fname, list(df.columns))
        if table is None:
            p.findings.append(Finding("error", fname, why))
            continue
        if table in p.frames:
            p.findings.append(Finding(
                "error", table,
                f"Two files both look like {table}. Upload one of each."))
            continue

        expected = SCHEMAS[table]
        missing = [c for c in expected if c not in df.columns]
        if missing:
            p.findings.append(Finding(
                "error", table, f"Missing column(s): {', '.join(missing)}"))
            continue

        df = df[expected].copy()
        if df.empty:
            p.findings.append(Finding("error", table, "The file has no rows."))
            continue
        if len(df) > MAX_ROWS:
            p.findings.append(Finding(
                "error", table,
                f"{len(df):,} rows exceeds the {MAX_ROWS:,}-row limit."))
            continue

        # -- month ------------------------------------------------------
        canon = df["month"].map(_normalise_month)
        bad = df.index[canon.isna()].tolist()
        if bad:
            p.findings.append(Finding(
                "error", table,
                f"{len(bad)} row(s) have a month that is not YYYY-MM or "
                f"YYYY-MM-DD.", [int(i) + 2 for i in bad[:6]]))
            continue
        df["month"] = canon

        clash = sorted(set(df["month"]) & existing_months)
        if clash:
            p.findings.append(Finding(
                "error", table,
                "These periods already exist and an upload may not modify a "
                "closed month: " + ", ".join(m[:7] for m in clash)))
            continue
        seen_periods |= set(df["month"])

        # -- dimension membership ---------------------------------------
        if "department_id" in df.columns:
            df["department_id"] = df["department_id"].str.strip()
            unknown = sorted(set(df["department_id"]) - known_depts)
            if unknown:
                p.findings.append(Finding(
                    "error", table,
                    f"Unknown department(s): {', '.join(unknown[:6])}. Known: "
                    + ", ".join(sorted(known_depts))))
                continue
        if "account_id" in df.columns:
            df["account_id"] = df["account_id"].str.strip()
            unknown = sorted(set(df["account_id"]) - known_accts)
            if unknown:
                p.findings.append(Finding(
                    "error", table,
                    f"Unknown account(s): {', '.join(unknown[:6])}. This tool "
                    "does not create accounts from an upload."))
                continue

        # -- numerics ---------------------------------------------------
        numeric_cols = [c for c in df.columns if c in NUMERIC]
        failed = False
        for col in numeric_cols:
            cleaned = (df[col].astype(str)
                       .str.replace(",", "", regex=False)
                       .str.replace("$", "", regex=False)
                       .str.strip()
                       .replace({"": None, "-": None}))
            # Accounting parentheses are a negative, not a parse error: a
            # finance export writes (1,234) and means -1234.
            neg = cleaned.fillna("").str.match(r"^\(.*\)$")
            cleaned = cleaned.fillna("").str.strip("()")
            converted = pd.to_numeric(cleaned, errors="coerce")
            converted = converted.where(~neg, -converted)

            bad = df.index[converted.isna()].tolist()
            if bad:
                p.findings.append(Finding(
                    "error", table,
                    f"Column '{col}' has {len(bad)} value(s) that are not "
                    f"numbers.", [int(i) + 2 for i in bad[:6]]))
                failed = True
                break
            df[col] = converted
        if failed:
            continue

        # -- duplicates --------------------------------------------------
        keys = [c for c in ("month", "department_id", "account_id")
                if c in df.columns]
        dupes = df.duplicated(subset=keys, keep=False)
        if dupes.any():
            p.findings.append(Finding(
                "error", table,
                f"{int(dupes.sum())} duplicate row(s) for the same "
                + " + ".join(keys) + ". Aggregate before uploading.",
                [int(i) + 2 for i in df.index[dupes][:6]]))
            continue

        p.frames[table] = df
        p.row_counts[table] = len(df)
        if "department_id" in df.columns:
            p.departments = sorted(set(p.departments) | set(df["department_id"]))
        if "account_id" in df.columns:
            p.accounts = sorted(set(p.accounts) | set(df["account_id"]))

    p.new_periods = sorted(seen_periods)
    p.missing_tables = [t for t in SCHEMAS if t not in p.frames]

    if p.frames:
        if len(p.new_periods) > 1:
            p.findings.append(Finding(
                "warning", "-",
                f"The upload covers {len(p.new_periods)} periods: "
                + ", ".join(m[:7] for m in p.new_periods)))
        for t in ESSENTIAL:
            if t not in p.frames:
                p.findings.append(Finding(
                    "warning", t,
                    f"No {t.replace('fact_', '')} supplied. Without it the "
                    "period loads but cannot show a variance against plan."))
        # Each optional file is named with the CONSEQUENCE of leaving it out,
        # because "no SaaS metrics budget" means nothing to a reader while
        # "the revenue volume/price split will be unavailable" is actionable.
        CONSEQUENCE = {
            "fact_headcount": "headcount versus plan and the compensation "
                              "split will be unavailable",
            "fact_saas_metrics": "the ARR bridge will be unavailable",
            "fact_saas_metrics_budget": "the revenue volume-versus-price split "
                                        "will be unavailable, and the agent "
                                        "will refuse to produce a complete "
                                        "package for this period",
            "fact_forecast": "actual-versus-forecast comparisons will be "
                             "unavailable",
            "fact_saas_metrics_forecast": "forecast retention will be "
                                          "unavailable",
        }
        for t, consequence in CONSEQUENCE.items():
            if t not in p.frames:
                p.findings.append(Finding(
                    "warning", t,
                    f"No {t.replace('fact_', '').replace('_', ' ')} supplied \u2014 "
                    f"{consequence}."))

        # A GAP is warned about, not refused. Loading January and then March
        # leaves February missing, and every month-over-month and
        # trailing-window figure then silently spans the hole -- but the data
        # is not wrong, only incomplete, and refusing would stop someone who
        # genuinely only has those two months. So it is surfaced loudly and
        # left as the uploader's call.
        if existing_months and seen_periods:
            latest = max(existing_months)
            gap = _missing_between(latest, min(seen_periods))
            if gap:
                p.findings.append(Finding(
                    "warning", "-",
                    f"This leaves {len(gap)} period(s) missing between "
                    f"{latest[:7]} and {min(seen_periods)[:7]}: "
                    + ", ".join(m[:7] for m in gap)
                    + ". Month-over-month and trailing-window figures will "
                      "span the gap. Load the missing month(s) first if you "
                      "have them."))

        # Periods must move forward, or an upload would rewrite history.
        if existing_months:
            latest = max(existing_months)
            for m in p.new_periods:
                if m <= latest:
                    p.findings.append(Finding(
                        "error", "-",
                        f"{m[:7]} is earlier than the last existing period "
                        f"({latest[:7]}). Uploads extend the dataset forward."))
    return p


def _missing_between(latest: str, first_new: str) -> list:
    """Months absent between the last existing period and the first new one."""
    ly, lm = int(latest[:4]), int(latest[5:7])
    ny, nm = int(first_new[:4]), int(first_new[5:7])
    out = []
    y, m = ly, lm
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if (y, m) >= (ny, nm):
            break
        out.append(f"{y:04d}-{m:02d}-01")
    return out


def merge(existing_tables: dict, preview: Preview) -> dict:
    """Return a NEW table dict with the uploaded periods added.

    The existing frames are never mutated: a caller holding the previous
    dataset keeps it, which is what makes an upload reversible by simply
    discarding the result.
    """
    if not preview.acceptable:
        raise ValueError("this upload was not accepted; merge is not permitted")

    merged = {k: v.copy() for k, v in existing_tables.items()}

    for table, df in preview.frames.items():
        if table not in merged:
            merged[table] = df.copy()
            continue
        base = merged[table]
        aligned = df.reindex(columns=base.columns)
        merged[table] = pd.concat([base, aligned], ignore_index=True)

    merged["dim_date"] = _extend_dim_date(merged["dim_date"],
                                          preview.new_periods)
    return merged


def _extend_dim_date(dim_date: pd.DataFrame, periods: list) -> pd.DataFrame:
    """Add the uploaded periods to the date dimension.

    Without this every join against dim_date drops the new month and the
    period appears to have loaded while showing nothing -- the silent-empty
    failure this codebase keeps having to design against.
    """
    existing = set(dim_date["month"].astype(str).map(lambda m: str(m)[:10]))
    rows = []
    for m in periods:
        if m in existing:
            continue
        year, month = int(m[:4]), int(m[5:7])
        rows.append({
            "month": m,
            "quarter": f"{year}-Q{((month - 1) // 3) + 1}",
            "fiscal_year": year,
            "month_number": month,
            "is_actual_period": True,
            "is_budget_period": True,
            "is_forecast_period": False,
        })
    if not rows:
        return dim_date
    out = pd.concat([dim_date, pd.DataFrame(rows)], ignore_index=True)
    return out.sort_values("month").reset_index(drop=True)


def summarise(preview: Preview) -> str:
    """One line for a log or a caption."""
    if not preview.frames:
        return "nothing accepted"
    return (f"{sum(preview.row_counts.values()):,} rows across "
            f"{len(preview.frames)} table(s), adding "
            + ", ".join(m[:7] for m in preview.new_periods))


# ==========================================================================
# the template workbook
# ==========================================================================
#: Sheets are named for the table they fill, which removes the ambiguity that
#: filenames have to resolve for loose CSVs: actuals, budget and forecast share
#: one schema exactly, so a file called "close final.csv" cannot be placed. A
#: named sheet always can.
TEMPLATE_SHEETS = [
    ("fact_actuals", "Actual results for the period. One row per department "
                     "and account.", True),
    ("fact_budget", "The plan for the same period. Without it there is no "
                    "variance to report.", True),
    ("fact_headcount", "Headcount and average compensation by department.",
     False),
    ("fact_saas_metrics", "Actual ARR movement for the period. One row.",
     False),
    ("fact_saas_metrics_budget", "Planned ARR movement. Needed for the "
                                 "revenue volume-versus-price split.", False),
    ("fact_forecast", "Optional. Enables actual-versus-forecast comparisons.",
     False),
]

FONT = "Arial"

#: A marker column on the template's example row. Without it, an unfilled
#: template uploads cleanly and creates a period containing one invented row --
#: the example silently becoming data, which is the worst outcome an example
#: can have. The column sits outside every schema, so it is ignored on a real
#: upload and cannot collide with a genuine field.
EXAMPLE_MARKER = "delete_this_example_row"


def build_template(tables: dict, period: str | None = None) -> bytes:
    """An Excel workbook shaped exactly as this tool reads it.

    One sheet per table, named for the table, plus an instruction sheet and a
    reference sheet listing every valid department and account id. Each sheet
    carries ONE example row in the fill-in colour, showing the expected format
    rather than describing it -- a reader copies a row far more reliably than
    they interpret a schema.

    The example row is deliberately obvious ("EXAMPLE - delete this row") so it
    cannot be mistaken for data. An upload that still contained it would be
    caught anyway, because its month is not a real period.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    if period is None:
        latest = max(tables["fact_actuals"]["month"].astype(str))
        year, month = int(latest[:4]), int(latest[5:7])
        period = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"

    ink = "0F172A"
    head_fill = PatternFill("solid", fgColor=ink)
    fill_in = PatternFill("solid", fgColor="FFF9C4")     # cells to complete
    ref_fill = PatternFill("solid", fgColor="F1F5F9")
    thin = Side(style="thin", color="CBD5E1")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    # -- instructions --------------------------------------------------
    ws = wb.active
    ws.title = "Instructions"
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 34
    # 82 was wide enough that the column fell off the printed page: every
    # instruction was present in the cell and invisible on paper.
    ws.column_dimensions["C"].width = 62

    def line(row, label, text, bold=False, size=11, colour=ink):
        ws.cell(row=row, column=2, value=label).font = Font(
            name=FONT, bold=True, size=size, color=colour)
        c = ws.cell(row=row, column=3, value=text)
        c.font = Font(name=FONT, size=size, bold=bold, color=colour)
        c.alignment = Alignment(wrap_text=True, vertical="top")

    ws.cell(row=2, column=2, value="Load a period").font = Font(
        name=FONT, bold=True, size=18, color=ink)
    ws.cell(row=3, column=2,
            value=f"Template for {period}").font = Font(
        name=FONT, size=12, color="64748B")

    rows = [
        ("How to use this", "Fill in one sheet per table below, then upload "
                            "this whole workbook. Sheet names identify the "
                            "table, so nothing depends on what you call the "
                            "file."),
        ("Yellow cells", "The example row on each sheet is shaded yellow. "
                         "Delete it and put your own rows in its place, "
                         "keeping the header row exactly as it is."),
        ("Month format", f"Use {period} or {period}-01. Every row on a sheet "
                         "should be the same month."),
        ("Amounts", "Plain numbers, or $ and accounting parentheses - "
                    "$1,234.00 and (1,234.00) are both read correctly. "
                    "Parentheses mean negative."),
        ("Departments and accounts", "Must already exist. See the 'Valid "
                                     "codes' sheet. An upload adds a period, "
                                     "never a new account."),
        ("What is required", "fact_actuals and fact_budget. Without both, the "
                             "period loads but has no variance to report. The "
                             "others are optional and each sheet says what it "
                             "costs to omit."),
        ("It will refuse", "A period that already exists, an unknown code, a "
                           "non-numeric amount, a bad month, or duplicate "
                           "rows. The tool names the sheet, the reason and "
                           "the row."),
        ("Synthetic data only", "Never upload anything derived from an "
                                "employer. This tool is a portfolio "
                                "demonstration built on synthetic data."),
    ]
    for i, (label, text) in enumerate(rows, start=5):
        line(i * 2 - 5, label, text)
        ws.row_dimensions[i * 2 - 5].height = 42

    # -- one sheet per table -------------------------------------------
    dept = str(tables["dim_department"]["department_id"].iloc[0])
    acct_df = tables["dim_account"]
    acct = str(acct_df["account_id"].iloc[0])

    for name, purpose, required in TEMPLATE_SHEETS:
        sh = wb.create_sheet(name)
        sh.cell(row=1, column=1, value=purpose).font = Font(
            name=FONT, size=10, italic=True, color="64748B")
        sh.cell(row=2, column=1,
                value=("REQUIRED" if required else "Optional")).font = Font(
            name=FONT, size=10, bold=True,
            color="B91C1C" if required else "64748B")

        cols = SCHEMAS[name]
        for j, col in enumerate(cols, start=1):
            c = sh.cell(row=4, column=j, value=col)
            c.font = Font(name=FONT, bold=True, size=10, color="FFFFFF")
            c.fill = head_fill
            c.border = box
            sh.column_dimensions[get_column_letter(j)].width = max(
                14, min(26, len(col) + 4))

        marker = sh.cell(row=4, column=len(cols) + 2, value=EXAMPLE_MARKER)
        marker.font = Font(name=FONT, bold=True, size=10, color="FFFFFF")
        marker.fill = PatternFill("solid", fgColor="B91C1C")
        sh.column_dimensions[get_column_letter(len(cols) + 2)].width = 26
        m = sh.cell(row=5, column=len(cols) + 2, value="EXAMPLE")
        m.font = Font(name=FONT, bold=True, size=10, color="B91C1C")
        m.fill = fill_in

        example = _example_row(name, period, dept, acct)
        for j, col in enumerate(cols, start=1):
            c = sh.cell(row=5, column=j, value=example.get(col))
            c.font = Font(name=FONT, size=10, color=ink)
            c.fill = fill_in
            c.border = box
            if col in NUMERIC:
                c.number_format = "#,##0.00"
        # The note goes in the MARKER column, not column A. In column A it sat
        # under "month", survived example-stripping, and was then reported as a
        # malformed date -- a confusing refusal caused by the instructions
        # rather than by the data.
        note = sh.cell(row=6, column=len(cols) + 2,
                       value="delete row 5, then enter your rows")
        note.font = Font(name=FONT, size=9, italic=True, color="B91C1C")
        sh.freeze_panes = "A5"

    # -- valid codes ----------------------------------------------------
    ref = wb.create_sheet("Valid codes")
    ref.column_dimensions["A"].width = 22
    ref.column_dimensions["B"].width = 34
    ref.column_dimensions["D"].width = 22
    ref.column_dimensions["E"].width = 34
    ref.cell(row=1, column=1, value="Departments").font = Font(
        name=FONT, bold=True, size=12, color=ink)
    ref.cell(row=1, column=4, value="Accounts").font = Font(
        name=FONT, bold=True, size=12, color=ink)

    for j, (col, label) in enumerate([(1, "department_id"), (2, "name")]):
        c = ref.cell(row=2, column=col, value=label)
        c.font = Font(name=FONT, bold=True, size=10, color="FFFFFF")
        c.fill = head_fill
    for i, row in enumerate(tables["dim_department"].itertuples(), start=3):
        ref.cell(row=i, column=1, value=str(row.department_id)).font = Font(
            name=FONT, size=10)
        ref.cell(row=i, column=2,
                 value=str(getattr(row, "department_name", ""))).font = Font(
            name=FONT, size=10)
        ref.cell(row=i, column=1).fill = ref_fill

    for j, (col, label) in enumerate([(4, "account_id"), (5, "name")]):
        c = ref.cell(row=2, column=col, value=label)
        c.font = Font(name=FONT, bold=True, size=10, color="FFFFFF")
        c.fill = head_fill
    name_col = "account_name" if "account_name" in acct_df.columns else None
    for i, row in enumerate(acct_df.itertuples(), start=3):
        ref.cell(row=i, column=4, value=str(row.account_id)).font = Font(
            name=FONT, size=10)
        if name_col:
            ref.cell(row=i, column=5,
                     value=str(getattr(row, name_col, ""))).font = Font(
                name=FONT, size=10)
        ref.cell(row=i, column=4).fill = ref_fill

    # Fit every sheet to one page wide. Without it the instruction column and
    # the wider data sheets paginate sideways, so a printed template loses
    # exactly the guidance it exists to carry.
    for sheet in wb.worksheets:
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _example_row(table: str, period: str, dept: str, acct: str) -> dict:
    """One realistic row per sheet. Shows the format instead of describing it."""
    if table in ("fact_actuals", "fact_budget", "fact_forecast"):
        return {"month": period, "department_id": dept, "account_id": acct,
                "amount": 125000.00}
    if table == "fact_headcount":
        return {"month": period, "department_id": dept,
                "actual_headcount": 12, "budget_headcount": 11,
                "forecast_headcount": 12, "avg_comp_actual": 11500.00,
                "avg_comp_budget": 11250.00, "avg_comp_forecast": 11500.00}
    return {"month": period, "starting_arr": 28800000.00, "new_arr": 480000.00,
            "expansion_arr": 175000.00, "contraction_arr": 95000.00,
            "churned_arr": 250000.00, "ending_arr": 29110000.00,
            "customers_start": 940, "new_customers": 22,
            "churned_customers": 8, "customers_end": 954, "arpa": 30500.00,
            "logo_churn_rate": 0.0085, "nrr": 1.021, "grr": 0.905}
