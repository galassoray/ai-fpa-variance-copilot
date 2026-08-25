"""
briefing.py
===========
The "where to look" layer: a ranked, computed account of where the money
actually moved, assembled entirely from ledger rows.

THE DISTINCTION THIS MODULE EXISTS TO HOLD
-------------------------------------------
An agent that says *"cut paid marketing 15% next quarter"* has asserted
something no tool returned. It cannot be traced, cannot be audited, and the
publication gate would pass it through untouched -- every guardrail in this
system is built to catch a wrong *number*, and a recommendation is not a number.

We have already watched the milder version of that failure: a model wrote
"Operating Expenses ... Corporate / Company ($76,065.38 over budget)" when CORP
holds no opex and the sign was inverted. Every figure passed the audit because
every magnitude was real. Prescriptive advice is that failure mode promoted to
a feature.

So this module answers a different question, and answers it in code:

    not  "what should the company do?"          (unverifiable)
    but  "where did the money actually move,
          how much, and what is underneath it?"  (every part retrieved)

That is the useful half. An analyst reading "S&M salaries are $67.1K over plan;
$48.3K of that is headcount at 60 heads against a plan of 56, $18.7K is rate"
knows exactly what to do next. The tool got them there in seconds with every
figure traceable, and left the judgment where it belongs.

WHAT IS COMPUTED HERE, AND WHAT IS NOT
---------------------------------------
Computed: the ranking, the materiality cut, and which detail attaches to which
driver. Those are selection decisions, and selection is code's job.

NOT computed: any figure that reaches a screen. Every displayed number is a
value a SQL tool returned, carried through by reference with its provenance.
The cumulative share used to decide the materiality cut is deliberately used
only for *selection* and never displayed -- publishing it would mean publishing
a number this module derived, which is precisely the boundary being defended.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Include drivers until this share of total absolute impact is covered, then
#: stop. A Pareto cut rather than a fixed count: a month with one dominant
#: driver should surface one, not pad to five.
COVERAGE_TARGET = 0.80

#: Never surface more than this, regardless of coverage. A "where to look"
#: list of ten items is not a prioritisation.
MAX_AREAS = 4

#: Below this share, a driver is noise for the purposes of a briefing. It still
#: appears in the full package -- this governs what gets called out.
MIN_SHARE = 0.03


@dataclass
class Evidence:
    """One retrieved fact supporting a focus area."""

    label: str
    value: float
    kind: str            # "money" | "percent" | "count" | "count_signed"
    section: str
    step: int


@dataclass
class FocusArea:
    rank: int
    name: str
    member: str
    oi_impact: float
    share: float | None
    direction: str                      # "unfavorable" | "favorable"
    grain: str
    context: list = field(default_factory=list)     # basis-free counts
    detail: list = field(default_factory=list)      # account level, OI basis
    # Supporting groups are kept SEPARATE rather than flattened into one list,
    # because they are stated on different bases. Account detail is on an
    # operating-income basis (negative = unfavourable); compensation variance
    # is on an expense basis (positive = spent more than plan). Printing
    # "Salaries ($67,061)" beside "salary variance $67,061" in one block is the
    # same sign confusion that produced a wrong claim earlier in this build, so
    # each group carries its own basis label.
    comp: list = field(default_factory=list)        # expense basis
    headcount: list = field(default_factory=list)   # counts
    revenue_split: list = field(default_factory=list)   # OI basis
    section: str = ""
    step: int = 0

    @property
    def headline(self) -> str:
        sign = "(" + f"${abs(self.oi_impact):,.0f}" + ")" \
            if self.oi_impact < 0 else f"${self.oi_impact:,.0f}"
        return f"{self.name} {sign} {self.direction}"


@dataclass
class Briefing:
    period: str
    comparison: str
    areas: list = field(default_factory=list)
    total_drivers_considered: int = 0
    #: What the ranking is measured on. Stated rather than assumed, because a
    #: month-over-month ranking and a versus-plan ranking answer different
    #: questions and a reader must not have to infer which one they are seeing.
    basis: str = "impact on operating income versus plan"
    note: str = ""

    @property
    def available(self) -> bool:
        return bool(self.areas)


def _sections_by_tool(result, tool: str) -> list:
    return [(name, sec) for name, sec in
            sorted(result.sections.items(), key=lambda kv: kv[1]["step"])
            if sec["tool"] == tool and sec["rows"]]


def _decomposition_for(result, member: str):
    """The account-level drill-down for one department, if the plan pulled it."""
    for name, sec in _sections_by_tool(result, "decompose_variance"):
        if sec.get("params", {}).get("department_id") == member:
            return name, sec
    return None, None


def _row_for(result, tool: str, key: str, member: str):
    for name, sec in _sections_by_tool(result, tool):
        for row in sec["rows"]:
            if row.get(key) == member:
                return name, sec, row
    return None, None, None


def build_briefing(result, goal: dict) -> Briefing:
    """Rank where the money moved, and attach what explains each one.

    Returns an empty briefing when the plan never ranked anything -- a briefing
    that invents priorities from a plan that did not look for them would be the
    same failure this module exists to avoid, one level up.
    """
    b = Briefing(period=goal.get("period", ""),
                 comparison=goal.get("comparison", "actual_vs_budget"))

    # Any RANKING tool can seed a briefing, not just this-month-versus-budget.
    # The first version only looked for rank_variance_drivers, so a
    # month-over-month or persistence question ran successfully and then
    # reported "nothing to prioritise" -- the briefing refusing to summarise a
    # run that had produced exactly the ranking it needed.
    #
    # The impact field differs per tool, so it is declared rather than guessed:
    # compare_periods ranks on the change, persistence on the cumulative total.
    RANKERS = [
        ("rank_variance_drivers", "oi_impact",
         "impact on operating income versus plan"),
        ("rank_persistent_drivers", "cumulative_oi_impact",
         "cumulative impact over the window"),
        ("rank_mom_movers", "oi_impact_of_change",
         "impact of the change since last month"),
        ("compare_periods", "oi_impact_of_change",
         "impact of the change between the two periods"),
    ]

    ranked = None
    for tool, impact_field, basis in RANKERS:
        preferred = [(n, sec) for n, sec in _sections_by_tool(result, tool)
                     if sec.get("params", {}).get("dimension") == "department"]
        found = preferred or _sections_by_tool(result, tool)
        if found:
            ranked = (found[0][0], found[0][1], impact_field, basis)
            break

    if ranked is None:
        b.note = ("This run did not rank anything, so there is nothing to "
                  "prioritise. A ranking is what makes a briefing possible.")
        return b

    name, sec, impact_field, basis = ranked
    b.basis = basis
    rows = sec["rows"]
    b.total_drivers_considered = len(rows)
    grain = sec.get("params", {}).get("dimension", "department")

    covered = 0.0
    for row in rows:
        share = row.get("share_of_total_oi_impact")
        if len(b.areas) >= MAX_AREAS:
            break
        if share is not None and share < MIN_SHARE and b.areas:
            break
        if row.get(impact_field) is None:
            continue

        area = FocusArea(
            rank=len(b.areas) + 1,
            name=str(row.get("name") or row.get("member")),
            member=str(row.get("member")),
            oi_impact=row[impact_field],
            share=share,
            # `favorable` exists on the versus-plan tools but NOT on the
            # comparative ones, so reading it directly labelled every
            # month-over-month row "unfavorable" -- including revenue that had
            # risen. Direction follows the impact SIGN, which is defined for
            # every ranking tool; the flag is used only to confirm it.
            direction=("favorable" if row[impact_field] > 0 else "unfavorable"),
            grain=grain,
            section=name,
            step=sec["step"],
        )
        _attach(result, area)
        b.areas.append(area)

        # Cumulative coverage decides where to stop. Used for SELECTION only
        # and never displayed: publishing it would mean publishing a figure
        # this module derived rather than one a tool returned.
        if share is not None:
            covered += share
            if covered >= COVERAGE_TARGET:
                break

    # Account names repeat across departments -- every team has "Salaries" --
    # so two rows reading "Salaries ($13,069)" and "Salaries ($9,503)" is
    # ambiguous in exactly the place a reader is deciding where to look.
    seen: dict = {}
    for a in b.areas:
        seen.setdefault(a.name, []).append(a)
    for name, group in seen.items():
        if len(group) > 1:
            for a in group:
                a.name = f"{name} ({a.member})"

    return b


def _attach(result, area: FocusArea) -> None:
    """Hang every retrieved fact that explains this driver onto it."""
    for name, sec in _sections_by_tool(result, "rank_persistent_drivers"):
        for row in sec["rows"]:
            if row.get("member") != area.member:
                continue
            for label, key, kind in (("months unfavorable",
                                      "months_unfavorable", "count"),
                                     ("months observed",
                                      "months_observed", "count")):
                if row.get(key) is not None:
                    area.context.append(Evidence(label=label, value=row[key],
                                                 kind=kind, section=name,
                                                 step=sec["step"]))

    if area.grain != "department":
        return

    dname, dsec = _decomposition_for(result, area.member)
    if dsec is not None:
        for row in dsec["rows"][:4]:
            if row.get("oi_impact") is None:
                continue
            area.detail.append(Evidence(
                label=str(row.get("account_name") or row.get("account_id")),
                value=row["oi_impact"], kind="money",
                section=dname, step=dsec["step"]))

    cname, csec, crow = _row_for(result, "get_comp_decomposition",
                                 "department_id", area.member)
    if crow is not None and crow.get("salary_variance") is not None:
        for label, key in (("salary variance", "salary_variance"),
                           ("headcount effect", "hc_impact"),
                           ("rate effect", "rate_impact")):
            if crow.get(key) is not None:
                area.comp.append(Evidence(label=label, value=crow[key],
                                          kind="money", section=cname,
                                          step=csec["step"]))

    hname, hsec, hrow = _row_for(result, "get_headcount_movement",
                                 "department_id", area.member)
    if hrow is not None:
        for label, key, kind in (("actual", "actual_headcount", "count"),
                                 ("plan", "budget_headcount", "count"),
                                 ("versus plan", "hc_var_vs_budget",
                                  "count_signed")):
            if hrow.get(key) is not None:
                area.headcount.append(Evidence(label=label, value=hrow[key],
                                               kind=kind, section=hname,
                                               step=hsec["step"]))

    rname, rsec, _ = _row_for(result, "get_revenue_decomposition", "month",
                              result.ledger.goal.get("period"))
    if rsec is not None and any(e.label == "Subscription Revenue"
                                for e in area.detail):
        row = rsec["rows"][0]
        for label, key in (("volume effect", "volume_impact"),
                           ("price effect", "price_impact")):
            if row.get(key) is not None:
                area.revenue_split.append(Evidence(label=label, value=row[key],
                                                   kind="money", section=rname,
                                                   step=rsec["step"]))


def fmt(value: float, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "percent":
        return f"{value * 100:.1f}%"
    if kind == "count":
        return f"{int(value):,}"
    if kind == "count_signed":
        return f"{int(value):+,}"
    body = f"${abs(value):,.0f}"
    return f"({body})" if value < 0 else body


def render_briefing(b: Briefing) -> str:
    """Plain-text briefing, for the CLI."""
    out = ["", "=" * 74, " WHERE TO LOOK", "=" * 74]
    if not b.available:
        out.append(f"\n  {b.note}")
        return "\n".join(out)

    out.append(f"\n  Ranked by {b.basis}. Every figure below is a tool return")
    out.append("  value. The tool does not recommend an action -- it shows "
               "where the money")
    out.append("  moved and what is underneath it.\n")

    for a in b.areas:
        out.append(f"  {a.rank}. {a.headline}"
                   + (f"  ·  {fmt(a.share, 'percent')} of total impact"
                      if a.share is not None else ""))
        if a.detail:
            out.append("       account detail (impact on operating income)")
            for e in a.detail:
                out.append(f"         {e.label:<28}{fmt(e.value, e.kind):>14}")
        if a.revenue_split:
            bits = "   ".join(f"{e.label} {fmt(e.value, e.kind)}"
                              for e in a.revenue_split)
            out.append(f"       revenue split (OI basis):  {bits}")
        if a.comp:
            bits = "   ".join(f"{e.label} {fmt(e.value, e.kind)}"
                              for e in a.comp)
            out.append(f"       compensation (expense basis, + = spent above "
                       f"plan):")
            out.append(f"         {bits}")
        if a.headcount:
            bits = "   ".join(f"{e.label} {fmt(e.value, e.kind)}"
                              for e in a.headcount)
            out.append(f"       headcount:  {bits}")
        if a.context:
            bits = "   ".join(f"{e.label} {fmt(e.value, e.kind)}"
                              for e in a.context)
            out.append(f"       persistence:  {bits}")
        out.append("")
    return "\n".join(out)


def narrative_from_briefing(b: Briefing) -> str:
    """A deterministic narrative built from the briefing.

    The copilot's ``injection_narrative`` reads a fixed schema -- revenue versus
    budget, top drivers, the ARR bridge -- and produces nothing for a run that
    filled none of those slots. So a month-over-month question executed
    perfectly and then fell back to empty prose.

    This covers the gap using material the briefing has already assembled:
    every figure is a ledger value that was carried here by reference, so the
    audit verifies it exactly as it would model-written text. It states no
    recommendation, for the same reason the briefing does not.
    """
    if not b.available:
        return ""

    out = [f"Ranked by {b.basis} for {b.period[:7]}."]
    for a in b.areas:
        parts = [f"{a.name} was {fmt(a.oi_impact, 'money')} {a.direction}"]
        if a.share is not None:
            parts.append(f", {fmt(a.share, 'percent')} of the total")
        line = "".join(parts) + "."

        if a.detail:
            bits = ", ".join(f"{e.label} {fmt(e.value, e.kind)}"
                             for e in a.detail[:3])
            line += f" The largest account movements were {bits}."
        if a.comp:
            lookup = {e.label: e for e in a.comp}
            if {"salary variance", "headcount effect", "rate effect"} <= set(lookup):
                line += (
                    f" Salary variance of "
                    f"{fmt(lookup['salary variance'].value, 'money')} on an "
                    f"expense basis splits into "
                    f"{fmt(lookup['headcount effect'].value, 'money')} from "
                    f"headcount and "
                    f"{fmt(lookup['rate effect'].value, 'money')} from rate.")
        if a.headcount:
            lookup = {e.label: e for e in a.headcount}
            if {"actual", "plan"} <= set(lookup):
                line += (f" Headcount was {fmt(lookup['actual'].value, 'count')} "
                         f"against a plan of "
                         f"{fmt(lookup['plan'].value, 'count')}.")
        if a.context:
            lookup = {e.label: e for e in a.context}
            if {"months unfavorable", "months observed"} <= set(lookup):
                line += (
                    f" It was unfavourable in "
                    f"{fmt(lookup['months unfavorable'].value, 'count')} of "
                    f"{fmt(lookup['months observed'].value, 'count')} months "
                    f"in the window.")
        if a.revenue_split:
            lookup = {e.label: e for e in a.revenue_split}
            if {"volume effect", "price effect"} <= set(lookup):
                line += (f" The revenue movement splits into "
                         f"{fmt(lookup['volume effect'].value, 'money')} of "
                         f"volume effect and "
                         f"{fmt(lookup['price effect'].value, 'money')} of "
                         f"price effect.")
        out.append(line)
    return " ".join(out)
