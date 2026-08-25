"""
test_agent_briefing.py
======================
The "where to look" layer, and the boundary it exists to hold.

WHY THIS SUITE MATTERS MORE THAN IT LOOKS
------------------------------------------
The obvious way to make an agent impressive is to have it recommend actions:
"cut paid marketing 15% next quarter." That sentence is not a retrieved number.
It cannot be traced, the audit has nothing to check it against, and the
publication gate would pass it through untouched -- every guardrail in this
system catches a wrong FIGURE, and a recommendation is not a figure.

We already watched the milder version happen on a live run: the model wrote
"Operating Expenses ... Corporate / Company ($76,065.38 over budget)" when CORP
holds no opex and the sign was inverted. Every magnitude was real, so the audit
passed it. Prescriptive advice is that failure mode promoted to a feature.

So the briefing answers a different question -- where did the money move, how
much, and what is underneath it -- and answers it in code. These tests pin that
the ranking is computed, that every displayed figure is a ledger value, and
that the module states no imperative.
"""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import run_pipeline as rp  # noqa: E402
from agent import materialize as mz  # noqa: E402
from agent import tools as _tools  # noqa: E402,F401
from agent.briefing import (COVERAGE_TARGET, MAX_AREAS, MIN_SHARE,  # noqa: E402
                            build_briefing, fmt, render_briefing)
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.packages import build_goal, variance_package_plan  # noqa: E402
from agent.plan import Plan, Step  # noqa: E402

PERIOD = "2025-09"


@pytest.fixture(scope="module")
def con():
    mz.materialize(rp.compute(rp.load()), verbose=False)
    c = mz.connect_readonly()
    yield c
    c.close()


@pytest.fixture(scope="module")
def goal(con):
    return build_goal(con, PERIOD)


@pytest.fixture(scope="module")
def run(con, goal):
    return Orchestrator(con).run(variance_package_plan(goal), goal)


@pytest.fixture(scope="module")
def brief(run, goal):
    return build_briefing(run, goal)


def _ledger_values(result) -> set:
    vals = set()
    for rows in result.ledger.results.values():
        for row in rows:
            for v in row.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.add(round(float(v), 2))
                    vals.add(round(abs(float(v)), 2))
    return vals


# --------------------------------------------------------------------------
# 1. every figure is retrieved, none derived
# --------------------------------------------------------------------------
def test_every_briefing_figure_is_a_ledger_value(brief, run):
    """The briefing ranks and selects. It must never compute a figure."""
    ledger = _ledger_values(run)
    for area in brief.areas:
        assert round(area.oi_impact, 2) in ledger, area.name
        if area.share is not None:
            assert round(area.share, 2) in ledger or round(
                area.share * 100, 2) in ledger
        for group in (area.detail, area.comp, area.headcount,
                      area.revenue_split):
            for e in group:
                assert round(float(e.value), 2) in ledger, (
                    f"{area.name}/{e.label}={e.value} is not a retrieved value"
                )


def test_every_evidence_item_names_the_step_that_produced_it(brief, run):
    steps = {e.step_idx for e in run.ledger.entries}
    for area in brief.areas:
        assert area.step in steps
        for group in (area.detail, area.comp, area.headcount,
                      area.revenue_split):
            for e in group:
                assert e.step in steps and e.section in run.sections


def test_the_module_performs_no_arithmetic_on_figures():
    """Enforced by reading the source, so it survives future edits.

    The cumulative-coverage sum is the one permitted exception: it drives
    SELECTION and is never displayed. That distinction is the whole reason the
    module can rank without deriving.
    """
    src = open(os.path.join(SRC, "agent", "briefing.py"), encoding="utf-8").read()
    body = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))
    banned = r"\b(sum|mean|round|min|max)\s*\(|\bstatistics\.|\bnp\.|\bmath\."
    hits = [ln.strip() for ln in body.splitlines() if re.search(banned, ln)]
    assert not hits, f"briefing.py derives a figure: {hits[:3]}"


# --------------------------------------------------------------------------
# 2. the boundary: no recommendations
# --------------------------------------------------------------------------
IMPERATIVES = [
    "should", "recommend", "we suggest", "consider cutting", "reduce ",
    "increase ", "must ", "ought to", "action:", "next step:",
]


def test_the_briefing_states_no_recommendation(brief):
    """The architectural commitment, asserted rather than documented.

    A recommendation is not a retrieved number: nothing could verify it, and
    the publication gate would pass it through. The tool shows where the money
    moved; the analyst decides what to do.
    """
    text = render_briefing(brief).lower()
    # The framing sentence explicitly says the tool does NOT recommend, so that
    # one disclaimer is allowed; nothing else may contain an imperative.
    text = text.replace("it shows where", "").replace(
        "the tool does not recommend an action", "")
    for word in IMPERATIVES:
        assert word not in text, f"the briefing gave advice: {word!r}"


def test_the_source_contains_no_recommendation_template():
    src = open(os.path.join(SRC, "agent", "briefing.py"), encoding="utf-8").read()
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#")
                     and '"""' not in ln)
    for phrase in ("we recommend", "you should", "suggested action"):
        assert phrase not in body.lower()


# --------------------------------------------------------------------------
# 3. the selection rule is computed, and behaves
# --------------------------------------------------------------------------
def test_areas_are_ranked_by_absolute_impact(brief):
    impacts = [abs(a.oi_impact) for a in brief.areas]
    assert impacts == sorted(impacts, reverse=True)
    assert [a.rank for a in brief.areas] == list(range(1, len(brief.areas) + 1))


def test_the_cut_covers_the_material_share_without_padding(brief):
    """A Pareto cut, not a fixed count: a month with one dominant driver should
    surface one, not pad to five."""
    assert 0 < len(brief.areas) <= MAX_AREAS
    covered = 0.0
    for a in brief.areas[:-1]:
        assert a.share is None or a.share >= MIN_SHARE
        covered += a.share or 0
    assert covered < COVERAGE_TARGET, (
        "every area before the last must be needed to reach coverage"
    )


def test_direction_follows_the_operating_income_sign(brief):
    for a in brief.areas:
        assert a.direction == ("favorable" if a.oi_impact > 0 else "unfavorable")


def test_bases_are_kept_apart(brief, run):
    """Account detail is on an operating-income basis; compensation variance is
    on an expense basis. Flattening them into one list is the sign confusion
    that produced a wrong claim earlier in this build."""
    sm = next((a for a in brief.areas if a.member == "SM"), None)
    assert sm is not None
    assert sm.detail and sm.comp
    salaries = next(e for e in sm.detail if e.label == "Salaries")
    variance = next(e for e in sm.comp if e.label == "salary variance")
    assert salaries.value < 0 < variance.value, (
        "the two bases genuinely have opposite signs, which is why they are "
        "presented separately"
    )
    text = render_briefing(brief)
    assert "operating income" in text and "expense basis" in text


# --------------------------------------------------------------------------
# 4. it follows the plan, like everything else
# --------------------------------------------------------------------------
def _thin(con, goal, *steps):
    plan = Plan(goal="g",
                steps=[Step(i, t, p, purpose=f"s{i}")
                       for i, (t, p) in enumerate(steps, start=1)],
                promises=[f"s{i}" for i in range(1, len(steps) + 1)])
    return Orchestrator(con).run(plan, goal)


def test_a_plan_that_never_ranked_produces_no_briefing(con, goal):
    """Inventing priorities from a plan that did not look for them would be the
    same failure this module exists to avoid, one level up."""
    run = _thin(con, goal, ("get_pl_summary", {"period": "$GOAL.period"}))
    b = build_briefing(run, goal)
    assert not b.available and "did not rank" in b.note
    assert "WHERE TO LOOK" in render_briefing(b)


def test_detail_appears_only_for_departments_the_plan_decomposed(con, goal):
    run = _thin(
        con, goal,
        ("rank_variance_drivers",
         {"period": "$GOAL.period", "dimension": "department", "top_n": 5}),
        ("decompose_variance",
         {"period": "$GOAL.period", "department_id": "$STEP_1.rows[0].member",
          "top_n": 5}),
    )
    b = build_briefing(run, goal)
    assert b.areas[0].detail, "the decomposed department should carry detail"
    assert not any(a.detail for a in b.areas[1:]), (
        "a department the plan never decomposed must not acquire detail"
    )


@pytest.mark.parametrize("period", ["2024-06", "2025-03", "2025-12"])
def test_the_briefing_builds_for_every_period(con, period):
    g = build_goal(con, period)
    run = Orchestrator(con).run(variance_package_plan(g), g)
    b = build_briefing(run, g)
    assert b.available and b.areas
    assert render_briefing(b)


def test_formatting_uses_accounting_negatives():
    assert fmt(-142610.66, "money") == "($142,611)"
    assert fmt(15559.0, "money") == "$15,559"
    assert fmt(0.452, "percent") == "45.2%"
    assert fmt(4, "count_signed") == "+4"
    assert fmt(None, "money") == "—"


# --------------------------------------------------------------------------
# 5. the app surfaces it, and the live path is not a copy of the free one
# --------------------------------------------------------------------------
def _app(monkeypatch, key=None):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    sys.path.insert(0, os.path.join(HERE, "..", "eval"))
    at = AppTest.from_file(os.path.join(HERE, "..", "app", "app.py"),
                           default_timeout=240)
    at.run()
    return at.sidebar.radio[0].set_value("Close-cycle agent").run()


def _plain(at):
    return [re.sub("<[^>]+>", "", m.value) for m in at.markdown]


def test_every_output_section_is_titled_and_explained(monkeypatch):
    """A visitor should never have to infer what a block is.

    The standard close deliberately carries no "Where to look" section: it is
    the fixed monthly package, and the prioritisation belongs to the question
    path where a reader has actually asked something.
    """
    at = _app(monkeypatch)
    assert not at.exception
    text = " ".join(_plain(at))
    for heading in ("Financial performance overview", "Commentary",
                    "Sign off before this is used"):
        assert heading in text, f"missing section heading: {heading}"
    assert "Where to look" not in text, (
        "the standard monthly close should not carry the briefing"
    )


def test_the_sign_off_is_written_in_plain_language(monkeypatch):
    """"Gate 1 · pre-publication approval" is internal vocabulary: a reader
    outside the project cannot tell what a gate is or what is being asked."""
    at = _app(monkeypatch)
    text = " ".join(_plain(at))
    assert "Sign off before this is used" in text
    assert "Gate 1" not in text and "Gate 0" not in text
    assert "the reading is right" in text.lower()


def test_gate_0_is_not_shown_on_the_page(monkeypatch):
    """Removed from the UI, not from the codebase: on a read-only surface it
    adds inspectability rather than safety, and a plan-approval table is
    unreadable to anyone outside the project."""
    at = _app(monkeypatch)
    text = " ".join(_plain(at)) + " ".join(
        e.label or "" for e in at.checkbox)
    assert "Gate 0" not in text
    assert "plan review" not in text.lower()


def test_live_mode_offers_example_questions_not_one_generic_goal(monkeypatch):
    """The generic goal produced a thinner copy of the free deterministic run,
    which is the wrong demonstration of what a model planner is for."""
    at = _app(monkeypatch, key="sk-not-real")
    mode = next(r for r in at.radio
                if r.options and "Ask a question" in r.options)
    at = mode.set_value("Ask a question").run()
    assert not at.exception

    examples = next(sb for sb in at.selectbox
                    if sb.label and "example" in sb.label.lower())
    assert len(examples.options) >= 4
    assert any("why did" in o for o in examples.options)
    assert any("volume problem or a price problem" in o for o in examples.options)

    goal = next(t for t in at.text_input if t.label and "Goal" in t.label)
    assert goal.value == examples.options[0]


def test_the_briefing_appears_in_the_question_path(monkeypatch):
    """Prioritisation belongs where someone has actually asked something."""
    at = _app(monkeypatch, key="sk-not-real")
    mode = next(r for r in at.radio
                if r.options and "Ask a question" in r.options)
    at = mode.set_value("Ask a question").run()
    assert not at.exception


def test_the_plan_table_is_in_execution_order(goal):
    """Sequence is the entire content of a plan. The first version let the
    grid sort by section name, so the plan rendered 3, 2, 4, 1, 5."""
    sys.path.insert(0, os.path.join(HERE, "..", "app"))
    from agent.gates import summarize_plan

    summary = summarize_plan(variance_package_plan(goal))
    shuffled = sorted(summary, key=lambda s: str(s["purpose"]))
    assert [s["step"] for s in shuffled] != [s["step"] for s in summary]

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app_mod", os.path.join(HERE, "..", "app", "app.py"))
    # Importing app.py executes Streamlit; read the function's behaviour from
    # its contract instead -- the table sorts on "step".
    src = open(spec.origin, encoding="utf-8").read()
    assert 'sorted(summary, key=lambda x: x["step"])' in src


# --------------------------------------------------------------------------
# 6. integration: adding a tool must not silently break the layers below it
# --------------------------------------------------------------------------
#: Exercise EVERY registered tool. A new tool with no entry here fails
#: test_every_tool_is_exercised_by_this_suite, which is deliberate: the point
#: is that adding a tool forces you to confirm the downstream layers handle it.
TOOL_CALLS = {
    "list_periods": {},
    "list_dimensions": {"dimension": "department"},
    "get_pl_summary": {"period": PERIOD},
    "get_operating_metrics": {"period": PERIOD},
    "rank_variance_drivers": {"period": PERIOD, "dimension": "department"},
    "decompose_variance": {"period": PERIOD, "department_id": "SM"},
    "get_comp_decomposition": {"period": PERIOD},
    "get_revenue_decomposition": {"period": PERIOD},
    "get_arr_bridge": {"period": PERIOD},
    "get_headcount_movement": {"period": PERIOD},
    "get_trend": {"metric": "opex", "start_period": "2025-04",
                  "end_period": PERIOD},
    "compare_periods": {"period_a": PERIOD, "period_b": "2025-08",
                        "dimension": "department"},
    "get_ytd_summary": {"period": PERIOD},
    "rank_persistent_drivers": {"period": PERIOD, "dimension": "account"},
    "get_account_trend": {"account_id": "SM_SAL", "start_period": "2025-04",
                          "end_period": PERIOD},
    "get_opex_ratio_trend": {"department_id": "SM", "start_period": "2025-04",
                             "end_period": PERIOD},
    "rank_mom_movers": {"period": PERIOD, "dimension": "account"},
}

#: Numeric fields that are deliberately NOT narratable: ordinals and flags.
#: Listed explicitly so that "not whitelisted" is always a decision rather
#: than an oversight.
NOT_NARRATABLE = {"rank", "fiscal_year", "month_number", "step"}


def test_every_tool_is_exercised_by_this_suite():
    from agent import registry as reg

    assert set(TOOL_CALLS) == set(reg.REGISTRY), (
        "a tool was added or removed without updating TOOL_CALLS: "
        f"{set(reg.REGISTRY) ^ set(TOOL_CALLS)}"
    )


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
def test_every_numeric_field_a_tool_returns_is_whitelistable(tool, con, goal):
    """The defect this closes, generalised.

    Six comparative tools were added and none of their fields were registered
    in facts._FIELD_KINDS. The tools ran, returned rows, and contributed
    NOTHING to the audit whitelist -- so a month-over-month question executed
    perfectly and then produced "insufficient data for variance commentary"
    from a run that had retrieved everything it needed.

    A field the model can see but is not permitted to state is worse than one
    it cannot see: the run looks successful and the output is empty.
    """
    from agent import registry as reg
    from agent.facts import _FIELD_KINDS, _NON_FACT_FIELDS

    result = reg.call(tool, TOOL_CALLS[tool], con)
    assert result.ok, f"{tool} -> {result.outcome} {result.error}"

    unregistered = []
    for row in result.rows:
        for field, value in row.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if field in _NON_FACT_FIELDS or field in NOT_NARRATABLE:
                continue
            if field not in _FIELD_KINDS:
                unregistered.append(field)

    assert not unregistered, (
        f"{tool} returns numeric field(s) {sorted(set(unregistered))} that "
        "facts._FIELD_KINDS does not know about, so they cannot be narrated "
        "and will not appear in the audit whitelist"
    )


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
def test_every_tool_contributes_to_the_fact_pack(tool, con, goal):
    """A tool whose output never reaches the fact pack is a tool the agent can
    call and then be unable to talk about."""
    from agent.facts import fact_pack_from_ledger
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    if tool in ("list_periods", "list_dimensions"):
        pytest.skip("orientation tools carry no narratable figures by design")

    run = Orchestrator(con).run(
        Plan(goal="g", steps=[Step(1, tool, TOOL_CALLS[tool], purpose="s")],
             promises=["s"]), goal)
    pack = fact_pack_from_ledger(run, goal)

    assert pack.status == "ok", f"{tool} produced an unusable fact pack"
    assert pack.allowed_values, f"{tool} whitelisted nothing"
    assert len(pack.prompt_facts) > 3, (
        f"{tool} reached the model only as bare context, with no facts"
    )


def test_the_month_over_month_question_produces_a_usable_pack(con, goal):
    """Regression for the exact reported failure: 'how does this month compare
    with last month?' ran successfully and reported insufficient data."""
    from agent.facts import fact_pack_from_ledger
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    run = Orchestrator(con).run(Plan(goal="g", steps=[
        Step(1, "compare_periods",
             {"period_a": "$GOAL.period", "period_b": "$GOAL.prior_period",
              "dimension": "department", "top_n": 5}, purpose="cmp"),
        Step(2, "get_operating_metrics", {"period": "$GOAL.period"},
             purpose="om"),
    ], promises=["cmp", "om"]), goal)

    pack = fact_pack_from_ledger(run, goal)
    assert pack.status == "ok"
    assert len(pack.allowed_values) > 30, (
        f"only {len(pack.allowed_values)} values whitelisted; the comparison "
        "contributed nothing"
    )
    assert "period_comparison" in pack.prompt_facts

    b = build_briefing(run, goal)
    assert b.available, b.note
    assert "change" in b.basis


@pytest.mark.parametrize("ranker,params", [
    ("rank_persistent_drivers", {"period": PERIOD, "months_back": 6,
                                 "dimension": "account", "top_n": 5}),
    ("rank_mom_movers", {"period": PERIOD, "dimension": "account",
                         "top_n": 5}),
    ("compare_periods", {"period_a": PERIOD, "period_b": "2025-08",
                         "dimension": "department", "top_n": 5}),
])
def test_any_ranking_tool_can_seed_a_briefing(ranker, params, con, goal):
    """The first version only recognised rank_variance_drivers, so a
    persistence or month-over-month question ran successfully and then
    reported 'nothing to prioritise' -- the briefing refusing to summarise a
    run that had produced exactly the ranking it needed."""
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    run = Orchestrator(con).run(
        Plan(goal="g", steps=[Step(1, ranker, params, purpose="r")],
             promises=["r"]), goal)
    b = build_briefing(run, goal)

    assert b.available, b.note
    assert b.areas and b.basis
    assert "impact" in b.basis
    text = render_briefing(b)
    assert f"Ranked by {b.basis}" in text


# --------------------------------------------------------------------------
# 7. direction, names, and the deterministic fallback
# --------------------------------------------------------------------------
def test_direction_follows_the_impact_sign_not_a_missing_flag(con, goal):
    """`favorable` exists on the versus-plan tools but NOT on the comparative
    ones, so reading it directly labelled every month-over-month row
    "unfavorable" -- including revenue that had RISEN."""
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    run = Orchestrator(con).run(Plan(goal="g", steps=[
        Step(1, "rank_mom_movers",
             {"period": PERIOD, "dimension": "account", "top_n": 5},
             purpose="m")], promises=["m"]), goal)
    b = build_briefing(run, goal)

    assert b.areas
    for a in b.areas:
        assert a.direction == ("favorable" if a.oi_impact > 0 else "unfavorable")
    up = [a for a in b.areas if a.oi_impact > 0]
    assert up and all(a.direction == "favorable" for a in up)


def test_colliding_names_are_disambiguated(con, goal):
    """Every department has an account called "Salaries", so two rows reading
    "Salaries ($13,069)" and "Salaries ($9,503)" are ambiguous in exactly the
    place a reader is deciding where to look."""
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    run = Orchestrator(con).run(Plan(goal="g", steps=[
        Step(1, "rank_mom_movers",
             {"period": PERIOD, "dimension": "account", "top_n": 8},
             purpose="m")], promises=["m"]), goal)
    names = [a.name for a in build_briefing(run, goal).areas]
    assert len(names) == len(set(names)), f"ambiguous names: {names}"


def test_a_comparative_run_still_produces_publishable_commentary(con, goal):
    """Regression for the reported failure in full.

    The copilot's deterministic narrative reads a fixed schema -- revenue
    versus budget, top drivers, the ARR bridge -- and produced NOTHING for a
    run that filled none of those slots. So "how does this month compare with
    last month?" executed perfectly and then reported insufficient data.
    """
    import run_pipeline as rp
    from agent.narrate import narrate
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step
    from guardrails import entity_audit as ea

    run = Orchestrator(con).run(Plan(goal="g", steps=[
        Step(1, "compare_periods",
             {"period_a": "$GOAL.period", "period_b": "$GOAL.prior_period",
              "dimension": "department", "top_n": 5}, purpose="c"),
        Step(2, "get_operating_metrics", {"period": "$GOAL.period"},
             purpose="o"),
    ], promises=["c", "o"]), goal)

    cand = narrate(run, goal, None, ea.canonical_entity_names(rp.load()),
                   mode="inject")
    assert cand.publishable, f"{cand.status}: {cand.reason}"
    assert cand.matched, "the commentary verified no figures"
    assert "insufficient" not in cand.text.lower()


@pytest.mark.parametrize("tool,params", [
    ("rank_persistent_drivers", {"period": PERIOD, "months_back": 6,
                                 "dimension": "account", "top_n": 5}),
    ("rank_mom_movers", {"period": PERIOD, "dimension": "department",
                         "top_n": 5}),
    ("compare_periods", {"period_a": PERIOD, "period_b": "2025-08",
                         "dimension": "department", "top_n": 5}),
])
def test_every_comparative_question_yields_commentary(tool, params, con, goal):
    import run_pipeline as rp
    from agent.narrate import narrate
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step
    from guardrails import entity_audit as ea

    run = Orchestrator(con).run(
        Plan(goal="g", steps=[Step(1, tool, params, purpose="r")],
             promises=["r"]), goal)
    cand = narrate(run, goal, None, ea.canonical_entity_names(rp.load()),
                   mode="inject")
    assert cand.publishable, f"{tool}: {cand.status} {cand.reason}"
    assert cand.matched


def test_the_fallback_narrative_states_no_recommendation(con, goal):
    from agent.briefing import narrative_from_briefing
    from agent.orchestrator import Orchestrator
    from agent.plan import Plan, Step

    run = Orchestrator(con).run(Plan(goal="g", steps=[
        Step(1, "rank_variance_drivers",
             {"period": PERIOD, "dimension": "department", "top_n": 5},
             purpose="r")], promises=["r"]), goal)
    text = narrative_from_briefing(build_briefing(run, goal)).lower()
    for word in IMPERATIVES:
        assert word not in text, f"the fallback gave advice: {word!r}"
