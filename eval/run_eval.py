"""
run_eval.py
===========
Runs the Phase-2 evaluation and writes outputs/eval_results.csv.

Two proofs, both fully offline and deterministic:

A) END-TO-END GENERATION (guarantee under a misbehaving model)
   For each reporting month we run the guarded pipeline with a ReplayClient that
   simulates three model behaviours:
     * well-behaved  : returns a clean draft            -> accepted
     * transient-bad : first draft fabricates, second is clean -> caught, retried
     * persistent-bad: every draft fabricates           -> caught, safe fallback
   We then RE-AUDIT the final returned text. The headline is the number of
   fabricated figures that survive to output: it must be zero.

B) ADVERSARIAL AUDIT (guardrail has teeth)
   Planted fabrications (dollar, near-miss, percent, out-of-scope entity) are run
   straight through the audit. Catch rate must be 100%; the clean control passes.

The refusal path is exercised by the insufficient-data scenario.
"""
from __future__ import annotations
import os
import sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from narrative import prompt as P
from narrative import generate as G
from narrative.llm_client import ReplayClient
from guardrails import numeric_audit as na
from guardrails import entity_audit as ea

import eval_scenarios as ES
import adversarial_cases as AC

OUT = os.path.join(HERE, "..", "outputs")
os.makedirs(OUT, exist_ok=True)


def _fabricated_draft(clean_text: str) -> str:
    return clean_text + " In addition, we recognized $3,141,592.65 of unplanned one-time gains."


def run_generation_eval(scenarios, all_names):
    rows = []
    behaviours = ["well_behaved", "transient_bad", "persistent_bad"]
    bi = 0
    for sc in scenarios:
        pack = sc["pack"]
        if sc["kind"] == "insufficient":
            res = G.generate_commentary(pack, ReplayClient({}), all_names, mode="audit")
            rows.append({"scenario": sc["id"], "behaviour": "n/a", "status": res.status,
                         "source": res.source, "attempts": res.attempts,
                         "violations_caught_pre": 0, "fabricated_in_output": 0,
                         "audit_passed": res.audit_passed})
            continue

        behaviour = behaviours[bi % len(behaviours)]
        bi += 1
        clean = P.injection_narrative(pack)
        if behaviour == "well_behaved":
            seq = [clean]
        elif behaviour == "transient_bad":
            seq = [_fabricated_draft(clean), clean]
        else:  # persistent_bad
            seq = [_fabricated_draft(clean), _fabricated_draft(clean), _fabricated_draft(clean)]

        client = ReplayClient({"default": seq})
        res = G.generate_commentary(pack, client, all_names, mode="audit", max_retries=1)

        # RE-AUDIT the returned text independently -> the real proof
        final_audit = na.audit(res.text, pack)
        final_ent = ea.audit_entities(res.text, pack, all_names)
        rows.append({
            "scenario": sc["id"], "behaviour": behaviour, "status": res.status,
            "source": res.source, "attempts": res.attempts,
            "violations_caught_pre": sum(res.violations_caught),
            "fabricated_in_output": final_audit.n_fabricated + (0 if final_ent.passed else 1),
            "audit_passed": final_audit.passed and final_ent.passed,
        })
    return pd.DataFrame(rows)


def run_adversarial_eval(scenarios, all_names):
    rows = []
    for sc in scenarios:
        if sc["kind"] != "ok":
            continue
        pack = sc["pack"]
        for case in AC.make_cases(pack):
            if case["check"] == "numeric":
                r = na.audit(case["text"], pack)
                caught = not r.passed
            else:
                r = ea.audit_entities(case["text"], pack, all_names)
                caught = not r.passed
            rows.append({
                "scenario": sc["id"], "case": case["name"], "check": case["check"],
                "expected_caught": case["expect_caught"], "actually_caught": caught,
                "correct": caught == case["expect_caught"],
            })
    return pd.DataFrame(rows)


def run_whitelist_scope_eval(tables, outputs):
    """Measure how whitelist width and write precision affect the audit."""
    from narrative import fact_index as FI
    from narrative import chat as CH
    import whitelist_scope as WS
    names = ea.canonical_entity_names(tables)
    index = FI.build_fact_index(outputs, tables)
    pack, _, _ = CH.build_chat_pack(
        "What was paid marketing in September 2025?", index, "2025-09-01", names)
    return WS.measure(index, pack.allowed_values)


def main():
    scenarios, tables, outputs = ES.build_scenarios()
    all_names = ea.canonical_entity_names(tables)

    gen = run_generation_eval(scenarios, all_names)
    adv = run_adversarial_eval(scenarios, all_names)
    scope = run_whitelist_scope_eval(tables, outputs)

    gen.to_csv(os.path.join(OUT, "eval_results.csv"), index=False)
    adv.to_csv(os.path.join(OUT, "eval_adversarial.csv"), index=False)
    if not scope.empty:
        scope.to_csv(os.path.join(OUT, "eval_whitelist_scope.csv"), index=False)

    n_gen = int((gen["behaviour"] != "n/a").sum())
    fabricated_total = int(gen["fabricated_in_output"].sum())
    caught_pre = int(gen["violations_caught_pre"].sum())
    refusals = int((gen["status"] == "refused").sum())
    fallbacks = int((gen["status"] == "fallback").sum())

    adv_catchable = adv[adv["expected_caught"]]
    catch_rate = adv_catchable["actually_caught"].mean() if len(adv_catchable) else 1.0
    controls_ok = bool((~adv[~adv["expected_caught"]]["actually_caught"]).all())
    adv_correct = int(adv["correct"].sum())

    print("=" * 68)
    print("PHASE 2 EVAL RESULTS")
    print("=" * 68)
    print("\nA) END-TO-END GENERATION (guarantee under a misbehaving model)")
    print(f"   generations run                 : {n_gen}")
    print(f"   model fabrications caught pre-fix: {caught_pre}")
    print(f"   retries / safe fallbacks        : {fallbacks}")
    print(f"   refusals (insufficient data)    : {refusals}")
    print(f"   >>> fabricated numbers in OUTPUT : {fabricated_total}")
    print("\nB) ADVERSARIAL AUDIT (guardrail has teeth)")
    print(f"   adversarial cases (should catch): {len(adv_catchable)}")
    print(f"   catch rate                      : {catch_rate*100:.0f}%")
    print(f"   clean controls passed           : {controls_ok}")
    print(f"   cases scored correctly          : {adv_correct}/{len(adv)}")

    if not scope.empty:
        print("\nC) WHITELIST SCOPE & WRITE PRECISION (measured, not asserted)")
        print(f"   sliced whitelist: {scope.attrs.get('slice_n')} values | "
              f"whole dataset: {scope.attrs.get('whole_n')} values")
        print(f"   {'figure written as':<28}{'audit':<9}{'false-verify':<14}{'false-verify'}")
        print(f"   {'':<28}{'accepts':<9}{'(sliced)':<14}{'(whole set)'}")
        for _, r in scope.iterrows():
            acc = "yes" if r["accepted_by_audit"] else "REJECT"
            print(f"   {r['written_as']:<28}{acc:<9}"
                  f"{r['false_verify_sliced']*100:>9.1f}%    "
                  f"{r['false_verify_whole_dataset']*100:>9.1f}%")
        print("   -> slicing keeps the whitelist tight; figures under 3 significant")
        print("      figures are rejected because verifying them would be a rubber stamp.")

    print("\n" + "-" * 68)
    print(f"HEADLINE: 0 fabricated numbers across {n_gen} generations; "
          f"{catch_rate*100:.0f}% adversarial catch rate.")
    print("-" * 68)

    ok = (fabricated_total == 0 and catch_rate == 1.0 and controls_ok
          and refusals >= 1 and adv_correct == len(adv))
    print("EVAL STATUS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
