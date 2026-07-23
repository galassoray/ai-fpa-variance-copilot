"""
test_phase2.py
==============
Pytest gate for the guarded narrative layer. These encode the guarantees that
must hold for the "zero fabricated numbers" claim to be defensible:

  * the numeric audit catches every planted fabrication (100% adversarial);
  * clean controls are not false-flagged;
  * every narrative the pipeline RETURNS passes the audit (0 orphans survive),
    including when the model persistently misbehaves (safe fallback);
  * insufficient data produces a refusal, never an invented commentary;
  * the entity audit catches out-of-scope line items.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "eval"))

from narrative import prompt as P
from narrative import generate as G
from narrative.llm_client import ReplayClient, ScriptedClient
from guardrails import numeric_audit as na
from guardrails import entity_audit as ea

import eval_scenarios as ES
import adversarial_cases as AC

_SCEN, _TABLES, _OUTPUTS = ES.build_scenarios()
_NAMES = ea.canonical_entity_names(_TABLES)
_OK = [s for s in _SCEN if s["kind"] == "ok"]
_PACK = _OK[2]["pack"]  # 2025-09


def test_adversarial_numeric_all_caught():
    for sc in _OK:
        for case in AC.make_cases(sc["pack"]):
            if case["check"] != "numeric":
                continue
            r = na.audit(case["text"], sc["pack"])
            caught = not r.passed
            assert caught == case["expect_caught"], (sc["id"], case["name"])


def test_clean_control_not_flagged():
    clean = P.injection_narrative(_PACK)
    assert na.audit(clean, _PACK).passed


def test_returned_text_always_passes_audit_when_model_misbehaves():
    clean = P.injection_narrative(_PACK)
    bad = clean + " Also $9,998,877.66 of mystery revenue and 44.4% churn."
    # persistent bad: model never complies -> pipeline must fall back safely
    client = ScriptedClient([bad, bad, bad])
    res = G.generate_commentary(_PACK, client, _NAMES, mode="audit", max_retries=1)
    assert res.status in ("fallback", "refused")
    assert na.audit(res.text, _PACK).passed
    assert res.source != "model"


def test_transient_bad_is_retried_then_accepted():
    clean = P.injection_narrative(_PACK)
    bad = clean + " Plus $7,777,777.77 of imaginary bookings."
    client = ScriptedClient([bad, clean])
    res = G.generate_commentary(_PACK, client, _NAMES, mode="audit", max_retries=1)
    assert res.attempts == 2
    assert sum(res.violations_caught) >= 1
    assert na.audit(res.text, _PACK).passed


def test_insufficient_data_refuses():
    bad_pack = [s for s in _SCEN if s["kind"] == "insufficient"][0]["pack"]
    res = G.generate_commentary(bad_pack, ReplayClient({}), _NAMES, mode="audit")
    assert res.status == "refused"
    assert res.source == "refusal"


def test_entity_audit_catches_out_of_scope():
    clean = P.injection_narrative(_PACK)
    if "Insurance" not in _PACK.allowed_entities:
        r = ea.audit_entities(clean + " Driven by the Insurance line.", _PACK, _NAMES)
        assert not r.passed
        assert "Insurance" in r.out_of_scope


def test_inject_mode_is_audit_clean():
    res = G.generate_commentary(_PACK, ReplayClient({}), _NAMES, mode="inject")
    assert res.audit_passed
    assert res.source == "injection"


def test_bare_ratio_audited_as_percent_not_dollar():
    """A model writing NRR as 0.923 must map to the retention percent, not be
    falsely absorbed by a near-zero dollar value; a fabricated ratio is caught."""
    pack = _OK[0]["pack"]
    nrr = next((a.value for a in pack.allowed_by_kind("percent")
                if "NRR" in a.label), None)
    if nrr is not None:
        r = na.audit(f"NRR was {nrr:.5f} this month.", pack)
        assert r.passed
        assert any("NRR" in lbl for _, _, lbl in r.matched)
    # a ratio not in the computed set must fail
    assert not na.audit("Retention slipped to 0.55.", pack).passed
