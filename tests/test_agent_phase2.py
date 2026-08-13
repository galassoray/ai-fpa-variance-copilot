"""
test_agent_phase2.py
====================
Phase-2 acceptance gate: the ledger, the orchestrator, symbolic reference
resolution, the failure taxonomy, run budgets, and the deterministic variance
package produced with no model in the loop.

WHAT THIS SUITE IS FOR
----------------------
Phase 1 proved the tool surface computes correctly. Phase 2 proves the *engine*
around it behaves correctly when things go wrong -- which is where agents
actually fail. The failure taxonomy is only real if every branch has a test that
triggers it, so each of EMPTY / INVALID_PARAM / RESOLUTION / BUDGET / REFUSED is
exercised against real data rather than mocked.

The distinction that gets the most attention here is EMPTY versus error. A valid
query returning zero rows is a retrieved fact; collapsing it into a failure is
how an agent ends up substituting a different query until something comes back,
and how a package quietly ends up describing the wrong thing.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import run_pipeline as rp  # noqa: E402
from agent import ledger as lg  # noqa: E402
from agent import materialize as mz  # noqa: E402
from agent import registry as reg  # noqa: E402
from agent import tools as _tools  # noqa: E402,F401  (registers the tools)
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.packages import (REQUIRED_SECTIONS, GoalError, build_goal,  # noqa: E402
                            package_digest, variance_package_plan)
from agent.plan import (GoalRef, Plan, PlanError, ResolutionError,  # noqa: E402
                        Step, StepRef, parse_reference, resolve_params,
                        validate_plan)

PERIOD = "2025-09"
PERIOD_KEY = "2025-09-01"
GOLDEN = os.path.join(HERE, "golden_agent_package.json")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def con():
    mz.materialize(rp.compute(rp.load()), verbose=False)
    c = mz.connect_readonly()
    yield c
    c.close()


@pytest.fixture(scope="module")
def goal(con):
    return build_goal(con, PERIOD)


class CountingConnection:
    """Wraps the connection to count executions.

    Used to prove that a plan rejected by static validation costs *zero*
    queries -- the claim that validation is the cheapest place to fail is only
    worth making if it is measured.
    """

    def __init__(self, inner):
        self._inner = inner
        self.executions = 0

    def execute(self, *a, **k):
        self.executions += 1
        return self._inner.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------------------------
# reference parsing
# --------------------------------------------------------------------------
def test_parses_goal_and_step_references():
    assert parse_reference("$GOAL.period") == GoalRef("period")
    assert parse_reference("$STEP_4.rows[0].member") == StepRef(4, 0, "member")
    assert parse_reference("2025-09") is None
    assert parse_reference(5) is None


def test_malformed_reference_is_an_error_not_a_literal():
    """Treating '$STEP_4.member' as a literal string would surface later as a
    confusing 'no such department' error instead of 'your plan is broken'."""
    for bad in ["$STEP_4.member", "$STEP_x.rows[0].a", "$GOAL", "$", "$STEP_4.rows[a].b"]:
        with pytest.raises(PlanError):
            parse_reference(bad)


# --------------------------------------------------------------------------
# static plan validation
# --------------------------------------------------------------------------
def _p(*steps, promises=None):
    return Plan(goal="t", steps=list(steps), promises=promises or [])


def test_valid_plan_passes(goal):
    validate_plan(variance_package_plan(goal), set(goal))


@pytest.mark.parametrize("plan,expect", [
    (_p(Step(1, "run_sql", {"query": "SELECT 1"})), "unknown tool"),
    (_p(Step(1, "get_pl_summary", {"period": "2025-09", "limit": 5})), "unknown parameter"),
    (_p(Step(1, "get_pl_summary", {})), "missing required parameter"),
    (_p(Step(1, "get_pl_summary", {"period": "$GOAL.nonsense"})), "not a goal field"),
    (_p(Step(1, "decompose_variance",
             {"period": "$GOAL.period", "department_id": "$STEP_1.rows[0].member"})),
     "references itself"),
    (_p(Step(1, "decompose_variance",
             {"period": "$GOAL.period", "department_id": "$STEP_2.rows[0].member"}),
        Step(2, "get_pl_summary", {"period": "$GOAL.period"})),
     "forward reference"),
    (_p(Step(2, "decompose_variance",
             {"period": "$GOAL.period", "department_id": "$STEP_1.rows[0].member"})),
     "nonexistent step"),
    (_p(Step(1, "get_pl_summary", {"period": "$GOAL.period"}),
        Step(1, "get_arr_bridge", {"period": "$GOAL.period"})),
     "duplicate step index"),
    (_p(), "no steps"),
])
def test_malformed_plans_are_rejected(plan, expect, goal):
    with pytest.raises(PlanError) as exc:
        validate_plan(plan, set(goal))
    assert expect in str(exc.value)


def test_validation_reports_every_problem_not_just_the_first(goal):
    plan = _p(Step(1, "no_such_tool", {}),
              Step(2, "get_pl_summary", {}),
              Step(3, "get_pl_summary", {"period": "$GOAL.bogus"}))
    with pytest.raises(PlanError) as exc:
        validate_plan(plan, set(goal))
    assert len(exc.value.problems) >= 3


def test_rejected_plan_costs_zero_queries(con, goal):
    """Static validation runs before anything touches the database."""
    counting = CountingConnection(con)
    orch = Orchestrator(counting, check_freshness=False)
    counting.executions = 0
    with pytest.raises(PlanError):
        orch.run(_p(Step(1, "run_sql", {"query": "SELECT 1"})), goal)
    assert counting.executions == 0


# --------------------------------------------------------------------------
# symbolic resolution
# --------------------------------------------------------------------------
def test_reference_resolves_from_prior_result(goal):
    results = {4: [{"member": "SM"}, {"member": "RND"}]}
    step = Step(5, "decompose_variance",
                {"period": "$GOAL.period", "department_id": "$STEP_4.rows[1].member"})
    out = resolve_params(step, goal, results)
    assert out == {"period": PERIOD_KEY, "department_id": "RND"}


def test_reference_to_missing_row_is_a_resolution_error(goal):
    """Rank 2 of a single-row result is a real condition, and it must surface
    as a resolution failure rather than a silent None."""
    step = Step(5, "decompose_variance",
                {"period": "$GOAL.period", "department_id": "$STEP_4.rows[3].member"})
    with pytest.raises(ResolutionError) as exc:
        resolve_params(step, goal, {4: [{"member": "SM"}]})
    assert "row index 3 does not exist" in str(exc.value)


def test_reference_to_missing_field_names_what_is_available(goal):
    step = Step(5, "decompose_variance",
                {"period": "$GOAL.period", "department_id": "$STEP_4.rows[0].dept"})
    with pytest.raises(ResolutionError) as exc:
        resolve_params(step, goal, {4: [{"member": "SM", "name": "Sales"}]})
    assert "available" in str(exc.value)


def test_ledger_records_declared_and_resolved_separately(con, goal):
    """The provenance proof: declared shows the reference, resolved shows the
    value, and the pair demonstrates the value came from a prior tool result
    rather than from a model retyping it."""
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    e5 = res.ledger.entry(5)
    assert e5.params_declared["department_id"] == "$STEP_4.rows[0].member"
    assert e5.params_resolved["department_id"] == "CORP"
    assert e5.used_symbolic_reference


def test_no_resolved_argument_is_a_financial_quantity(con, goal):
    """Phase 1 made money parameters unrepresentable by type. This asserts the
    property survives symbolic resolution -- a reference must not be able to
    smuggle a figure into an argument slot."""
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    for e in res.ledger.entries:
        for pname, val in e.params_resolved.items():
            assert not isinstance(val, float), f"step {e.step_idx}.{pname} resolved to a float"
            if isinstance(val, int) and not isinstance(val, bool):
                spec = reg.REGISTRY[e.tool].params[pname]
                assert isinstance(spec, reg.IntParam), (
                    f"step {e.step_idx}.{pname} is a bare int on a non-IntParam"
                )


# --------------------------------------------------------------------------
# failure taxonomy -- every branch triggered against real data
# --------------------------------------------------------------------------
def test_empty_on_optional_step_continues(con, goal):
    """2024 has no forecast, so this is a genuinely empty result."""
    g = dict(goal)
    plan = _p(
        Step(1, "get_pl_summary", {"period": "$GOAL.period"}, purpose="a"),
        Step(2, "get_pl_summary",
             {"period": "2024-03", "comparison": "actual_vs_forecast"},
             purpose="b", optional=True),
        Step(3, "get_arr_bridge", {"period": "$GOAL.period"}, purpose="c"),
        promises=["a", "c"],
    )
    res = Orchestrator(con).run(plan, g)
    assert res.outcome == lg.COMPLETED and res.complete
    assert res.ledger.entry(2).outcome == reg.EMPTY
    assert res.ledger.entry(3).outcome == reg.OK, "run must continue past an optional EMPTY"
    assert any("no rows" in n for n in res.ledger.notes)


def test_empty_on_required_step_refuses(con, goal):
    """A promised section that cannot be produced is a refusal, not a package
    with a hole in it and no mention of the hole."""
    plan = _p(
        Step(1, "get_pl_summary",
             {"period": "2024-03", "comparison": "actual_vs_forecast"}, purpose="a"),
        Step(2, "get_arr_bridge", {"period": "$GOAL.period"}, purpose="b"),
        promises=["a", "b"],
    )
    res = Orchestrator(con).run(plan, goal)
    assert res.outcome == lg.REFUSED
    assert res.refusal_reason == "REQUIRED_SECTION_UNAVAILABLE"
    assert res.ledger.entry(2) is None, "run must stop, not continue past a required EMPTY"


def test_invalid_param_on_required_step_aborts(con, goal):
    plan = _p(Step(1, "decompose_variance",
                   {"period": "$GOAL.period", "department_id": "LEGAL"}, purpose="a"),
              promises=["a"])
    res = Orchestrator(con).run(plan, goal)
    assert res.outcome == lg.ABORTED
    assert res.ledger.entry(1).outcome == reg.INVALID_PARAM
    assert "SM" in res.ledger.entry(1).valid_alternatives


def test_resolution_failure_is_recorded_and_aborts(con, goal):
    """A plan asking for rank 2 of a one-row result fails validation nowhere --
    it is only knowable at execution time."""
    plan = _p(
        Step(1, "get_arr_bridge", {"period": "$GOAL.period"}, purpose="a"),
        Step(2, "decompose_variance",
             {"period": "$GOAL.period", "department_id": "$STEP_1.rows[4].member"},
             purpose="b"),
        promises=["a", "b"],
    )
    res = Orchestrator(con).run(plan, goal)
    assert res.outcome == lg.ABORTED
    e = res.ledger.entry(2)
    assert e.outcome == reg.INVALID_PARAM and "could not be resolved" in e.error
    assert e.params_resolved == {}, "nothing was executed for an unresolvable step"


def test_tool_error_on_optional_step_does_not_abort(con, goal):
    plan = _p(
        Step(1, "get_trend",
             {"metric": "opex", "start_period": "2025-09", "end_period": "2025-01"},
             purpose="a", optional=True),
        Step(2, "get_arr_bridge", {"period": "$GOAL.period"}, purpose="b"),
        promises=["b"],
    )
    res = Orchestrator(con).run(plan, goal)
    assert res.ledger.entry(1).outcome == reg.TOOL_ERROR
    assert res.outcome == lg.COMPLETED and res.complete


# --------------------------------------------------------------------------
# coverage -- silent incompleteness is the failure being prevented
# --------------------------------------------------------------------------
def test_promised_but_unproduced_section_is_flagged(con, goal):
    plan = _p(Step(1, "get_arr_bridge", {"period": "$GOAL.period"}, purpose="a"),
              promises=["a", "never_produced"])
    res = Orchestrator(con).run(plan, goal)
    assert res.missing_sections == ["never_produced"]
    assert not res.complete, "a package missing a promised section is not complete"
    assert any("missing promised sections" in n for n in res.ledger.notes)


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------
def test_step_budget_stops_the_run_and_marks_it_incomplete(con, goal):
    plan = variance_package_plan(goal)
    res = Orchestrator(con, budget=lg.Budget(max_steps=4)).run(plan, goal)
    assert res.outcome == lg.BUDGET_EXHAUSTED
    assert res.ledger.steps_used == 4
    assert res.missing_sections, "truncation must be declared, never silent"
    assert any("budget exhausted" in n for n in res.ledger.notes)


def test_wall_clock_budget_is_enforced(con, goal):
    res = Orchestrator(con, budget=lg.Budget(max_wall_clock_s=0.0)).run(
        variance_package_plan(goal), goal)
    assert res.outcome == lg.BUDGET_EXHAUSTED
    assert res.ledger.steps_used == 0


def test_budget_snapshot_is_recorded(con, goal):
    res = Orchestrator(con, budget=lg.Budget(max_steps=7)).run(
        variance_package_plan(goal), goal)
    assert json.loads(res.ledger.to_json())["budget"]["max_steps"] == 7


# --------------------------------------------------------------------------
# goal construction and refusal reason codes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("period,comparison,code", [
    ("2029-03", "actual_vs_budget", "PERIOD_NOT_IN_DATASET"),
    ("not-a-date", "actual_vs_budget", "PERIOD_NOT_IN_DATASET"),
    ("2024-03", "actual_vs_forecast", "COMPARISON_UNAVAILABLE"),
])
def test_unservable_goals_refuse_with_a_reason_code(con, period, comparison, code):
    """Refusal recall is an eval metric. Metrics over free text are not
    measurable, so refusals carry codes."""
    with pytest.raises(GoalError) as exc:
        build_goal(con, period, comparison)
    assert exc.value.reason_code == code


def test_servable_goals_are_not_refused(con):
    """False-refusal rate must be zero on valid goals."""
    for m in ["2024-01", "2024-12", "2025-01", "2025-09", "2025-12"]:
        assert build_goal(con, m)["period"].startswith(m)


def test_goal_fields_are_computed_not_modeled(con):
    g = build_goal(con, "2025-09")
    assert g["fiscal_year"] == 2025 and g["fy_start"] == "2025-01-01"
    assert g["prior_period"] == "2025-08-01" and g["quarter"] == "2025-Q3"


# --------------------------------------------------------------------------
# ledger integrity
# --------------------------------------------------------------------------
def test_ledger_is_append_only(con, goal):
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    n = len(res.ledger.entries)
    res.ledger.entries.append("junk")          # mutating the copy must not stick
    assert len(res.ledger.entries) == n


def test_replay_verification_passes_and_detects_tampering(con, goal):
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    saved = json.loads(res.ledger.to_json())
    ok, problems = lg.verify_replay(saved)
    assert ok and not problems

    saved["results"]["3"][0]["actual"] = 999999.99
    ok2, problems2 = lg.verify_replay(saved)
    assert not ok2 and any("step 3" in p for p in problems2)


def test_cost_is_instrumented_from_the_start(con, goal):
    """Zero model spend at Phase 2, but the fields exist and are populated, so
    the agent/pipeline comparison in Phase 6 measures the right things."""
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    c = res.ledger.cost_summary()
    assert set(c) == {"steps", "wall_clock_s", "tool_latency_ms",
                      "tokens_in", "tokens_out", "cost_usd", "replans"}
    assert c["steps"] == 11 and c["tool_latency_ms"] > 0
    assert c["tokens_in"] == 0 and c["cost_usd"] == 0.0


def test_every_step_produces_exactly_one_ledger_entry(con, goal):
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    idxs = [e.step_idx for e in res.ledger.entries]
    assert idxs == sorted(idxs) and len(idxs) == len(set(idxs)) == 11


# --------------------------------------------------------------------------
# the deterministic package
# --------------------------------------------------------------------------
def test_september_package_is_complete(con, goal):
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    assert res.outcome == lg.COMPLETED and res.complete
    assert not res.missing_sections
    for s in REQUIRED_SECTIONS:
        assert s in res.sections, f"missing {s}"


def test_package_numbers_are_reproducible_across_runs(con, goal):
    """Model nondeterminism is confined to plan selection and prose. With no
    model at all, the figures must be bit-identical every time -- and this is
    the control condition the Phase 3 planner is measured against."""
    runs = [Orchestrator(con).run(variance_package_plan(build_goal(con, PERIOD)),
                                  build_goal(con, PERIOD)) for _ in range(3)]
    digests = [json.dumps(package_digest(r), sort_keys=True) for r in runs]
    assert len(set(digests)) == 1


def test_package_works_for_every_period_with_data(con):
    """The plan is written once and runs for any period; the two decomposition
    steps bind by reference rather than hardcoding a department."""
    for m in ["2024-06", "2025-01", "2025-12"]:
        g = build_goal(con, m)
        res = Orchestrator(con).run(variance_package_plan(g), g)
        assert res.complete, f"{m}: {res.outcome} missing={res.missing_sections}"


def test_package_matches_golden(con, goal):
    """Pins plan shape and every section's result hash. Regenerate deliberately
    with AGENT_GOLDEN=regen after an intended computation change."""
    res = Orchestrator(con).run(variance_package_plan(goal), goal)
    digest = package_digest(res)
    if os.environ.get("AGENT_GOLDEN") == "regen" or not os.path.exists(GOLDEN):
        with open(GOLDEN, "w") as fh:
            json.dump(digest, fh, indent=2, sort_keys=True)
        pytest.skip("golden regenerated")
    with open(GOLDEN) as fh:
        expected = json.load(fh)
    assert digest == expected, (
        "package diverged from golden. If intended, re-run with AGENT_GOLDEN=regen"
    )


def test_orchestrator_refuses_stale_marts(con, monkeypatch):
    monkeypatch.setattr(mz, "build_hash", lambda: "deadbeefdeadbeef")
    with pytest.raises(mz.StaleMartError):
        Orchestrator(con)


# --------------------------------------------------------------------------
# Phase-2 boundary
# --------------------------------------------------------------------------
#: Phase 3 introduces exactly one module permitted to touch a model. Every
#: other module in src/agent must stay model-free, so that the execution engine
#: remains provably correct independent of any planner. Widening this set should
#: require a decision-log entry.
LLM_ALLOWED_MODULES = {"planner.py"}


def test_only_the_planner_may_reach_a_model():
    """The execution engine must be correct independent of any model.

    Phase 2 asserted that nothing in src/agent imported a model client. Phase 3
    adds a planner, which necessarily does -- so the assertion narrows rather
    than disappears: the model client has exactly one home, and the ledger,
    orchestrator, registry, tools, and materialization stay model-free.
    """
    banned = ("anthropic", "openai", "llm_client", "generate_commentary")
    agent_dir = os.path.join(SRC, "agent")
    for fname in sorted(os.listdir(agent_dir)):
        if not fname.endswith(".py") or fname in LLM_ALLOWED_MODULES:
            continue
        text = open(os.path.join(agent_dir, fname)).read()
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for b in banned:
                assert b not in stripped, f"{fname} imports {b}: {stripped}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_runs_completes_and_refuses(con, capsys):
    """The CLI is the Phase-3 comparison baseline and the Phase-5 replay source,
    so its exit codes are contract, not convenience: 0 complete, 2 refused."""
    from agent import run_package as cli

    assert cli.main([PERIOD]) == 0
    out = capsys.readouterr().out
    assert "VARIANCE PACKAGE" in out
    assert "$STEP_4.rows[0].member ->" in out, "ledger must show reference resolution"
    assert "no model in the loop" in out

    assert cli.main(["2029-01"]) == 2
    assert "PERIOD_NOT_IN_DATASET" in capsys.readouterr().err

    assert cli.main(["2024-03", "--comparison", "actual_vs_forecast"]) == 2
    assert "COMPARISON_UNAVAILABLE" in capsys.readouterr().err


def test_cli_json_output_is_replayable(con, capsys):
    from agent import run_package as cli

    assert cli.main([PERIOD, "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    ok, problems = lg.verify_replay(saved)
    assert ok and not problems
    assert len(saved["steps"]) == 11 and saved["outcome"] == lg.COMPLETED


# --------------------------------------------------------------------------
# source -> base tables -> marts: the chain must not lie
# --------------------------------------------------------------------------
def test_changed_source_data_propagates_all_the_way_to_the_package(tmp_path, monkeypatch):
    """Regression for a false-green found by changing a CSV and re-running.

    run_pipeline.ensure_database() only builds the DuckDB file when it is
    absent, so the database is a cache that was never invalidated. load() reads
    that cache while build_hash fingerprints the CSVs -- so editing a CSV and
    re-materializing produced marts computed from STALE base tables, stamped
    with a hash derived from the NEW CSVs. The freshness check reported current
    while certifying data it had never read.

    A check that lies is worse than no check, because it is trusted. This
    asserts the whole chain moves together: change the source, and the package
    changes.
    """
    import csv
    import shutil
    import subprocess

    work = tmp_path / "repo"
    root = os.path.abspath(os.path.join(HERE, ".."))
    for sub in ["src", "data/synthetic"]:
        shutil.copytree(os.path.join(root, sub), work / sub, dirs_exist_ok=True)
    (work / "data" / "processed").mkdir(parents=True, exist_ok=True)

    def run(*args):
        return subprocess.run([sys.executable, *args], cwd=str(work),
                              capture_output=True, text=True)

    assert run("src/agent/materialize.py").returncode == 0
    before = run("src/agent/run_package.py", PERIOD)
    assert before.returncode == 0 and "Customer Success" in before.stdout

    # Blow up one department's salaries so the ranking must change.
    fp = work / "data" / "synthetic" / "fact_actuals.csv"
    rows = list(csv.DictReader(open(fp, newline="")))
    for r in rows:
        if r["month"].startswith("2025-09") and r["account_id"] == "CS_SAL":
            r["amount"] = str(float(r["amount"]) + 400_000)
    with open(fp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["month", "department_id", "account_id", "amount"])
        w.writeheader()
        w.writerows(rows)

    # 1. Without re-materializing, the run must refuse rather than serve stale marts.
    stale = run("src/agent/run_package.py", PERIOD)
    assert stale.returncode != 0 and "stale" in stale.stderr.lower()

    # 2. Re-materializing must rebuild the base tables from the changed CSVs...
    remat = run("src/agent/materialize.py")
    assert remat.returncode == 0
    assert "rebuilding base tables" in remat.stdout

    # 3. ...and the package must reflect the new data and re-bind its plan.
    after = run("src/agent/run_package.py", PERIOD)
    assert after.returncode == 0
    assert "$STEP_4.rows[0].member -> CS" in after.stdout, (
        "the plan must follow the data: CS is now the largest driver"
    )
    assert "($375," in after.stdout or "($384," in after.stdout


def test_csv_fingerprint_is_recorded_and_distinct_from_build_hash(con):
    """Two fingerprints answering two questions: are the base tables current
    with the CSVs, and are the marts current with the computation."""
    assert mz.csv_fingerprint() != mz.build_hash()
    row = con.execute(f"SELECT csv_fingerprint FROM {mz.SOURCE_META} LIMIT 1").fetchone()
    assert row and row[0] == mz.csv_fingerprint()
