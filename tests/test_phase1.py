"""
test_phase1.py
==============
Pytest wrapper around the deterministic validation + golden checks so the whole
Phase-1 foundation is CI-gateable. Phase 2 builds only on green here.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from validation import validate_data as vd
from validation import golden_tests as gt


def test_all_validation_checks_pass():
    df = vd.run_checks()
    failed = df[df["status"] == "FAIL"]
    assert len(failed) == 0, f"failed checks: {list(failed['check'])}"


def test_all_golden_tests_pass():
    tests = [v for k, v in sorted(vars(gt).items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()


def test_arr_bridge_specific_month():
    s = gt.T["fact_saas_metrics"].query("month=='2025-09-01'").iloc[0]
    calc = (s["starting_arr"] + s["new_arr"] + s["expansion_arr"]
            - s["contraction_arr"] - s["churned_arr"])
    assert abs(calc - s["ending_arr"]) <= 0.01


def test_expense_favorability_is_sign_aware():
    line = gt.LINE
    exp = line[line["is_expense"] == 1]
    assert (exp["favorable_ab"] == (exp["actual"] < exp["budget"])).all()
