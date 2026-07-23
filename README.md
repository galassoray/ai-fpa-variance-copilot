# AI FP&A Variance Commentary Copilot

A finance-owned tool that turns a monthly close into executive-ready variance
commentary — where **code computes every number and the model only explains**, so
leadership gets faster commentary without ever trusting an AI-generated figure.

Built on 100% synthetic Series C SaaS financials. No real-company data of any kind.

---

## The one idea

Large language models fabricate numbers. In finance that is disqualifying. So the
architecture draws a hard line:

```
  data ──▶ deterministic computation (Python/SQL) ──▶ FACTS ──▶ LLM ──▶ prose
                       every decision-facing number             (narration only)
                       is computed here                              │
                                                                     ▼
                                            post-generation NUMERIC AUDIT
                                    rejects any $ / % figure not in the computed set
                                       │ pass ──▶ accept
                                       │ fail ──▶ re-prompt, then safe fallback
```

The model receives finished facts and writes prose. A deterministic audit then
verifies every dollar and percentage it wrote against the computed set. Anything
that matches nothing is a fabrication and never reaches output — the guarantee
rests on the audit catching the model, not on the model behaving.

---

## What's built

**Phase 1 — deterministic foundation (complete).**
Synthetic SaaS dataset (~$30M ending ARR, ~150 employees, 24 months, star schema
in DuckDB) with six business stories engineered into FY2025. A sign-aware variance
engine, a SaaS KPI layer (ARR bridge, NRR/GRR, margins, per-head), and driver
decompositions (comp → headcount vs rate; revenue → volume vs price).
**11/11 validation checks and 11/11 golden tests pass**; salaries tie to headcount
to the penny, decompositions reconcile, the ARR bridge and operating-income
identities hold.

**Phase 2 — guarded narrative layer + eval (complete).**
A fact pack that packages computed facts and the whitelist of allowed figures; a
precision-aware numeric audit; an entity audit; a provider-agnostic LLM client
(real Anthropic + deterministic replay); and orchestration that re-prompts on a
bad draft and falls back to a deterministic narrative rather than surface a
fabrication. The eval proves it:

```
HEADLINE: 0 fabricated numbers across 5 generations; 100% adversarial catch rate.
```

- End-to-end, under a deliberately misbehaving model (transient- and
  persistent-bad drafts), **zero fabricated figures survive to output**; bad drafts
  are caught, retried, and safely replaced.
- Planted adversarial fabrications (made-up dollars, near-miss figures, invented
  percentages, out-of-scope line items) are caught **100%**; clean controls pass.
- Insufficient data produces a **refusal**, never a guess.

**Phase 3 — Streamlit demo (complete).**
A five-page app (Overview, Variance, Commentary, Guardrails & Eval, Decision log)
that runs the exact Phase-1 computation layer and Phase-2 guardrails. It is safe
to share as a public link — the computed metrics, the deterministic narrative, and
the full "0 fabricated numbers" eval all run with **no API key**. Live model
generation (Anthropic or OpenAI) is an optional enhancement that activates only
when a key is present, for a screen-share. The signature element is the **audit
trace**: every figure in the commentary renders as a chip tied back to the
computed value it matched. Run with `streamlit run app/app.py`; see `DEPLOY.md`.

**Phase 4 — live scenario editing, forecast projection, guarded chat (complete).**

*Scenario editing.* Every **input** is editable from the sidebar — headcount plan,
average comp, spend baselines, churn, the story multipliers, starting ARR. Nothing
**derived** is: salary lines stay computed as headcount × comp/12, revenue stays
derived from the ARR bridge. Move an input and the whole app recomputes in ~0.2s
with **all 11 validation checks still passing** — asserted for arbitrary scenarios,
not just the baseline, so changing numbers live in an interview can't break the
reconciliations. The seed is held fixed, so a change is attributable to the input
you moved rather than to noise.

*Forecast.* Actual vs budget over time, plus a projected continuation. The method
is an explicit choice — linear trend (with R²), run-rate, or driver-based ARR
continuation — each labeled with what it assumes and ignores. Code computes the
number; you pick the method; the model never selects it.

*Guarded chat.* Ask anything about the dataset. A precomputed fact index holds
every computed number (6,528 facts across all 24 months, every department and
account, YTD, decompositions, retention); deterministic code selects the relevant
slice per question. Every answer is audited like the commentary, and it **refuses
rather than estimates** — there's no deterministic fallback for an arbitrary
question, and a wrong number is worse than no answer.

```
HEADLINE: 0 fabricated numbers across 5 generations; 100% adversarial catch rate.

WHITELIST SCOPE & WRITE PRECISION (measured, not asserted)
  figure written as        audit accepts   false-verify (sliced)   (whole set)
  1 sig fig   e.g. $2M         REJECT              92.9%              100.0%
  2 sig figs  e.g. $1.7M       REJECT              50.6%               98.8%
  3 sig figs  e.g. $1.73M      yes                 10.7%               66.8%
  6 sig figs  e.g. $1,730,055  yes                  0.3%                3.8%
```

That table is the honest part. It measures two things instead of asserting them:
sending the chat the **whole** dataset rather than a slice would make the audit
~6× more likely to wave a fabrication through, and a figure written coarsely
enough (`$2M`) can't be verified at all — so the audit **rejects** those rather
than rubber-stamping them. Found by measuring; the earlier eval only planted
full-precision fabrications, which was the easy case.

---

## Run it

```bash
pip install -r requirements.txt

# Phase 1: generate data, build DB, compute, validate
python src/generate_synthetic_data.py
python src/build_database.py
cd src && python run_pipeline.py
python validation/validate_data.py        # 11/11 PASS
python validation/golden_tests.py         # 11/11 PASS

# Phase 2: the guarded narrative eval
cd ../eval && python run_eval.py          # 0 fabricated numbers; 100% catch rate

# full test gate
cd .. && pytest -q                        # Phase 1 + Phase 2

# Phase 3/4: the interactive demo
streamlit run app/app.py                  # opens the 6-page app in your browser
```

A live model demo (optional) needs `ANTHROPIC_API_KEY`; everything above runs
fully offline and deterministically.

---

## Layout

```
ai-fpa-variance-copilot/
├── decision_log.md          # the "why" behind every choice (read this)
├── src/
│   ├── scenario.py          # every editable INPUT lives here          (Phase 4)
│   ├── generate_synthetic_data.py
│   ├── build_database.py
│   ├── calculations/        # variance engine, SaaS metrics, decomposition, projection
│   ├── validation/          # 11 reconciliation checks + hand-checkable golden tests
│   ├── narrative/           # fact_pack, prompt, llm_client, generate  (Phase 2)
│   │                        # fact_index, chat                         (Phase 4)
│   └── guardrails/          # numeric_audit, entity_audit             (Phase 2)
├── eval/                    # scenarios, adversarial cases, run_eval  (Phase 2)
│                            # whitelist_scope (precision measurement) (Phase 4)
├── tests/                   # pytest gate for both phases
├── app/                     # Streamlit demo (Phase 3)
└── outputs/                 # computed tables + validation & eval results
```

## Guardrail guarantees (what an interviewer can check)

- Every decision-facing number is computed in `src/calculations/`; the LLM never does arithmetic.
- `guardrails/numeric_audit.py` verifies every `$`/`%` figure in the prose against the computed whitelist within a precision-aware tolerance (honest rounding passes; fabrication fails).
- Whatever the pipeline returns has passed the audit **by construction** — a persistently misbehaving model yields a deterministic fallback or a refusal, never a fabricated figure.
- A figure too coarse to verify (`$2M`) is **rejected**, not blessed — verification there would be a rubber stamp.
- Editing an input can never break a reconciliation: all 11 checks are asserted for arbitrary scenarios.
- The proof is reproducible offline: `eval/run_eval.py`.

Production path: the local prototype proves the logic; production re-points the
same narrative client at a governed channel (Bedrock / Azure OpenAI / internal
gateway) with least-privilege read access, logging, and data minimization. Because
nothing the model emits is trusted as a number, audit exposure is bounded.
