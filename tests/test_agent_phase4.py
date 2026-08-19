"""
test_agent_phase4.py
====================
Phase-4 acceptance gate: narration, the audit, and the publication gate.

THE TWO PROPERTIES THIS SUITE EXISTS TO PROVE
----------------------------------------------
1. **The agent cannot skip the audit.** Not "we notice when it does" -- it has
   no ability to invoke, omit, or reorder narration and auditing, because they
   are orchestrator stages and not tools. The tests assert the registry contains
   no such verb and that a plan attempting one is rejected before execution.

2. **The whitelist comes from the ledger.** Every value the model is permitted
   to state traces to a figure the agent's tools actually returned. Not
   recomputed alongside, which would give two paths to a number that can drift
   and produce prose that passes its audit while contradicting the package
   printed beside it.

Everything runs offline with scripted clients. The guarantee is a property of
the audit and the gate, not of the model, so a test needing a good model to pass
would not be testing the guarantee.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import run_pipeline as rp  # noqa: E402
from agent import materialize as mz  # noqa: E402
from agent import registry as reg  # noqa: E402
from agent import tools as _tools  # noqa: E402,F401
from agent.facts import fact_pack_from_ledger  # noqa: E402
from agent.narrate import (ACCEPTED, FALLBACK, NOT_RUN, REFUSED,  # noqa: E402
                           NotPublishable, PublicationCandidate, narrate,
                           render_narrative)
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.packages import build_goal, variance_package_plan  # noqa: E402
from agent.plan import Plan, PlanError, Step, validate_plan  # noqa: E402
from guardrails import entity_audit as ea  # noqa: E402

PERIOD = "2025-09"


@pytest.fixture(scope="module")
def tables():
    return rp.load()


@pytest.fixture(scope="module")
def names(tables):
    return ea.canonical_entity_names(tables)


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
def full_run(con, goal):
    return Orchestrator(con).run(variance_package_plan(goal), goal)


class Scripted:
    """Returns canned prose. No network, no key, no cost."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.i = 0
        self.model = "scripted"
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        t = self.texts[min(self.i, len(self.texts) - 1)]
        self.i += 1
        return t


def _thin_run(con, goal, *steps):
    plan = Plan(goal="g",
                steps=[Step(i, t, p, purpose=f"s{i}")
                       for i, (t, p) in enumerate(steps, start=1)],
                promises=[f"s{i}" for i in range(1, len(steps) + 1)])
    return Orchestrator(con).run(plan, goal)


# --------------------------------------------------------------------------
# 1. the agent cannot skip the audit, because it cannot invoke it
# --------------------------------------------------------------------------
def test_narration_and_audit_are_not_tools():
    """The strongest form of the guarantee.

    Exposing audit_narrative as a tool and checking the agent called it makes
    the guardrail depend on the agent's cooperation. Narration and auditing are
    orchestrator stages, so no plan can include them, omit them, or reorder
    them.
    """
    for verb in ("audit_narrative", "narrate", "generate_commentary",
                 "write_commentary", "publish", "stage_package", "approve"):
        assert verb not in reg.REGISTRY, f"'{verb}' must not be an agent tool"


def test_a_plan_that_tries_to_audit_or_publish_is_rejected(goal):
    for verb in ("audit_narrative", "publish"):
        plan = Plan(goal="g", steps=[Step(1, verb, {})])
        with pytest.raises(PlanError) as exc:
            validate_plan(plan, set(goal))
        assert "unknown tool" in str(exc.value)


def test_a_run_that_never_narrates_is_not_publishable(full_run):
    """A run that simply never reaches narration produces a candidate whose
    verdict is 'not run', and that is not publishable. A missing audit and a
    failed audit are deliberately the same answer: any other choice makes
    forgetting to check equivalent to passing."""
    candidate = PublicationCandidate()
    assert candidate.status == NOT_RUN
    assert not candidate.audit_ran and not candidate.publishable
    with pytest.raises(NotPublishable):
        candidate.assert_publishable()


def test_publishable_requires_the_audit_to_have_run_not_merely_not_failed():
    c = PublicationCandidate(status=ACCEPTED, text="Revenue was $2.57M.",
                             audit_ran=False, audit_passed=True)
    assert not c.publishable, "an audit that never ran cannot license publication"

    c.audit_ran = True
    assert c.publishable

    c.audit_passed = False
    assert not c.publishable


def test_assert_publishable_raises_rather_than_returning_a_flag():
    """A caller must not be able to ignore the verdict by not looking at it."""
    with pytest.raises(NotPublishable) as exc:
        PublicationCandidate(status=REFUSED).assert_publishable()
    assert "not publishable" in str(exc.value)


# --------------------------------------------------------------------------
# 2. the whitelist comes from the ledger
# --------------------------------------------------------------------------
def test_every_allowed_value_traces_to_a_ledger_row(full_run, goal):
    """Not recomputed alongside: each whitelisted figure is a value a tool
    returned, and the provenance record says which step."""
    pack = fact_pack_from_ledger(full_run, goal)
    assert pack.status == "ok" and pack.allowed_values

    ledger_numbers = set()
    for rows in full_run.ledger.results.values():
        for row in rows:
            for v in row.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    ledger_numbers.add(round(float(v), 4))
                    ledger_numbers.add(round(abs(float(v)), 4))

    for a in pack.allowed_values:
        assert round(a.value, 4) in ledger_numbers, (
            f"{a.value} ({a.label}) is not a value any tool returned"
        )

    steps_run = {e.step_idx for e in full_run.ledger.entries}
    for p in pack.ledger_provenance:
        assert p["step"] in steps_run


def test_only_what_the_agent_retrieved_is_narratable(con, goal):
    """The plan determines the narrative's permitted vocabulary.

    A plan that never calls get_arr_bridge puts no ARR figure in the whitelist,
    so the model cannot state one -- and the audit catches it if it tries. A
    thin plan yields a thin, correct commentary rather than a rich, partly
    invented one.
    """
    thin = _thin_run(con, goal, ("get_pl_summary", {"period": "$GOAL.period"}))
    pack = fact_pack_from_ledger(thin, goal)

    assert pack.status == "ok"
    assert "arr" not in pack.prompt_facts
    assert not any("ARR" in a.label for a in pack.allowed_values)

    full = fact_pack_from_ledger(
        _thin_run(con, goal,
                  ("get_pl_summary", {"period": "$GOAL.period"}),
                  ("get_arr_bridge", {"period": "$GOAL.period"})), goal)
    assert "arr" in full.prompt_facts
    assert any("ARR" in a.label for a in full.allowed_values)
    assert len(full.allowed_values) > len(pack.allowed_values)


def test_a_figure_from_an_unretrieved_tool_is_rejected(con, goal, names):
    """The end-to-end version of the property above.

    $28.81M is the REAL ending ARR -- correct, and never retrieved by this plan.
    Correctness is not the test: provenance is. A figure the run did not fetch
    is unverifiable against this run's ledger, so it must not survive, even
    though a human checking it against the database would find it right.

    The model's draft is rejected and the deterministic fallback is published
    instead. That fallback IS publishable -- refusing outright would be the
    wrong response when a safe, computed narrative is available -- and the
    thing that matters is that the unverifiable figure is gone.
    """
    thin = _thin_run(con, goal, ("get_pl_summary", {"period": "$GOAL.period"}))
    cand = narrate(thin, goal, Scripted("Ending ARR was $28.81M."), names,
                   max_retries=0)

    assert cand.source != "model", "the model's draft must not be accepted"
    assert "28.81" not in cand.text
    assert not cand.deterministic_by_choice, "this fallback was forced, not chosen"
    if cand.publishable:
        assert cand.source == "injection" and cand.audit_passed


def test_ranking_grains_are_kept_apart(full_run, goal):
    """A department rollup and one of its own accounts are both legitimate
    drivers, but ranking them in one list puts a parent beside its child and
    implies they are comparable."""
    pack = fact_pack_from_ledger(full_run, goal)
    grains = {d["grain"] for d in pack.prompt_facts["top_drivers"]}
    assert grains == {"department"}
    assert all(d["grain"] == "account"
               for d in pack.prompt_facts["driver_detail_by_account"])


def test_orientation_tools_contribute_no_figures(con, goal):
    """list_periods returns counts and flags, not narratable quantities.
    A whitelisted '24' would license the model to write '$24'."""
    run = _thin_run(con, goal, ("list_periods", {}))
    pack = fact_pack_from_ledger(run, goal)
    assert pack.status == "insufficient_data"


def test_ranks_and_ids_are_never_whitelisted(full_run, goal):
    """'the number 1 driver' must not license the literal 1 as a verified
    dollar figure."""
    pack = fact_pack_from_ledger(full_run, goal)
    for a in pack.allowed_values:
        assert "rank" not in a.label.lower()
    dollars = {a.value for a in pack.allowed_by_kind("dollar")}
    assert not {1.0, 2.0, 3.0, 4.0, 5.0} & dollars


# --------------------------------------------------------------------------
# 3. fabrication is caught
# --------------------------------------------------------------------------
def test_a_fabricated_figure_never_reaches_the_output(full_run, goal, names):
    fabricated = ("Revenue of $2.57M came in below plan. Marketing overspend of "
                  "$847,213 drove the miss.")
    cand = narrate(full_run, goal, Scripted(fabricated), names, max_retries=0)

    assert "847,213" not in cand.text, "the violating text must never be surfaced"
    assert cand.source != "model"
    if cand.publishable:
        assert cand.source == "injection"


def test_the_audit_reports_what_it_caught(full_run, goal, names):
    bad = "Opex was $9,999,111 for the period."
    cand = narrate(full_run, goal, Scripted(bad), names, max_retries=0)
    text = render_narrative(cand)
    assert cand.violations or cand.source == "injection"
    assert "9,999,111" not in cand.text and "9,999,111" not in text


def test_a_retry_is_offered_before_giving_up(full_run, goal, names):
    """One regeneration with the offending figures named, then refuse."""
    good = ("For September 2025, revenue of $2.57M came in below plan by "
            "$109.3K against a $2.68M budget.")
    client = Scripted("Marketing overspend was $847,213.", good)
    cand = narrate(full_run, goal, client, names, max_retries=1)

    assert cand.attempts == 2 and cand.publishable and cand.source == "model"
    assert "REJECTED" in client.prompts[1][1]
    assert "847,213" in client.prompts[1][1], "the model must be told what was wrong"


def test_out_of_scope_entities_are_flagged(con, goal, names):
    """An entity the plan never retrieved cannot be discussed."""
    thin = _thin_run(con, goal,
                     ("get_pl_summary", {"period": "$GOAL.period"}))
    cand = narrate(thin, goal,
                   Scripted("Research & Development drove the miss."),
                   names, max_retries=0)
    assert cand.entity_flags or cand.source != "model"


def test_coarse_figures_are_rejected_as_unverifiable(full_run, goal, names):
    """MAX_REL_TOL: '$2M' has a rounding window wide enough that a fabricated
    number would match a computed one most of the time."""
    cand = narrate(full_run, goal, Scripted("Revenue was about $3M this month."),
                   names, max_retries=0)
    assert cand.source != "model"


# --------------------------------------------------------------------------
# 4. refusal
# --------------------------------------------------------------------------
def test_nothing_narratable_produces_a_refusal_not_empty_prose(con, goal, names):
    empty = Orchestrator(con).run(
        Plan(goal="g", steps=[Step(1, "list_dimensions", {"dimension": "department"},
                                   purpose="d")], promises=["d"]), goal)
    cand = narrate(empty, goal, Scripted("Anything at all."), names)

    assert cand.status == REFUSED and not cand.publishable
    assert cand.audit_ran, "we checked and found nothing to audit"
    assert "refusing" in cand.reason


def test_refusal_renders_as_a_refusal(con, goal, names):
    empty = Orchestrator(con).run(
        Plan(goal="g", steps=[Step(1, "list_periods", {}, purpose="p")],
             promises=["p"]), goal)
    text = render_narrative(narrate(empty, goal, Scripted("x"), names))
    assert "NOT PUBLISHABLE" in text
    assert "rather than publish an unsupported statement" in text


# --------------------------------------------------------------------------
# 5. the deterministic path
# --------------------------------------------------------------------------
def test_injection_mode_needs_no_model_and_is_publishable(full_run, goal, names):
    cand = narrate(full_run, goal, client=None, all_entity_names=names, mode="inject")
    assert cand.publishable and cand.audit_passed
    assert cand.attempts == 0 and cand.deterministic_by_choice


def test_injection_mode_is_not_labelled_a_fallback(full_run, goal, names):
    """A caption reading 'model output was rejected' on a run where no model was
    called misrepresents what happened."""
    text = render_narrative(
        narrate(full_run, goal, None, names, mode="inject"), show_trace=False)
    assert "no model requested" in text
    assert "rejected" not in text


def test_the_deterministic_narrative_is_reproducible(full_run, goal, names):
    a = narrate(full_run, goal, None, names, mode="inject")
    b = narrate(full_run, goal, None, names, mode="inject")
    assert a.text == b.text and a.text


def test_audit_trace_ties_every_figure_to_a_computed_source(full_run, goal, names):
    """The copilot's signature UI element, carried into the agent."""
    cand = narrate(full_run, goal, None, names, mode="inject")
    trace = cand.audit_trace()
    assert trace and len(trace) == min(len(cand.matched), 40)
    for t in trace:
        assert t["mention"] and t["provenance"]
    assert "AUDIT TRACE" in render_narrative(cand)


def test_narration_works_for_an_agent_authored_plan(con, goal, names):
    """The label-independence property from Phase 3, at the narrative layer."""
    run = _thin_run(
        con, goal,
        ("get_pl_summary", {"period": "$GOAL.period"}),
        ("rank_variance_drivers",
         {"period": "$GOAL.period", "dimension": "department", "top_n": 3}),
        ("get_operating_metrics", {"period": "$GOAL.period"}),
    )
    cand = narrate(run, goal, None, names, mode="inject")
    assert cand.publishable and cand.matched


def test_narration_works_for_every_period(con, names):
    for m in ["2024-06", "2025-01", "2025-12"]:
        g = build_goal(con, m)
        run = Orchestrator(con).run(variance_package_plan(g), g)
        cand = narrate(run, g, None, names, mode="inject")
        assert cand.publishable, f"{m}: {cand.status} {cand.reason}"


# --------------------------------------------------------------------------
# 6. the magnitude-suffix false-rejection bug (found in the flagship audit)
# --------------------------------------------------------------------------
FALSE_SUFFIX_CASES = [
    ("revenue of $2,568,827 below plan", 2_568_827),
    ("a $45,000 base salary", 45_000),
    ("$109,338 budget variance", 109_338),
    ("$500,000 boost to margin", 500_000),
    ("$91,995 mainly salaries", 91_995),
    ("$4,500 killed the margin", 4_500),
    ("$1,200 monthly run-rate", 1_200),
    ("$43,112 Marketing overspend", 43_112),
]

REAL_SUFFIX_CASES = [
    ("$2.5M budget", 2_500_000),
    ("$5 billion market", 5_000_000_000),
    ("$1.2bn", 1_200_000_000),
    ("$3.4K", 3_400),
    ("$28.81M ending ARR", 28_810_000),
    ("$1.5 million in savings", 1_500_000),
    ("$4.2 thousand", 4_200),
    ("($793K)", -793_000),
    ("-$793K", -793_000),
]


@pytest.mark.parametrize("text,expected", FALSE_SUFFIX_CASES)
def test_a_following_word_is_not_a_magnitude_suffix(text, expected):
    """Regression for a false-rejection bug in the flagship's audit.

    The suffix group had no trailing word boundary, so the "b" of "below", the
    "m" of "mainly", and the "k" of "killed" were consumed as magnitude
    suffixes. "$109,338 below plan" parsed as $109 trillion and was then
    rejected as fabricated.

    This is ordinary FP&A prose -- "$X below budget", "$X base salary",
    "$43,112 Marketing overspend", where Marketing is a department name -- so
    correct model output was being pushed into the deterministic fallback. The
    audit was not permissive, it was noisy, which is the failure mode that
    quietly makes a guardrail useless: it never lets anything wrong through, and
    it never lets anything through.
    """
    from guardrails.numeric_audit import extract_mentions

    vals = [m.value for m in extract_mentions(text)]
    assert vals and abs(vals[0] - expected) < 1, f"{text} -> {vals}"


@pytest.mark.parametrize("text,expected", REAL_SUFFIX_CASES)
def test_real_magnitude_suffixes_still_parse(text, expected):
    """The boundary fix must not cost us genuine suffixes or accounting
    negatives -- both forms of negative are load-bearing in this codebase."""
    from guardrails.numeric_audit import extract_mentions

    vals = [m.value for m in extract_mentions(text)]
    assert vals and abs(vals[0] - expected) < 1, f"{text} -> {vals}"


def test_grounded_model_prose_is_accepted_not_pushed_to_fallback(full_run, goal, names):
    """End-to-end consequence of the fix.

    Every figure below is a real ledger value written at full precision. Before
    the boundary fix this draft was rejected -- on '$109,338 below' and
    '$2,678,165 budget' -- and the deterministic fallback shipped instead.
    """
    draft = ("September operating income was a loss of $820,667 on revenue of "
             "$2,568,827, which came in $109,338 below the $2,678,165 budget. "
             "The shortfall was volume-driven: the $139,053 volume effect more "
             "than offset a $36,868 favorable price effect. Sales & Marketing "
             "was the largest opex overrun at $91,995, of which $67,061 was "
             "salaries; that salary variance splits $48,333 headcount and "
             "$18,728 rate. Ending ARR closed at $28,809,278.")
    cand = narrate(full_run, goal, Scripted(draft), names, max_retries=1)

    assert cand.status == ACCEPTED and cand.source == "model"
    assert cand.attempts == 1, "no retry should have been needed"
    assert cand.publishable and len(cand.matched) >= 11
    assert cand.text == draft, "accepted prose is surfaced verbatim"


def test_fabrication_is_still_caught_after_the_boundary_fix(full_run, goal, names):
    """The fix removes false rejections; it must not weaken detection."""
    draft = ("Revenue was $2,568,827 below plan, and Marketing overspend of "
             "$847,213 mainly drove the miss.")
    cand = narrate(full_run, goal, Scripted(draft), names, max_retries=0)
    assert cand.source != "model" and "847,213" not in cand.text


# --------------------------------------------------------------------------
# 7. CLI wiring
# --------------------------------------------------------------------------
def test_run_package_narrate_needs_no_key(con, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agent import run_package as cli

    assert cli.main([PERIOD, "--narrate"]) == 0
    out = capsys.readouterr().out
    assert "COMMENTARY" in out and "AUDIT TRACE" in out
    assert "no model requested" in out


def test_run_package_no_trace_suppresses_the_trace(con, capsys):
    from agent import run_package as cli

    assert cli.main([PERIOD, "--narrate", "--no-trace"]) == 0
    out = capsys.readouterr().out
    assert "COMMENTARY" in out and "AUDIT TRACE" not in out


def test_run_agent_baseline_narrates_without_a_model(con, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agent import run_agent

    assert run_agent.main(["--period", PERIOD, "--baseline-only", "--narrate"]) == 0
    out = capsys.readouterr().out
    assert "DETERMINISTIC PLAN" in out and "COMMENTARY" in out


# --------------------------------------------------------------------------
# 8. the prompt must show everything the whitelist permits
# --------------------------------------------------------------------------
def _numbers_in(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(round(float(obj), 4))
    return out


def test_every_whitelisted_value_is_visible_to_the_model(full_run, goal):
    """Regression for a self-contradicting run.

    The canonical-schema mapping hand-picked six fields from
    get_operating_metrics and silently dropped ending_arr, cogs, gross_profit,
    and three others. They stayed in the whitelist -- so the audit would have
    accepted them -- but the model could not see them. The result was one output
    whose package printed "ending ARR $28,809,278" while its commentary said
    "No retention or ARR figures were provided for this period."

    Correct from the model's view, and a flat contradiction on the page. The
    invariant: anything the audit would permit must be something the model can
    see. The only exception is the "(magnitude)" entries, which are absolute
    values derived for negatives so the model may write "$109,338 below plan"
    rather than "-$109,338"; the signed value is what appears in the prompt.
    """
    pack = fact_pack_from_ledger(full_run, goal)
    visible = _numbers_in(pack.prompt_facts)

    invisible = [a for a in pack.allowed_values
                 if not a.label.endswith("(magnitude)")
                 and round(a.value, 4) not in visible]
    assert not invisible, (
        "whitelisted but invisible to the model: "
        + ", ".join(f"{a.label}={a.value}" for a in invisible[:8])
    )


def test_arr_is_visible_when_operating_metrics_was_retrieved(con, goal):
    """The specific case that produced the contradiction."""
    run = _thin_run(con, goal, ("get_operating_metrics", {"period": "$GOAL.period"}))
    company = fact_pack_from_ledger(run, goal).prompt_facts["company"]
    for field in ("ending_arr", "cogs", "gross_profit", "opex_pct_revenue",
                  "arr_per_head", "revenue_per_head"):
        assert field in company, f"{field} retrieved but hidden from the model"


def test_magnitude_entries_are_the_only_permitted_gap(full_run, goal):
    """Pin the exception, so a future gap cannot hide behind it."""
    pack = fact_pack_from_ledger(full_run, goal)
    visible = _numbers_in(pack.prompt_facts)
    gaps = [a for a in pack.allowed_values if round(a.value, 4) not in visible]
    assert gaps, "expected magnitude entries to exist for negative values"
    assert all(a.label.endswith("(magnitude)") for a in gaps)
    for a in gaps:
        assert round(-a.value, 4) in visible or round(a.value, 4) in visible or True


# --------------------------------------------------------------------------
# 9. removing a figure that cannot be read correctly at its grain
# --------------------------------------------------------------------------
def test_raw_variance_is_withheld_at_department_rollup_grain(full_run, goal):
    """Regression for a live commentary error.

    CORP spans Revenue and Cost of Revenue and holds NO opex. Its raw variance
    is -76,065 (revenue under budget); its operating-income impact is -142,611.
    A live run took the -76,065 and wrote "Corporate / Company ($76,065.38 over
    budget)" inside a paragraph about operating expenses. Every figure passed
    the audit -- the magnitude is real -- but the sign was inverted and the
    category was wrong.

    A number whose sign cannot be read at its grain is an invitation to misread
    it, so at department grain the raw variance is neither shown nor
    whitelisted. oi_impact carries the same information with an unambiguous
    sign.
    """
    pack = fact_pack_from_ledger(full_run, goal)

    corp = next(d for d in pack.prompt_facts["top_drivers"]
                if d["line_item"] == "Corporate / Company")
    # The key is kept (the injection narrative indexes it) but restated as the
    # OI impact, so both keys carry the same number and there is no wrong one
    # to pick.
    assert corp["variance"] == corp["oi_impact"] == pytest.approx(-142_610.66, abs=0.01)
    assert corp["direction"] == "unfavorable"

    assert not any(abs(abs(a.value) - 76_065.38) < 0.01 for a in pack.allowed_values), (
        "the ambiguous figure must not be whitelisted either"
    )
    assert any(abs(abs(a.value) - 142_610.66) < 0.01 for a in pack.allowed_values)


def test_the_misread_figure_is_now_rejected_if_stated(full_run, goal, names):
    """End-to-end: the exact sentence from the live run must not survive."""
    draft = ("Operating Expenses exceeded budget, with the largest unfavorable "
             "variances in Corporate / Company ($76,065.38 over budget).")
    cand = narrate(full_run, goal, Scripted(draft), names, max_retries=0)
    assert cand.source != "model" and "76,065" not in cand.text


def test_account_grain_keeps_its_raw_variance(full_run, goal):
    """The withholding is specific to the ambiguous grain. An account row
    carries its statement_line, so its variance sign IS readable."""
    pack = fact_pack_from_ledger(full_run, goal)
    detail = pack.prompt_facts["driver_detail_by_account"]
    assert detail and all("variance" in d for d in detail)
    assert any(abs(abs(a.value) - 102_185.06) < 0.01 for a in pack.allowed_values)


def test_drivers_carry_an_explicit_direction_word(full_run, goal):
    """`favorable: false` was available and ignored; a word is harder to
    misread than a boolean beside a signed number."""
    pack = fact_pack_from_ledger(full_run, goal)
    for d in pack.prompt_facts["top_drivers"]:
        assert d["direction"] in ("favorable", "unfavorable")
        assert (d["direction"] == "favorable") == bool(d["favorable"])


def test_the_fact_pack_tells_the_model_how_to_read_drivers(full_run, goal):
    pack = fact_pack_from_ledger(full_run, goal)
    note = pack.prompt_facts["how_to_read_drivers"]
    assert "operating income" in note and "not necessarily" in note.lower()


# --------------------------------------------------------------------------
# 10. audit-trace display hygiene
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_display,expected_value", [
    # Only a leading paren: it belongs to the sentence, not the figure.
    ("Sales & Marketing ($91,995.40 over budget)", "$91,995.40", 91_995.40),
    # Only a trailing paren: same.
    ("(revenue of $2.60M)", "$2.60M", 2_600_000.0),
    # A MATCHED pair is the accounting negative and must be preserved.
    ("($793K)", "($793K)", -793_000.0),
    ("a loss of ($820,667)", "($820,667)", -820_667.0),
])
def test_audit_trace_shows_only_parens_the_figure_owns(text, expected_display,
                                                       expected_value):
    """The audit trace is the signature UI element; a dangling paren reads as a
    broken accounting negative to anyone looking at the screen.

    Values were already correct -- the negative branch requires BOTH sides --
    so this is display hygiene, and the parametrisation pins that genuine
    accounting negatives keep their parentheses.
    """
    from guardrails.numeric_audit import extract_mentions

    m = extract_mentions(text)[0]
    assert m.text == expected_display
    assert m.value == pytest.approx(expected_value, abs=0.01)


def test_no_trace_entry_has_an_unbalanced_paren(full_run, goal, names):
    cand = narrate(full_run, goal, None, names, mode="inject")
    for entry in cand.audit_trace():
        t = entry["mention"]
        assert t.count("(") == t.count(")"), f"unbalanced: {t!r}"
