"""
test_agent_phase5.py
====================
Phase-5 acceptance gate: the approval gates and the replay store.

THE THREE PROPERTIES THIS SUITE PROVES
---------------------------------------
1. **Approval is not an override.** A failed audit cannot be approved. If a
   human could wave through a fabricated figure, the guarantee would be
   advisory, and the whole architecture rests on it not being.

2. **An approval is bound to what was reviewed.** It carries the hash of the
   exact package and narrative the approver saw. Approve draft A, change the
   narrative, and publication is blocked -- because otherwise "approved" decays
   into a flag someone set once, and approve-A-publish-B is invisible in the
   record.

3. **A cached run proves it is real.** Every saved artifact re-verifies its
   per-step hashes on load and is refused if edited. The one accusation this
   portfolio must withstand is "you could have typed those numbers in", and an
   unverified JSON file *is* that accusation.
"""
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import run_pipeline as rp  # noqa: E402
from agent import materialize as mz  # noqa: E402
from agent import replay as rpl  # noqa: E402
from agent import tools as _tools  # noqa: E402,F401
from agent.gates import (APPROVED, PENDING, REJECTED, REVISION_REQUESTED,  # noqa: E402
                         Approval, NotApproved, PlanRejected, PublicationPacket,
                         artifact_hash, blocking_reasons, review_plan,
                         summarize_plan)
from agent.narrate import narrate  # noqa: E402
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.packages import build_goal, variance_package_plan  # noqa: E402
from agent.plan import Plan, Step  # noqa: E402
from guardrails import entity_audit as ea  # noqa: E402

PERIOD = "2025-09"


@pytest.fixture(scope="module")
def names():
    return ea.canonical_entity_names(rp.load())


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


@pytest.fixture
def packet(run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    return PublicationPacket(run, cand)


class Scripted:
    def __init__(self, *texts):
        self.texts, self.i, self.model = list(texts), 0, "scripted"

    def complete(self, system, user):
        t = self.texts[min(self.i, len(self.texts) - 1)]
        self.i += 1
        return t


# --------------------------------------------------------------------------
# 1. approval is not an override
# --------------------------------------------------------------------------
def test_a_failed_audit_cannot_be_approved(run, goal, names):
    """If a human could wave through a fabricated figure, the numeric guarantee
    would be advisory. It is not."""
    bad = narrate(run, goal, Scripted("Marketing overspend was $847,213."),
                  names, max_retries=0)
    bad.status, bad.text, bad.audit_passed = "accepted", "x $847,213", False
    pkt = PublicationPacket(run, bad)

    assert not pkt.audit_ok
    with pytest.raises(NotApproved, match="not an override"):
        pkt.approve("ray")
    assert not pkt.published


def test_an_unaudited_narrative_cannot_be_approved(run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    cand.audit_ran = False
    pkt = PublicationPacket(run, cand)
    with pytest.raises(NotApproved):
        pkt.approve("ray")


def test_a_clean_run_can_be_approved_and_published(packet):
    assert packet.audit_ok and not packet.published
    packet.approve("ray", note="reviewed")
    assert packet.published and packet.approval.decision == APPROVED
    packet.assert_published()


# --------------------------------------------------------------------------
# 2. approval is bound to what was reviewed
# --------------------------------------------------------------------------
def test_changing_the_narrative_voids_the_approval(packet):
    """Approve draft A, publish draft B: without hash binding this is invisible
    in the record."""
    packet.approve("ray")
    assert packet.published
    before = packet.approval.artifact_hash

    packet.candidate.text += " Marketing was also a factor."

    assert packet.artifact_hash != before
    assert not packet.approval_is_current
    assert not packet.published
    with pytest.raises(NotApproved, match="changed after it was approved"):
        packet.assert_published()


def test_changing_the_package_voids_the_approval(packet):
    packet.approve("ray")
    assert packet.published

    # Restored afterwards: `run` is a module-scoped fixture, and mutating it
    # would corrupt every later test in this file.
    name = sorted(packet.result.sections)[0]
    original = packet.result.sections[name]["result_hash"]
    try:
        packet.result.sections[name]["result_hash"] = "tampered00000000"
        assert not packet.published
    finally:
        packet.result.sections[name]["result_hash"] = original
    assert packet.published, "the approval is valid again once the artifact is"


def test_the_artifact_hash_ignores_incidental_run_metadata(run, goal, names):
    """Two identical runs of the same plan must produce the same artifact hash;
    an approval that expired because a run was faster would be noise."""
    cand = narrate(run, goal, None, names, mode="inject")
    a = artifact_hash(run, cand)

    con = mz.connect_readonly()
    try:
        g2 = build_goal(con, PERIOD)
        r2 = Orchestrator(con).run(variance_package_plan(g2), g2)
        c2 = narrate(r2, g2, None, names, mode="inject")
        b = artifact_hash(r2, c2)
    finally:
        con.close()

    assert r2.ledger.run_id != run.ledger.run_id, "different runs"
    assert a == b, "same content must hash the same despite different run ids"


def test_rejection_and_revision_are_recorded_distinctly(packet):
    packet.reject("ray", note="causal claim unsupported", revision=True)
    assert packet.approval.decision == REVISION_REQUESTED
    assert not packet.published

    packet.reject("ray", note="wrong period")
    assert packet.approval.decision == REJECTED
    assert not packet.published


def test_a_fresh_packet_is_pending_not_approved(packet):
    assert packet.approval.decision == PENDING
    assert not packet.published
    with pytest.raises(NotApproved, match="no approval on record"):
        packet.assert_published()


def test_blocking_reasons_surface_what_is_wrong(run, goal, names):
    clean = PublicationPacket(run, narrate(run, goal, None, names, mode="inject"))
    assert blocking_reasons(clean) == ["awaiting human approval"]
    clean.approve("ray")
    assert blocking_reasons(clean) == []

    bad = narrate(run, goal, Scripted("Overspend of $847,213."), names,
                  max_retries=0)
    bad.audit_passed, bad.violations = False, [
        {"mention": "$847,213", "value": 847213.0, "kind": "dollar",
         "reason": "no computed dollar within tolerance"}]
    reasons = blocking_reasons(PublicationPacket(run, bad))
    assert any("rejected 1 figure" in r for r in reasons)
    assert any("847,213" in r for r in reasons)


# --------------------------------------------------------------------------
# 3. Gate 0
# --------------------------------------------------------------------------
def test_gate0_runs_after_validation_and_before_any_query(con, goal):
    """A reviewer never sees a plan that could not have run, and a rejected plan
    costs zero queries."""
    seen = {}

    class Counting:
        def __init__(self, inner):
            self._inner, self.executions = inner, 0

        def execute(self, *a, **k):
            self.executions += 1
            return self._inner.execute(*a, **k)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    counting = Counting(con)
    orch = Orchestrator(counting, check_freshness=False)
    counting.executions = 0

    def gate(plan):
        seen["plan"] = plan
        return False

    with pytest.raises(PlanRejected):
        orch.run(variance_package_plan(goal), goal, gate0=gate)

    assert seen["plan"] is not None, "the reviewer saw the plan"
    assert counting.executions == 0, "a rejected plan must cost no queries"


def test_gate0_approval_lets_the_run_proceed(con, goal):
    res = Orchestrator(con).run(variance_package_plan(goal), goal,
                                gate0=lambda plan: True)
    assert res.complete


def test_gate0_is_off_by_default(con, goal):
    """On a read-only surface it adds inspectability, not safety, so it does not
    block the default path."""
    assert Orchestrator(con).run(variance_package_plan(goal), goal).complete


def test_plan_summary_marks_runtime_bound_arguments(goal):
    """A reviewer must see which arguments are decided at execution time."""
    summary = summarize_plan(variance_package_plan(goal))
    step5 = next(s for s in summary if s["step"] == 5)
    assert step5["resolved_at_runtime"] == ["comparison", "department_id", "period"]
    # Steps 5-9 decompose every department, so the ARR bridge sits at 12.
    step12 = next(s for s in summary if s["step"] == 12)
    assert step12["tool"] == "get_arr_bridge"


def test_review_plan_rejects_an_unknown_decision(goal):
    with pytest.raises(ValueError):
        review_plan(variance_package_plan(goal), decision="MAYBE")


# --------------------------------------------------------------------------
# 4. the replay store
# --------------------------------------------------------------------------
def test_a_saved_run_round_trips(tmp_path, run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    pkt = PublicationPacket(run, cand)
    pkt.approve("ray")

    path = rpl.save_run(run, cand, pkt, label="t", directory=str(tmp_path))
    rr = rpl.ReplayedRun(rpl.load_run(path))

    assert rr.outcome == run.outcome and rr.complete
    assert set(rr.sections) == set(run.sections)
    assert rr.narrative["text"] == cand.text
    assert rr.gates["published"] is True
    assert len(rr.ledger.entries) == len(run.ledger.entries)
    assert rr.ledger.entry(5).params_resolved["department_id"] == "CORP"


def test_an_edited_run_is_refused_not_displayed(tmp_path, run, goal, names):
    """The accusation this must withstand is 'you could have typed those numbers
    in'. An unverified JSON file IS that accusation."""
    cand = narrate(run, goal, None, names, mode="inject")
    path = rpl.save_run(run, cand, None, label="t", directory=str(tmp_path))

    data = json.loads(open(path, encoding="utf-8").read())
    name = sorted(data["sections"])[0]
    row = data["sections"][name]["rows"][0]
    field = next(k for k, v in row.items() if isinstance(v, (int, float)))
    row[field] = 999_999.99
    open(path, "w", encoding="utf-8").write(json.dumps(data))

    with pytest.raises(rpl.CorruptRun, match="was modified"):
        rpl.load_run(path)


def test_an_edited_ledger_is_also_refused(tmp_path, run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    path = rpl.save_run(run, cand, None, label="t", directory=str(tmp_path))

    data = json.loads(open(path, encoding="utf-8").read())
    idx = sorted(data["ledger"]["results"])[0]
    data["ledger"]["results"][idx][0]["__injected__"] = 1
    open(path, "w", encoding="utf-8").write(json.dumps(data))

    with pytest.raises(rpl.CorruptRun):
        rpl.load_run(path)


def test_verification_can_be_bypassed_only_explicitly(tmp_path, run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    path = rpl.save_run(run, cand, None, label="t", directory=str(tmp_path))
    data = json.loads(open(path, encoding="utf-8").read())
    data["sections"][sorted(data["sections"])[0]]["result_hash"] = "0" * 16
    open(path, "w", encoding="utf-8").write(json.dumps(data))

    with pytest.raises(rpl.CorruptRun):
        rpl.load_run(path)
    assert rpl.load_run(path, verify=False)["outcome"] == run.outcome


def test_list_runs_reports_enough_to_choose_one(tmp_path, run, goal, names):
    cand = narrate(run, goal, None, names, mode="inject")
    rpl.save_run(run, cand, None, label="a", directory=str(tmp_path))
    listed = rpl.list_runs(str(tmp_path))
    assert len(listed) == 1
    r = listed[0]
    assert r["period"] == "2025-09-01" and r["steps"] == 14
    assert r["planner_model"] == "none (deterministic)" and r["has_narrative"]


def test_list_runs_on_a_missing_directory_is_empty(tmp_path):
    assert rpl.list_runs(str(tmp_path / "nope")) == []


# --------------------------------------------------------------------------
# 5. the committed demo runs
# --------------------------------------------------------------------------
def test_the_committed_demo_runs_exist_and_verify():
    """These are what the deployed app serves with no key. If one fails
    verification the public link shows nothing, so it is a build gate."""
    runs = rpl.list_runs()
    assert runs, ("no demo runs committed; build them with "
                  "python src/agent/build_demo_runs.py")
    for r in runs:
        data = rpl.load_run(r["path"])          # raises CorruptRun on tampering
        rr = rpl.ReplayedRun(data)
        assert rr.complete, f"{r['label']} is not a complete run"
        assert rr.narrative.get("text"), f"{r['label']} has no commentary"
        assert rr.gates.get("published"), f"{r['label']} is not approved"


def test_demo_runs_are_reproducible(tmp_path):
    """A cached artifact is only trustworthy if rebuilding it reproduces the
    same figures. Deterministic plan, no model, seeded data."""
    from agent import build_demo_runs

    before = {r["label"]: rpl.load_run(r["path"]) for r in rpl.list_runs()}
    rebuilt_dir = str(tmp_path / "runs")
    shutil.rmtree(rebuilt_dir, ignore_errors=True)

    con = mz.connect_readonly()
    try:
        for period in build_demo_runs.DEMO_PERIODS:
            g = build_goal(con, period)
            res = Orchestrator(con).run(variance_package_plan(g), g)
            label = f"deterministic-{period}"
            if label not in before:
                continue
            old = before[label]
            for name, sec in old["sections"].items():
                assert res.sections[name]["result_hash"] == sec["result_hash"], (
                    f"{label}/{name} diverged from the committed artifact"
                )
    finally:
        con.close()


def test_demo_runs_cover_a_period_without_ttm_retention():
    """2024-06 predates any trailing twelve months, which is where the
    narrative previously raised KeyError."""
    labels = {r["label"] for r in rpl.list_runs()}
    assert any("2024" in lab for lab in labels), (
        "a pre-TTM period should be in the demo set; it is the edge case"
    )


# --------------------------------------------------------------------------
# 6. the app page
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    """The real Streamlit app, run headless.

    A page that raises on load is worse than no page: the deployed link is the
    artifact an interviewer actually clicks, and the failure would surface in
    front of them rather than in CI.
    """
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    sys.path.insert(0, os.path.join(HERE, "..", "eval"))
    at = AppTest.from_file(os.path.join(HERE, "..", "app", "app.py"),
                           default_timeout=180)
    at.run()
    assert not at.exception, f"app failed to load: {at.exception}"
    return at


def test_the_agent_page_is_registered(app):
    assert "Close-cycle agent" in app.sidebar.radio[0].options


def test_the_agent_page_renders_the_standard_close(app):
    at = app.sidebar.radio[0].set_value("Close-cycle agent").run()
    assert not at.exception, f"agent page raised: {at.exception}"

    # The "verified replay" TAB was removed: it rendered the same package as
    # the standard close with an integrity banner most readers could not
    # interpret, so three modes where two looked identical cost more in
    # confusion than the point was worth. The replay MACHINERY is unchanged
    # and still covered by the round-trip and tamper tests above -- that is
    # what the claim rests on; it just no longer needs a tab.
    assert at.code, "the package should render"
    package = at.code[0].value
    assert "VARIANCE PACKAGE" in package and "RUN LEDGER" in package
    assert "no model in the loop" in package


def _agent_live_page(monkeypatch, key: str | None):
    """A fresh app instance with the key environment set deliberately.

    The module-scoped `app` fixture cannot be reused here: key detection reads
    os.environ, so this test has to control it. The first version asserted the
    no-key path without clearing the environment and passed only on machines
    that happened to have no key set -- it failed on a developer shell where
    OPENAI_API_KEY was exported, which is the common case, not the odd one.
    """
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", key)

    sys.path.insert(0, os.path.join(HERE, "..", "eval"))
    at = AppTest.from_file(os.path.join(HERE, "..", "app", "app.py"),
                           default_timeout=180)
    at.run()
    at = at.sidebar.radio[0].set_value("Close-cycle agent").run()
    mode = next(r for r in at.radio
                if r.options and "Ask a question" in r.options)
    return mode.set_value("Ask a question").run()


def test_live_mode_without_a_key_informs_rather_than_crashes(monkeypatch):
    at = _agent_live_page(monkeypatch, key=None)

    assert not at.exception
    assert any("cached runs above need no key" in i.value for i in at.info)
    assert any("API key" in t.label for t in at.text_input)
    assert not any(b.label == "Plan and run" for b in at.button), (
        "no run control should be offered without a credential"
    )


def test_live_mode_with_a_key_offers_the_run_controls(monkeypatch):
    """The complement, so the pair pins the actual branch rather than whatever
    the developer's shell happens to contain."""
    at = _agent_live_page(monkeypatch, key="sk-not-a-real-key")

    assert not at.exception
    assert any(b.label == "Plan and run" for b in at.button)
    assert not any("cached runs above need no key" in i.value for i in at.info)
    assert not any("API key" in t.label for t in at.text_input)
