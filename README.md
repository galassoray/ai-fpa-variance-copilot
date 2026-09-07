# AI FP&A Variance Commentary Copilot

A finance-owned tool that turns a monthly close into executive-ready variance
commentary and the deliverables that go with it — where **code computes every
number and the model only explains**, so leadership gets faster commentary
without ever trusting an AI-generated figure.

Built on 100% synthetic Series C SaaS financials. No real-company data of any kind.

**▶ [Live demo](https://ai-fpa-variance-copilot-ydenei544tse8dcgexffwv.streamlit.app)** — no login, no API key needed.

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

That line holds everywhere downstream. The PowerPoint generator, the Word
generators and the prioritisation module contain no arithmetic on a financial
value at all: there is exactly one way a figure reaches a page, and it is by
having been retrieved.

---

## What it does

### The copilot — one month, explained

Pick a reporting month and get the variance picture: P&L against plan, ranked
drivers, account-level detail, compensation split into headcount versus rate,
revenue split into volume versus price, the ARR bridge, headcount against plan.
Every figure is computed in SQL. The commentary is written by a model over those
figures and audited before it is shown.

### The close-cycle agent — a plan, executed and audited

Two modes.

**Standard monthly close** runs a hand-written plan of 14 tool calls in about
40 milliseconds, with no model and no credential. Same steps every period.

**Ask a question** hands the planning to a model. It writes a plan, static
validation gates it *before any query runs*, the orchestrator executes it, and
the narrative is audited before publication. Ask something the tools cannot
answer — *"what is our cash runway?"* — and it **declines**, rather than
answering a different question and presenting it as the one asked.

Seventeen parameterised SQL tools over materialised marts. No free-text SQL, and
no financial parameter type exists, so a model cannot pass a number into a query
even in principle. Values move between steps by symbolic reference
(`$STEP_4.rows[0].member`), resolved by the orchestrator from its own ledger —
the model never re-reads a result and retypes a figure.

### Where to look — prioritisation, computed

Ranked by impact on operating income, with the account detail, compensation
split and headcount underneath each driver. The ranking, the materiality cut and
the evidence attached to each item are all computed in SQL.

**It states no recommendation, deliberately.** A recommendation is not a
retrieved number: nothing could verify it, and the publication gate would pass it
straight through. The tool puts an analyst in front of the exact evidence a
decision needs, and leaves the judgment where it belongs.

### Deliverables

After sign-off, one run produces eight artifacts:

| Artifact | What it is |
|---|---|
| **Board deck** (`.pptx`) | 12 slides, native PowerPoint charts, appendix carrying the run ledger |
| **Flash report** (`.docx`) | Half a page for day three or four |
| **Monthly variance memo** (`.docx`) | The full commentary |
| **Budget-owner packets** (`.docx` ×5) | One per department, containing only that owner's numbers and a sign-off block |

Every document ends with an **intentionally blank** assessment section, with a
note explaining why: the tool assembles every fact and the analyst supplies the
judgment.

### Load a period

Download an Excel template — one sheet per table, an example row showing the
exact format, a reference sheet listing every valid code — fill it in, and upload
it. The reporting-month list, every table, the company aggregates and the agent
all update.

Validation is the feature. It refuses, naming the table, the reason and the row:
a period that already exists, an unknown department or account, a non-numeric
amount, a malformed month, duplicate keys. It accepts `$` and accounting
parentheses, because that is what a finance export writes.

Uploads are session-scoped: the committed dataset is never modified.

---

## What an interviewer can check

Everything below is reproducible from a clean clone.

**The audit does not let fabrications through.** `python eval/run_eval.py` runs
40 adversarial cases — sentences with known planted fakes plus clean controls —
and the audit must flag every fake and pass every control.

```
0 fabricated numbers across 5 generations; 100% adversarial catch rate
cases scored correctly: 40/40
```

A second table runs the whole pipeline with the model **deliberately sabotaged**.
When it fabricates once, the audit catches it and the retry succeeds. When it
fabricates persistently, the model is refused entirely and the deterministic
version ships. Fabricated figures reaching output: zero on every row.

**The tolerance was measured, not chosen.** `MAX_REL_TOL = 0.005` came from
running the false-verify rate at each precision: 92.9% at one significant figure,
50.6% at two, 10.7% at three, 0.3% at full precision. The residual is real and is
stated rather than hidden.

**Every figure in every artifact traces to a computed value.** The tests re-open
the generated `.pptx` and `.docx` files, read every text frame, table cell and
chart series back out, and audit them with the same auditor used on the prose.
Checking a generator against its own bookkeeping would prove only that it is
self-consistent.

**`python verify_decks.py`** checks any generated deck against ground truth
recomputed from the pandas layer — including whether it could belong to a
*different month*, which is the failure that would survive every internal check.

**526 tests.** `python -m pytest tests/ -q`

---

## Run it

```bash
pip install -r requirements.txt

# generate data, build the database, compute, validate
python src/generate_synthetic_data.py
python src/build_database.py
cd src && python run_pipeline.py
python validation/validate_data.py        # 11/11 PASS
python validation/golden_tests.py         # 11/11 PASS

# the guardrail eval
cd ../eval && python run_eval.py          # 0 fabricated; 100% catch rate

# full test gate
cd .. && python -m pytest tests/ -q       # 526 passed

# the app
streamlit run app/app.py
```

The agent from the command line, no API key required:

```bash
python src/agent/run_package.py 2025-09 --brief          # where to look
python src/agent/run_package.py 2025-09 --deck out.pptx  # the board deck
python src/agent/run_package.py 2025-09 --reports docs/  # all Word documents
python src/agent/run_agent.py --period 2025-09 --narrate # model-planned
```

Sample upload files: `python make_january.py`

---

## Layout

```
src/
  generate_synthetic_data.py   seeded synthetic dataset
  build_database.py            CSVs -> DuckDB star schema
  run_pipeline.py              the deterministic computation layer
  scenario.py                  parametric scenario inputs
  validation/validate_data.py  11 integrity checks
  roi.py                       measured / assumed / derived
  narrative/                   prompt construction, fact packs
  guardrails/                  numeric audit, entity audit
  agent/
    registry.py  tools.py      17 parameterised SQL tools
    plan.py      planner.py    plan schema, LLM planner, static validation
    orchestrator.py  ledger.py execution, append-only run ledger
    facts.py     narrate.py    ledger -> fact pack -> audited narrative
    briefing.py                computed "where to look"
    deck.py      reports.py    PowerPoint and Word generators
    gates.py     replay.py     sign-off, verified replay store
    ingest.py                  period upload: validate, preview, merge
app/app.py                     the Streamlit app
eval/                          adversarial cases and the eval harness
tests/                         526 tests
decision_log.md                why each architectural choice was made
```

---

## What it does not do

Stated plainly, because the limits are part of the design.

- **It does not recommend actions.** A recommendation cannot be verified, so it
  is not generated. Documents carry a blank assessment section instead.
- **It does not compute anything in the model.** Not in the commentary, not in
  the deck, not in the documents.
- **It does not run on real data.** Synthetic only, by design.
- **Retention is aggregate ARR-based, not cohort-based.** A real deployment
  would want cohort retention; this is a known simplification.
- **It has no production users.** The deployment path is understood —
  containerisation, provider abstraction, least-privilege credentials, a
  governed model channel — but this is a portfolio build, and claiming otherwise
  would be the same kind of unverifiable assertion the tool exists to prevent.

---

`decision_log.md` records the reasoning behind each architectural choice, and the
defects found along the way — most of which were found by running the thing, not
by reading it.
