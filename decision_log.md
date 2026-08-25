# Decision Log — AI FP&A Variance Commentary Copilot

Terse, defensible rationale for each material architectural choice. Every entry is
something I decided and can defend under questioning. Format: context → options →
choice & why → tradeoff → production note.

---

## Phase 1 — deterministic foundation

### Decision: 100% synthetic data, generated in-repo from a fixed seed
- Context: portfolio must be publicly shareable with zero exposure of any prior/current employer's data.
- Options considered: (A) public sample financials; (B) hand-built spreadsheet; (C) seeded synthetic generator.
- Choice & why: (C). A seeded generator (`SEED=20260625`) reproduces the dataset bit-for-bit, lets me engineer specific business stories to test the engine, and carries no provenance risk.
- Tradeoff accepted: synthetic data isn't "real"; I mitigate by making unit economics realistic and the stories defensible.
- Production note: in a real deployment the same schema is populated from the GL/ERP via a governed read path; nothing else changes.

### Decision: amounts stored as positive magnitudes; favorability derived from account metadata
- Context: a variance of "-$50K" is favorable for an expense and unfavorable for revenue. Sign alone is ambiguous.
- Options considered: (A) signed amounts everywhere; (B) magnitudes + an `oi_sign` per account.
- Choice & why: (B). Each account carries `oi_sign` (+1 revenue, −1 expense). Favorability = `oi_sign × variance > 0`. This makes favorable/unfavorable a property of the chart of accounts, not of a raw sign a reader has to interpret.
- Tradeoff accepted: one extra metadata column and a join.
- Production note: mirrors how a real COA tags natural balances.

### Decision: salary lines generated as headcount × (avg_comp / 12)
- Context: I want the compensation variance to decompose exactly into a headcount effect and a rate effect.
- Options considered: (A) salaries as an independent random line; (B) salaries derived from the headcount table.
- Choice & why: (B). Because the GL salary line IS headcount × comp, the two-way decomposition reconciles to the penny (validation check #9 = $0.00). A decomposition that doesn't tie to the GL is not defensible.
- Tradeoff accepted: salaries are smoother than reality (no off-cycle bonuses modeled).

### Decision: company scaled to ~$30M ending ARR (not the originally proposed ~$20M)
- Context: the first spec (~$20M ARR, ~150 employees) implied salaries ≈ revenue, i.e. ~−75% operating margin — structurally broken unit economics that would undermine the whole narrative.
- Options considered: (A) keep $20M and accept implausible margins; (B) cut headcount; (C) raise ARR to a healthy Series C efficiency (~$200K ARR/head).
- Choice & why: (C). `STARTING_ARR = 21,000,000` → ~$30.2M ending ARR, ~82% gross margin, ~$192K ARR/head. Set `STARTING_ARR = 13_500_000` to revert to the original heavier-burn profile.
- Tradeoff accepted: less dramatic burn story; gained a company an interviewer won't reject on sight.

### Decision: DuckDB over SQLite
- Context: workload is analytical aggregation over fact tables joined to dimensions.
- Choice & why: DuckDB is columnar and vectorized — the right engine for group-by/rollup, and it demonstrates the SQL-shaped modeling an FP&A analyst needs. Reversible; the SQL is portable.
- Tradeoff accepted: a heavier dependency than SQLite.

### Decision: % variance is NULL ("n/m") below a $100 budget floor
- Context: dividing by a near-zero budget base produces meaningless or infinite percentages.
- Choice & why: a `MATERIALITY_FLOOR` guards the denominator; below it the % is "not meaningful" rather than a garbage number. Keeps the audit and the UI honest.

---

## Phase 2 — guarded narrative layer + eval

### Decision: layer TWO guardrail strategies (injection + post-hoc audit), audit always on
- Context: need to guarantee no fabricated figure reaches a decision-maker, while still producing fluent commentary.
- Options considered: (A) injection only — model emits placeholders, code substitutes values (can't type a digit, but stiff prose and the model can still misattribute); (B) free generation + post-hoc numeric audit only (fluent, but relies on the audit); (C) both, layered.
- Choice & why: (C). The post-generation numeric audit is the always-on guarantee — it rejects any figure not in the computed set. Injection mode is available as a strict setting AND as the deterministic fallback when a model's draft fails the audit twice. The "zero fabrication" claim rests on the audit catching the model, not on hoping the model behaves.
- Tradeoff accepted: two code paths to maintain; worth it because the guarantee no longer depends on model compliance.
- Production note: identical behaviour on a governed channel; the audit runs client-side on whatever the model returns, so provider choice doesn't affect the guarantee.

### Decision: precision-aware tolerance in the numeric audit
- Context: a model legitimately rounds — "$1.7M" for $1,730,054.57 must pass; "$2.1M" must fail. A fixed tolerance can't do both.
- Options considered: (A) exact match (rejects all rounding — unusable); (B) fixed relative tolerance (either too loose for big numbers or too tight for rounded ones); (C) tolerance derived from the written number's least-significant digit.
- Choice & why: (C). I infer the least-significant place of the *mention* and allow a match only within half a unit of that place. "$1.7M" (LSD $0.1M) matches anything in ±$50K; "$1.73M" (LSD $0.01M) only within ±$5K; "$1,730,055" within ±$1. Coarser mentions get a bounded-but-coarser window.
- Tradeoff accepted: a very coarse mention (e.g. "$2M" at one sig fig) has a wide window and can match a nearby computed value; documented, with a knob to tighten. This is honest rounding tolerance, not a hole.

### Decision: audit only figures carrying a financial signal ($, %, k/M/B, comma, or decimal)
- Context: an early false positive flagged the year "2025" as a fabricated dollar figure.
- Options considered: (A) audit every number ≥ some magnitude (false-flags years/counts); (B) special-case years; (C) require a financial signal.
- Choice & why: (C). Decision-facing figures are written as currency or percentages — with a `$`, a `%`, a magnitude suffix, comma-grouping, or a decimal. Bare ungrouped integers (years, ordinals, small counts) are contextual and not audited as currency. Models format money conventionally, so this doesn't weaken fabrication detection.
- Tradeoff accepted: a bare, unformatted large integer (e.g. "spent 500000") wouldn't be audited as a dollar figure; in practice models don't write money that way, and headcount/counts are covered separately.

### Decision: entity audit scoped to known-but-out-of-scope names
- Context: a subtler fabrication than a wrong number is naming a real account/department that wasn't in the facts (false attribution).
- Options considered: (A) NER on free text (noisy, over-flags); (B) check the model's named entities against the canonical COA, flag any real name not in this month's pack.
- Choice & why: (B). Fully invented names aren't enumerable, but the meaningful, catchable failure is importing a real out-of-scope line item — that's what this catches. Generic names that recur across departments ("Salaries") are excluded from the strict check to avoid ambiguity.
- Tradeoff accepted: doesn't catch a wholly invented department name; low-value compared to the false-attribution case.

### Decision: offline, deterministic eval as the proof (real API optional)
- Context: the eval must be reproducible in CI and runnable by a reviewer with no API key, yet still prove the guarantee holds when the model misbehaves.
- Options considered: (A) live API calls in the eval (non-deterministic, needs a key, flaky in CI); (B) deterministic replay + adversarial audit.
- Choice & why: (B). A `ReplayClient` simulates well-behaved, transient-bad, and persistent-bad model behaviour; the pipeline must catch fabrications, retry, and fall back safely, and the FINAL returned text is re-audited independently. Separately, planted adversarial fabrications are run straight through the audit for a catch-rate number. A real `AnthropicClient` is provided for a live demo with a key.
- Tradeoff accepted: the offline "clean" drafts are built via injection, so end-to-end fluency of a real model isn't shown offline; the guarantee (what survives to output) is what's proven, which is the point.
- Production note: swapping `AnthropicClient` for the replay client is the only change between the live demo and the CI eval.

### Decision: refusal on insufficient data, never a guess
- Context: if a period lacks computed facts, the safe behaviour is to say so.
- Choice & why: the fact pack carries a `status`; an `insufficient_data` pack short-circuits to a refusal string before any model call. Proven by the `insufficient_2099` eval scenario.
- Tradeoff accepted: a period with partial data yields no commentary rather than partial commentary; conservative by design.

---

## Phase 3 — Streamlit demo

### Decision: one app that is safe to share as a link AND live for a screen-share
- Context: interviews split into two modes — sending a clickable link, or screen-sharing a live demo — and I can't know which in advance.
- Options considered: (A) a public deploy with my key embedded (exposes the key, lets strangers spend my credits); (B) two separate apps; (C) one app that defaults to deterministic/key-free content, with live model generation gated on a key being present.
- Choice & why: (C). Everything a viewer needs to be convinced — computed metrics, the deterministic narrative, and the "0 fabricated numbers" eval — runs with no key. Live generation activates only when a key is found (pasted in session → `st.secrets` → env), so the same codebase serves a safe public link and a compelling live screen-share.
- Tradeoff accepted: the public link doesn't show a live model writing prose; the eval page carries that proof instead, which is stronger than a single live sample.
- Production note: the key-lookup order mirrors a real secrets story — nothing is hard-coded; a governed deploy injects credentials via the platform's secret store.

### Decision: the app runs the same computation layer the tests validate (no shadow logic)
- Context: a demo that recomputes numbers its own way would undermine the "code computes every number" claim.
- Choice & why: the app boots by calling the exact Phase-1 pipeline (`generate → build → run_pipeline`) in a cached bootstrap and reads its outputs. No figure is computed in the UI layer. The eval page calls the same `run_eval` functions used at the command line.
- Tradeoff accepted: a few seconds of cold-start compute on first load; acceptable and cached thereafter.

### Decision: bare decimals below 1 are audited as ratios, not sub-dollar amounts
- Context: during the live demo a model wrote retention as `0.92329` instead of `92.3%`. The audit treated it as ~$0.92 and matched it to a computed value of $0.00 (a department whose headcount hit plan that month), so it was falsely marked "verified" and mislabeled.
- Options considered: (A) leave it — the number was technically in-range of a near-zero value; (B) skip bare sub-dollar decimals entirely (but then a mis-formatted ratio reaches output un-audited); (C) classify a bare decimal below 1 (no `$`, comma, or magnitude suffix) as a ratio and audit it against the percent set.
- Choice & why: (C). A sub-1 bare decimal in finance commentary is a ratio, not a dollar figure. It now matches the correct computed percentage (NRR/GRR/margin), and a fabricated ratio is still caught. The model prompt was also tightened to require `%` form, so this path is a backstop, not the primary route.
- Tradeoff accepted: a bare decimal ≥ 1 is still read as dollars (rare ambiguity; models write "$1.5M"). Reversible by removing the reclassification branch in `extract_mentions`.

### Decision: the audit trace is the signature UI element
- Context: the differentiator isn't a dashboard — every variance tool has one — it's that the model's numbers are verified.
- Choice & why: each figure in the narrative renders as a chip tied back to the computed value it matched (green = verified); unverified figures and out-of-scope entities render red. This makes the guarantee visible rather than asserted, and the "audit any commentary" box lets a skeptic type a fabricated number and watch it get flagged live.
- Tradeoff accepted: spends the design budget on one element and keeps everything else deliberately quiet; that restraint is the point.

### Decision: nothing user-facing depends on the DuckDB file
- Context: the Guardrails page crashed on a fresh install with `Cannot open database ... does not exist`. The eval loaded the canonical dataset from `fpa.duckdb` — a build artifact that is gitignored and stripped from the package, so it is absent on any fresh extraction *and* on a cloud deployment. The same bug would have broken the deployed public link, which is the whole point of the shareable-link design.
- Why it wasn't caught: the app was boot-tested in a working directory where the database had already been built, so the tests passed against stale local state rather than a clean install. A green test suite on a dirty directory proves less than it appears to.
- Options considered: (A) commit the .duckdb (a binary build artifact in git — no); (B) have the app build the database on boot (works, but keeps a file dependency on a path that may be read-only in a hosted environment); (C) build the eval's baseline in memory from the seeded generator, since that is what defines the baseline anyway.
- Choice & why: (C), plus a self-healing `run_pipeline.ensure_database()` so the CLI paths and tests materialize the database from the packaged CSVs instead of failing with an IO error. The eval is now self-contained and identical in CI, on a laptop, and in the deployed app — which is also why its headline is unaffected by scenario edits in the UI.
- Tradeoff accepted: `load()` now has a side effect (it may build the database). It prints when it does, so the behaviour is visible rather than magic.
- Regression guard: `test_eval_does_not_depend_on_the_database_file` monkeypatches `rp.load` to raise, so any future reintroduction of the dependency fails the suite; `test_eval_baseline_matches_the_canonical_dataset` proves the in-memory baseline is the same company as the packaged one. The whole suite and every page are now verified from a clean extraction with no database present.

---

## Phase 4 — live scenario editing, forecast projection, guarded chat

### Decision: only INPUTS are editable; everything derived is recomputed
- Context: the tool needs to be altered live in an interview, with changes flowing through the whole app. The naive version — edit any cell — quietly destroys the tool's credibility.
- Options considered: (A) edit any GL cell directly; (B) edit only the inputs a planner actually sets, and recompute everything downstream; (C) both.
- Choice & why: (B). Salary lines are *generated* as headcount x comp/12, and revenue is derived from the ARR bridge. Hand-editing a salary cell breaks the tie that makes the comp decomposition reconcile, and editing revenue breaks the ARR bridge — i.e. it breaks exactly the checks an interviewer probes first. So every input lives in `scenario.py` (headcount plan, comp, spend baselines, story multipliers, ARR bridge assumptions) and the derived layer is always recomputed. All 11 validation checks are asserted to pass for *any* scenario, not just the baseline (`tests/test_phase4.py`), which is what makes live editing safe rather than a trap.
- Tradeoff accepted: you cannot type a specific number into a specific derived cell; you move the assumption that produces it. That is also the more defensible move in front of a finance audience.
- Production note: same shape as a real driver-based model — assumptions are inputs, the statements are outputs.

### Decision: the seed is held fixed across scenarios
- Context: if random noise were re-drawn per scenario, a change in the output couldn't be attributed to the input that was moved.
- Choice & why: `Scenario.seed` is fixed by default, so moving one knob changes the output *because of that knob*. A demo where the numbers wobble on their own is worthless.
- Tradeoff accepted: the same noise pattern recurs across scenarios; irrelevant for the purpose.

### Decision: default Scenario must reproduce the baseline dataset exactly
- Context: parameterising the generator risks silently drifting the company the whole portfolio narrative is built on.
- Choice & why: a test asserts the default Scenario reproduces the canonical dataset to within $0.005 on every numeric column. The refactor was verified against a pre-refactor checksum; the only delta was int-vs-float CSV rendering (`560000` vs `560000.0`), with a max numeric difference of 0.0 — so the guard is numeric equality, which is the property that actually matters.

### Decision: the projection method is a visible choice, not a hidden assumption
- Context: the forecast view extrapolates a continuation from actuals. "How did you forecast this?" is the first question an interviewer asks, and a single unlabeled trend line is not an answer.
- Options considered: (A) one method, chosen silently; (B) several methods, selectable, each labeled with what it assumes and ignores.
- Choice & why: (B). Three methods ship: linear trend (OLS on trailing months, with an R² so a weak fit is visible rather than implied), run-rate (trailing average held flat — makes no growth claim, which is its virtue), and driver-based (continues the ARR bridge at trailing average new/expansion/contraction/churn, then derives revenue). Code computes the number; the user picks the method; the model may narrate the assumption but never selects it.
- Tradeoff accepted: three code paths instead of one. Also honest: the driver-based method only exists where drivers exist (revenue), which is the real limit of the approach and is stated in the UI rather than hidden.

### Decision: the chat has access to the whole dataset, but is sent a deterministic slice
- Context: the chat should answer any question about the company ("YTD contractor spend?", "how did S&M trend?"), which argues for handing the model everything.
- Options considered: (A) selected month only — safest, but refuses obvious questions and looks broken; (B) send the whole dataset every turn; (C) precompute the whole dataset into a fact index, and deterministically select the relevant slice per question.
- Choice & why: (C). Access and delivery are different things. The index holds every computed number (all 24 months, every department and account, YTD, decompositions, retention — 6,528 facts); code selects the slice by the months, departments, accounts and metrics the question names. Measured: the sliced whitelist is ~228 dollar values vs ~3,293 for the whole set, and the false-verify rate for a fabricated 3-significant-figure figure is 10.7% sliced vs 66.8% whole (`eval/whitelist_scope.py`). Sending everything would gut the audit's meaning — "verified" would degrade from *this is the number we computed* to *this resembles some number we computed somewhere*.
- Tradeoff accepted: a question whose wording matches nothing gets a company-wide fallback slice, and an oddly-phrased question can under-retrieve. Selection is keyword-based and deterministic — code chooses, never the model, so the compute/explain boundary holds.
- Production note: at real scale this becomes retrieval over a governed semantic layer; the principle (code decides which facts exist) is unchanged.

### Decision: the chat refuses rather than falls back
- Context: for commentary, a rejected draft falls back to the deterministic narrative. There is no deterministic answer to an arbitrary question.
- Choice & why: if a chat answer fails the audit twice, it refuses. A wrong number is worse than no answer, and "it declines rather than estimate" is a feature to say out loud, not a limitation to hide.
- Tradeoff accepted: visible refusals in a demo. That is the correct behaviour and worth explaining.

### Decision: figures written below 3 significant figures are REJECTED, not verified
- Context: this was found by measuring rather than assuming, and it corrected a real weakness. The audit tolerates rounding via a precision-aware window ("$1.7M" matches 1,730,054.57). But the wider the window, the more likely a *fabricated* figure lands near some computed value by luck. Measured false-verify rates against the sliced whitelist: **92.9% at 1 significant figure ("$2M"), 50.6% at 2 ("$1.7M")**, 10.7% at 3, 0.3% at full precision. The prior adversarial eval planted fabrications only at full precision — the easy case — so it flattered the guardrail.
- Options considered: (A) leave it (the "zero fabrication" claim would be precision-dependent and quietly false for coarse figures); (B) tighten tolerance globally (breaks legitimate rounding); (C) reject any figure too coarse to verify, and require adequate precision in the prompt.
- Choice & why: (C). A figure whose rounding window swallows a large share of the computed range cannot be meaningfully checked, so blessing it would be a rubber stamp. `MAX_REL_TOL = 0.005` admits 3+ significant figures and rejects 1–2; the deterministic narrative and both model prompts now write at 3+ ("$2.60M", "$247.1K"); the adversarial eval now includes coarse-precision cases so the catch rate is honest.
- Tradeoff accepted: a legitimate "$2M" is rejected — the model must write "$2.04M". Slightly stiffer prose in exchange for a claim that survives scrutiny. Residual, stated plainly: at 3 significant figures a fabricated figure still has ~10.7% chance of coincidentally matching within the sliced whitelist. The next hardening step is label-anchored verification (check a figure only against values whose label matches the entity named beside it), which would collapse that residual; it is not built yet.
- Revert path: set `max_rel_tol=None` in `numeric_audit.audit` to restore the previous behaviour.

---

## Post-deployment review -- display integrity and sign handling

### Decision: negatives follow the accounting convention, ($793K)
- Context: the UI rendered operating income as "$-793K" -- the naive f"${x:,.0f}",
  which glues the minus between the currency symbol and the digits. It is the
  first thing visible on load and the most common formatting tell in a finance UI.
- Options considered: (A) leave it; (B) "-$793K"; (C) parentheses, "($793K)".
- Choice & why: (C), for dollars and percentages alike. Parentheses are what a
  P&L, a variance pack and every audited statement use, so a finance reader
  parses them without thinking. A house convention applied inconsistently is
  worse than either convention applied consistently.
- Tradeoff accepted: one branch in each formatter, plus a parens=False fallback
  for contexts where parentheses read badly. Negative zero is collapsed to "$0"
  so a rounding artifact never renders as "-$0".

### Decision: the audit parses signs itself; it does not inherit them from the UI
- Context: found while making the parentheses change. The audit's money regex
  accepted a minus only AFTER the "$", so "-$793K" -- the conventional form a
  model writes unprompted -- parsed as +793,000. Adopting parentheses in the UI
  would have widened the same hole: "($793K)" also parsed as positive.
- Why it matters: a sign error on a variance inverts favourable and
  unfavourable. Because the fact pack deliberately exposes most variances in
  both signs, a sign-flipped figure would have verified GREEN against the
  computed magnitude. That is a material error wearing a verified badge.
- Choice & why: the audit now folds a sign from either side of the "$" and
  treats a MATCHED pair of parentheses as negative, for dollars and percentages.
  A matched pair is required so ordinary prose -- "(revenue of $2.60M" -- is not
  misread.
- Design note worth saying out loud: the display formatter and the audit's
  parser are deliberately SEPARATE functions. That is why the UI could adopt a
  house convention without changing what the guardrail understands, and why the
  guardrail needed its own explicit fix rather than inheriting one. Presentation
  and validation drifting apart silently is the failure mode this avoids.
- Residual, stated plainly: this makes the audit READ signs correctly; it does
  not make it sign-strict. Most variances appear in the whitelist in both signs
  by design ("$113.3K unfavorable" writes the magnitude and lets the word carry
  direction), so flipping those is not fabrication by this whitelist's
  definition. Sign strictness in the general case needs label-anchored
  verification, already logged as the next hardening step. The new
  sign_flipped_dollar adversarial case targets a decomposition term that exists
  in one sign only, which is where a flip is detectable today.
- Regression guards: tests/test_formatting_and_signs.py pins both notations,
  parenthesised negatives, and the false-positive cases (years, bare ratios,
  ordinary parentheses). Suite 26 -> 61 tests.

### Decision: a goodness-of-fit is never rounded up to a perfect 1.00
- Context: the forecast page printed "Fit (R2): 1.00". The true value is 0.9996.
- Choice & why: three decimals, with anything in [0.999, 1) rendering as
  "0.999+". In a tool whose claim is that figures are precise and checked,
  rounding a fit UP to perfect is the wrong thing to be caught doing.
- Note: this is a display fix. Why R2 is 0.9996 is a data-model question,
  handled separately.

### Decision: chart height is derived from row count
- Context: the OI-impact chart plotted all 8 drivers but at a fixed 300px Vega
  thinned the overlapping category labels, so 8 bars carried 4 labels and one was
  clipped to "Services Revenue...". The chart appeared to disagree with the
  table directly above it.
- Choice & why: height = max(240, 34 x rows), labelOverlap=False,
  labelLimit=260. Every driver in the table gets a labelled bar.
- Tradeoff accepted: the chart grows if the driver count is raised above 8.

### Decision: bookings seasonality is a Scenario input, defaulted to zero
- Context: trailing-12 revenue fits a straight line at R2 = 0.9996. The cause is
  structural: revenue is derived as ending ARR / 12, and ARR is a STOCK. Noise on
  the FLOWS (~3% of ~$500K of new ARR) is diluted ~60:1 against a $26M base, so
  it moves revenue ~0.06%. High R2 on subscription revenue is realistic -- that
  smoothness is the point of the model -- but a fit that round invites the
  question of whether the data was generated on a line.
- Options considered: (A) raise flow noise -- would need ~60x, implying a 180%
  standard deviation on monthly bookings; fixes the chart by breaking the
  company. (B) recognition noise so GL revenue != ARR/12. Rejected: the revenue
  decomposition computes its own rev = ARR/12 and never reads the GL, so check #7
  would still pass -- but the decomposition would then explain a DIFFERENT
  revenue number than the P&L shows. "Does your driver decomposition tie to your
  P&L?" would become "no." Viable only with a third timing term and a twelfth
  check; deferred. (C) seasonality on the ARR flows.
- Choice & why: (C). Revenue stays exactly ARR/12, so the bridge closes, the
  volume/price decomposition ties, and all 11 checks pass with nothing downstream
  changed. Applied to budget, actual and forecast alike -- a planner plans for
  the Q4 push, so seasonality must not masquerade as variance.
- Measured at 0.25/0.30: R2 0.9996 -> 0.9690; MoM growth 0.99-1.28% ->
  0.29-1.75%; FY2025 miss unchanged at -5.3%; ending ARR $30.17M -> $30.23M;
  checks 11/11.
- Default & why: 0.0, reproducing the canonical dataset bit-for-bit. Turning it
  on is a data-model change requiring its own decision, not a side effect of a
  UI fix.

### Decision: FY2024 gets a small, opex-only, two-sided story -- off by default
- Context: FY2024 carried no story. Actual-vs-budget revenue variance averaged
  0.16% and every opex line tracked plan, so the first twelve rows of any
  variance view read "actual == budget, every month". A clean comparison year is
  defensible, but it spends twelve rows of an interviewer's attention proving the
  engine has nothing to find.
- Options considered: (A) leave it clean, answer verbally. (B) a revenue-side
  FY2024 miss -- rejected: revenue derives from the ARR bridge, so this means the
  company had a demand problem in 2024 AND 2025, which weakens the FY2025
  narrative rather than supporting it. (C) a small opex-only story.
- Choice & why: (C). Each constraint is load-bearing and pinned by a test.
  Opex-only: FY2024 revenue still tracks plan, which is what makes the FY2025
  bookings miss land. H2-weighted: H1-2024 stays a clean baseline. Two-sided:
  every FY2025 driver is unfavourable, so the favourable branch of
  oi_sign x variance > 0 otherwise goes undemonstrated on a real driver.
- The stories: R&D recruiting runs hot Jul-Dec 2024 as the company hires ahead of
  the FY2025 headcount ramp already in fact_headcount (R&D 38 -> 52, S&M 40 ->
  58) -- a variance explained by another table in the same dataset. S&M events
  land under plan Sep-Nov as the user conference is scaled back.
- Measured at 1.90/0.62: FY24 H1 worst monthly OI impact unchanged at ~$12K; H2
  ~$17K -> ~$55K; FY24 top driver RND_SAL $16.5K -> RND_RECRUIT $24.0K; FY25 top
  driver unchanged at $141K; checks 11/11.
- Implementation note: neither multiplier consumes a random draw and both apply
  before the existing noise draw, so the RNG stream is untouched.
- Default & why: 1.0/1.0, reproducing the canonical dataset bit-for-bit.

### Decision: the ROI claim is built under the same discipline as the commentary
- Context: INSTRUCTIONS.md sec.3 requires every tool to prove three things --
  numeric integrity, guardrails, and quantified ROI. The first two were airtight.
  The third did not exist anywhere in this repo: no ROI figure in the README, the
  decision log, the app, or any case study. Two of three pillars, with the missing
  one being the question a finance hiring manager asks first.
- Options considered: (A) write a headline number in the README. Rejected, and the
  reason is the whole point: an unsourced ROI figure would be the one fabricated
  number in a project whose headline is "zero fabricated numbers", and an
  interviewer who noticed would be right to discount everything else. (B) measure
  the manual baseline empirically -- not available, it would require timing real
  analysts on real close cycles at a real employer, which sec.3 rule 1 forbids.
  (C) split the claim into what is measured and what is assumed, explicitly.
- Choice & why: (C). The ROI model mirrors the tool's own architecture.
  MEASURED, instrumented at run time, not hand-entered: 16 figures computed and
  self-verified per commentary, 84 words, ~15ms, across 24 months, from 624
  computed variance rows. ASSUMED, the manual baseline: named, defaulted
  conservatively, each with a stated basis, exposed as sliders so a reader can
  move them. DERIVED: a sensitivity range, never a single number.
- The core term: tie-out cost = (figures cited, MEASURED) x (minutes per figure,
  ASSUMED). That is the honest shape -- the part that scales is measured, and the
  per-unit rate is on the table where it can be argued with. A test asserts that
  doubling the figure count doubles the tie-out saving.
- Measured result at base assumptions (75 min drafting, 1.5 min/figure tie-out,
  2 review cycles at 20 min, $85/hr loaded):
    conservative      67m -> 48m,  19m saved, 28% reduction,  4 hrs/yr
    base             139m -> 40m,  99m saved, 71% reduction, 20 hrs/yr
    heavier process  225m -> 45m, 180m saved, 80% reduction, 36 hrs/yr
- Tradeoff accepted: the headline is a range, not a number, and the conservative
  case is deliberately unimpressive. That is the cost of a claim that survives a
  hostile question. A model that wins big under every assumption is not a model,
  and a reader who can move the sliders and watch the answer shrink will trust
  the base case more than one who cannot.
- Explicitly NOT claimed, and stated in the app: not the analysis (deciding why a
  variance happened is judgment the tool does not do), not the close (getting to a
  trial balance is upstream), not headcount reduction (the saving is redeployed
  analyst hours -- a different and more believable claim), and not annualised from
  one good month (the unit is one commentary, scaled by a cadence the reader sets).
- Ownership note (sec.4): the baseline assumptions ARE problem framing, which is
  mine to own. The committed defaults are generic FP&A starting points drawn from
  no employer's process. They are placeholders to be replaced with figures I can
  defend from direct experience of what a monthly variance package takes.
- Regression guards: tests/test_roi.py (9 tests) pins the honesty properties
  rather than the arithmetic -- that the measured half is really instrumented,
  that every displayed assumption actually moves the answer (no decorative
  inputs), that the tool's own cost is never modelled as zero or the reduction as
  >95%, that the scenarios genuinely diverge, that a conservative reading stays
  under 50%, that the headline never states a bare number without its condition,
  and that the report names what is not claimed.

### Decision: seasonality and the FY2024 story are ON in the baseline
- Date: turned on after both were built, tested, and verified behind default-off
  flags. Flipping the defaults is a data-model change, so it was made as its own
  decision rather than as a side effect of a UI fix.
- What changed: bookings_seasonality 0.0 -> 0.25, churn_seasonality 0.0 -> 0.30,
  actual_fy24_recruiting_mult 1.0 -> 1.90, actual_fy24_events_mult 1.0 -> 0.62.
- Verified after regeneration: 11 validation checks 0 failed; 11/11 golden tests;
  72 unit tests; eval headline unchanged at 0 fabricated numbers across 5
  generations and 100% adversarial catch across 40 cases.
- Effect on the company: trailing-12 revenue R2 0.9996 -> 0.9690; MoM revenue
  growth 0.99-1.28% -> 0.29-1.75%; Sept-2025 revenue $2.60M -> $2.57M, operating
  income ($793K) -> ($821K), ending ARR $29.21M -> $28.81M, NRR 92.2% -> 92.1%,
  GRR 83.4% -> 83.2%; final ending ARR $30.17M -> $30.23M. The FY2025 story is
  unchanged: revenue still misses plan by -5.3%, and the largest single driver is
  still ~$141K.
- What this bought: FY2024 now carries a real, two-sided variance story instead of
  twelve rows of "actual == budget". November 2024 reads: Recruiting (RND) $23.9K
  unfavorable; Salaries (RND) $16.5K unfavorable; Events (SM) $14.2K FAVORABLE.
  That favorable driver is the first real number in the dataset that exercises the
  favorable branch of oi_sign x variance > 0 -- previously it was proven only by a
  validation check, never demonstrated on a live line item.
- Expected artifact, not a bug: FY2024 months before December show TTM NRR and GRR
  as n/a, because a trailing-twelve-month metric needs twelve months of history.
- Reversibility: the knobs still fully control the behaviour. Setting all four back
  to their neutral values reproduces the pre-seasonality company exactly, and two
  tests (test_zero_amplitude_recovers_the_flat_series,
  test_neutral_multipliers_recover_the_storyless_year) assert precisely that. The
  default-pinning tests were inverted rather than deleted, because the default is
  itself a decision and stays pinned in whichever direction it points.

### Decision: seasonality and the FY2024 story are ON in the baseline
- Context: both were built, tested and verified behind default-off flags. Flipping
  the defaults is a data-model change, so it was made as its own decision rather
  than as a side effect of a UI fix.
- What changed: bookings_seasonality 0.0 -> 0.25, churn_seasonality 0.0 -> 0.30,
  actual_fy24_recruiting_mult 1.0 -> 1.90, actual_fy24_events_mult 1.0 -> 0.62.
- Verified after regeneration: 11 validation checks, 0 failed; 11/11 golden tests;
  72 unit tests; eval headline unchanged at 0 fabricated numbers across 5
  generations and 100% adversarial catch across 40 cases.
- Effect: trailing-12 revenue R2 0.9996 -> 0.9690; MoM revenue growth 0.99-1.28%
  -> 0.29-1.75%; Sept-2025 revenue $2.60M -> $2.57M, operating income ($793K) ->
  ($821K), ending ARR $29.21M -> $28.81M, NRR 92.2% -> 92.1%, GRR 83.4% -> 83.2%;
  final ending ARR $30.17M -> $30.23M. The FY2025 story is unchanged: revenue
  still misses plan by -5.3% and the largest single driver is still ~$141K.
- What this bought: FY2024 now carries a real, two-sided variance story instead of
  twelve rows of "actual == budget". November 2024 reads: Recruiting (RND) $23.9K
  unfavorable; Salaries (RND) $16.5K unfavorable; Events (SM) $14.2K FAVORABLE.
  That favorable driver is the first real number in the dataset that exercises the
  favorable branch of oi_sign x variance > 0 -- previously proven only by a
  validation check, never demonstrated on a live line item.
- Expected artifact, not a bug: FY2024 months before December show TTM NRR and GRR
  as n/a, because a trailing-twelve-month metric needs twelve months of history.
- Reversibility: setting all four knobs back to neutral reproduces the
  pre-seasonality company exactly, asserted by
  test_zero_amplitude_recovers_the_flat_series and
  test_neutral_multipliers_recover_the_storyless_year. The default-pinning tests
  were inverted rather than deleted, because the default is itself a decision and
  stays pinned in whichever direction it points.

### Decision: scenario diffs compare floats with a tolerance
- Context: after the defaults were turned on, the sidebar read "1 input changed"
  on a fresh load. Streamlit computes a slider's reachable values as min + n*step,
  so 0.00 + 6*0.05 returns 0.30000000000000004 rather than 0.30. diff_from_default
  used an exact !=, so it reported a phantom change.
- Why it mattered more than it looked: that badge is the only indicator telling a
  viewer whether they are looking at the baseline or a modified scenario. A false
  positive on load undermines the one control that establishes trust in what is on
  screen -- and "what did you change?" is not a question to be unable to answer.
- Choice & why: compare floats with a 1e-9 tolerance, far tighter than any
  meaningful slider step, and leave non-float comparison exact.
- Tradeoff accepted: none material. Worth noting as a class of bug -- a display
  artifact of float accumulation that only appeared once the defaults stopped
  being whole-number neutral values.

### Decision: Materialize computed outputs as marts; the agent's SQL queries those, not the raw facts
- **Context:** The agent needed a SQL tool surface, but the copilot's computation layer is pandas — `run_pipeline.load()` pulls all ten tables with `SELECT *` and every variance, ranking, and decomposition happens in memory. DuckDB was a storage layer, not a computation layer.
- **Options considered:** (A) Wrap the pandas `outputs` dict in tool functions — zero divergence risk, but no SQL exists anywhere. (B) Write new SQL against `fact_actuals` / `fact_budget` / `dim_account` that recomputes variance. (C) Materialize the already-computed outputs as `out_*` marts and write SQL over those.
- **Choice & why:** C. Option B would have created a second implementation of the variance math — two sign conventions, two materiality floors, two pct-safety rules — which is exactly what `run_pipeline.py`'s docstring says this codebase does not have. C matches how production finance stacks are actually built: a transformation tier computes marts on a schedule and analysts query the marts. Nobody recomputes revenue recognition in an ad-hoc query. Here the pandas layer *is* the transformation tier and `out_variance_detail` is a mart. SQL may slice, filter, join, rank, and aggregate figures the canonical layer already computed; it may not recompute them.
- **Tradeoff accepted:** One additional build step, and a mart can go stale — if the pandas layer changes and materialization is not re-run, the agent serves old numbers with a clean audit trail attesting to them. Closed structurally: `build_hash()` fingerprints both the synthetic inputs and the source of every module that produces an output table, and `assert_fresh()` refuses to run against a stale mart. Refusal is the correct outcome, not an inconvenience.
- **Production note:** In a governed deployment the marts are dbt models on a schedule and the build hash is the dbt run manifest; `read_only=True` becomes a least-privilege database role rather than a connection flag. The security property is identical from the agent's side — there is no code path through which it can write.

---

### Decision: No financial quantity may ever be a tool parameter
- **Context:** The obvious guardrail is "the model doesn't compute." The non-obvious hole is the model reading `$1.24M` out of step 3's output and retyping it as step 4's argument — which is generating a number, inside a pipeline that otherwise never lets it.
- **Options considered:** (A) Instruct the model not to do this in the system prompt. (B) Validate at call time that no argument looks like a figure. (C) Make the failure unrepresentable by omitting any float/money parameter type from the registry.
- **Choice & why:** C. The permitted parameter types are exactly `PeriodParam`, `DimParam`, `EnumParam`, `IntParam`. No money type exists to be misused. Data flows between steps by *reference* — the Phase-2 orchestrator resolves `$STEP_3.rows[0].account_id` from the run ledger and binds it to a validated `DimParam` — so the model sees a symbol, never a figure it must transcribe. `test_no_tool_accepts_a_financial_parameter` asserts the property over the whole registry, so it survives future tools written by someone who has not read the docstring.
- **Tradeoff accepted:** Any threshold the agent might want ("show me variances over $50K") must be a config constant with a documented value, not a model choice. That is a real capability limit, accepted deliberately.

---

### Decision: EMPTY is a first-class outcome, distinct from error
- **Context:** A valid query returning zero rows and a malformed query are completely different events, and collapsing them is how agents quietly produce wrong packages.
- **Choice & why:** The taxonomy is `OK` / `EMPTY` / `INVALID_PARAM` / `TOOL_ERROR`. `EMPTY` is a *retrieved fact* — "no forecast exists for March 2024" — that the agent may narrate. The model view for `EMPTY` explicitly tells the model not to substitute a different query to obtain rows. `INVALID_PARAM` returns the real list of valid alternatives, so a nonexistent department is corrected against the dimension rather than guessed at again.
- **Tradeoff accepted:** More branches to test. `test_empty_is_distinguished_from_error` pins the behavior.

---

### Decision: Bounds are enforced, never clamped
- **Context:** `top_n=500` against a `1..10` parameter could be silently clamped to 10.
- **Choice & why:** Rejected with an error naming the range. Clamping would hide a planning error and let the agent believe it received what it asked for. Silent-coercion rate is an eval metric and its target is zero.
- **Tradeoff accepted:** One extra replan cycle when the planner overreaches — which is the visible, correct cost.

---

### Decision: Rank on operating-income impact, never raw variance
- **Context:** `var_ab_amount` is a raw difference. An expense line $200K over budget and a revenue line $200K over budget have identical raw variance and opposite business meaning.
- **Choice & why:** Every tool returns `oi_impact` (= `oi_sign × var_amount`) alongside the raw figure, and all ranking, decomposition, and share-of-parent logic uses `oi_impact`. This is the highest-risk defect in the build: a tool ranking on `abs(var_ab_amount)` would present an expense overrun as favorable, and *every downstream guardrail would agree*, because the figure itself is correct. The numeric audit verifies that prose matches computation — it cannot verify that the computation asked the right question.
- **Tradeoff accepted:** None. `test_ranking_uses_oi_impact_not_raw_variance` asserts both the ordering and that at least one over-budget expense line in the top 10 is correctly marked unfavorable.

---

### Decision: Tools return canonical entity names, not dimension ids
- **Context:** Found during the build. The entity audit whitelists `department_name` ("Sales & Marketing") via `canonical_entity_names()`, not `department_id` ("SM").
- **Choice & why:** Every tool joins its dimension table and returns the canonical name. A tool returning ids would hand the narrative layer tokens that the narrative layer's own guardrail rejects — the guardrail would fire correctly on data the pipeline itself produced.
- **Tradeoff accepted:** An extra join per tool, negligible at this scale.

---

### Decision: Comparison is a single enum, not two free scenario arguments
- **Context:** `get_pl_summary(period, scenario_a, scenario_b)` permits `budget_vs_forecast`, which the marts do not compute.
- **Choice & why:** A single `comparison` enum of `actual_vs_budget` / `actual_vs_forecast` makes the unsupported pair unrepresentable rather than a runtime failure the agent has to discover and recover from. The default resolves to `actual_vs_budget` and is written into `params_resolved`, so the ledger records what actually ran rather than what was typed.
- **Tradeoff accepted:** Adding a comparison later means an enum change plus a mart column, not just a new argument.

---

### Decision (amended): the mart fingerprint is content-based, not byte-based
- Context: `build_hash()` originally digested raw file bytes. Git checks CSVs out with CRLF on Windows and LF on Linux, so an identical logical dataset fingerprinted differently on a laptop versus Streamlit Community Cloud.
- Why it mattered: the hash is the mart freshness check. A cross-platform mismatch is indistinguishable from real staleness, so the check would have cried wolf on every deployment and been learned-ignored.
- Fix: line endings are normalized to LF before hashing. Nothing else is normalized -- real content and whitespace changes must still change the digest, because catching a changed computation layer is the entire point.
- Tradeoff accepted: the fingerprint no longer detects a pure line-ending change. That is the intent, not a gap.
- How it was found: comparing hashes across two machines (Windows CRLF -> 985fe1e9dfbc3467, Linux LF -> 63674a648a637aa4, identical data). It surfaces no other way. Post-fix both platforms produce 12355fb5db35bbea.
### Decision: Plan-then-execute with static validation, not free-form ReAct
- Context: The agent must sequence a multi-step goal. The default pattern is ReAct, which is maximally adaptive but produces no inspectable artifact before execution begins.
- Choice & why: Plan-then-execute with bounded replan. The complete plan is inspectable before anything runs, which is what a security review asks for and what makes evaluation tractable: plans can be scored on step recall, step precision, and dependency-order validity against a reference plan, not just outcomes. A plan rejected by static validation costs zero queries and zero tokens, measured with a counting connection rather than asserted.
- Tradeoff accepted: Less adaptive than ReAct on open-ended goals. Mitigated by symbolic references, which let a fixed plan skeleton bind to data it could not know at plan time.

### Decision: Data moves between steps by reference, never by transcription
- Context: Phase 1 made financial parameters unrepresentable by type. A behavioral hole remained: a model that reads a figure from step 3 and retypes it into step 4 has generated a number, even though every individual guardrail held.
- Choice & why: Symbolic references resolved by the orchestrator ($STEP_4.rows[0].member). The model emits a symbol; the orchestrator resolves it from the ledger. The ledger records params_declared and params_resolved separately, so the provenance of every argument is auditable: declared shows the reference, resolved shows CORP, and the pair proves the value came from a prior tool result. $GOAL fields are computed in code from the dimension tables, because date arithmetic is arithmetic and the model has no arithmetic capability.
- Tradeoff accepted: The reference grammar is deliberately narrow. No expressions, no arithmetic, no slicing. A model wanting something outside it must ask for a tool, not compose one.

### Decision: EMPTY is handled differently for required and optional steps
- Choice & why: Optional steps log a note and continue. Required steps refuse, with reason code REQUIRED_SECTION_UNAVAILABLE, and the run stops. Continuing and shipping a package with a silent hole is the worse failure, because the reader cannot tell what is missing.
- Tradeoff accepted: One unavailable section kills the package rather than degrading it. Deliberate: a variance package missing its P&L summary is not partial, it is misleading.

### Decision: Refusals carry reason codes, not free text
- Context: Refusal recall and false-refusal rate are both eval metrics.
- Choice & why: A fixed enum. Metrics over free-text reasons are not measurable, so the eval design constrained the implementation rather than the reverse.
- Tradeoff accepted: New refusal conditions require a new code rather than a new sentence, which is the intended friction.

### Decision: Budget exhaustion is a hard stop that marks the run incomplete
- Choice & why: Hard stop, BUDGET_EXHAUSTED, and the coverage check then reports every promised section not produced. Never a silent truncation.
- Tradeoff accepted: A run one step short of completing produces nothing usable. Correct: the alternative is a reader who cannot distinguish "no variance here" from "we ran out of budget before checking."

### Decision: Instrument tokens, cost, and latency before any model exists
- Choice & why: Phase 2 has no LLM, so every model-cost field is zero, but the fields are populated from the first line anyway. Retrofitted instrumentation measures whatever was convenient after the fact. Specifically: the agent ROI must be measured in different units from the copilot commentary-hours claim (sequencing, retrieval, assembly) or the two stories double-count and collapse under one question. That comparison needs the deterministic baseline numbers, which only exist if captured here.
- Production note: The same ledger fields are the SOX-relevant audit record.

### Decision: The deterministic baseline is a shipped artifact, not scaffolding
- Choice & why: It does three jobs permanently. Eval control condition (the Phase 3 planner is scored against this reference plan). Numeric ground truth (whatever the planner produces must match, since both route through the same tools). And the honest comparison: side by side with the agent it answers "is the agent earning its keep?" with measured tokens, latency, and cost rather than an opinion. For a fixed monthly close the pipeline is very likely the right answer; the agent earns its place when the goal space is open.
- Tradeoff accepted: More engineering than a throwaway harness. run_package.py exists as a CLI so the comparison can be run rather than described.

### Decision: Assert the absence of a model rather than assume it
- Choice & why: A test scans every import in src/agent and fails on any model client. Enforced, not documented, so it survives Phase 3 adding a planner.
- Tradeoff accepted: Phase 3 must place the model client deliberately. That is the point.

### Decision: Close the source -> base tables -> marts chain (false-green fix)
- Context: Found by testing rather than reasoning. Changing a synthetic CSV and re-running materialization produced an identical package: the data change never propagated.
- Root cause: run_pipeline.ensure_database() builds the DuckDB file only when it is absent, so once it exists the database is a cache that is never invalidated. load() reads that cache, while build_hash fingerprints the CSVs. Marts were computed from stale base tables and stamped with a hash derived from CSVs that were never read.
- Why it mattered: The freshness check reported current while certifying data it had not seen. A false green is strictly worse than the stale-mart problem the hash exists to prevent, because a check that lies is trusted.
- Choice & why: csv_fingerprint() is recorded in the database and checked at materialization; the base tables are rebuilt from the CSVs whenever the source moves. The CSVs are the committed source of truth and the .duckdb file is a gitignored build artifact; a cache that never invalidates is the bug.
- How it was found: Mutating one account actuals and asking whether the package changed. It surfaces no other way: every test in the suite passed while the defect was live.

## OPEN ITEM -- carry into Stage 2 (data-model regeneration)

run_pipeline.ensure_database() still has the un-invalidated cache described above. The fix currently lives inside src/agent/materialize.py, so the AGENT is safe but the COPILOT is not: regenerating the synthetic CSVs leaves the app serving the previous dataset until build_database.py is run manually.

Deliberately not fixed in place, to avoid reopening finished flagship code mid-build. Fold the csv_fingerprint check into run_pipeline.ensure_database() as part of Stage 2, when the data model is being regenerated anyway and the whole chain will be exercised end to end.

### Decision: The model produces a plan and stops; the orchestrator executes
- Context: The obvious implementation is the native tool-use API -- hand the model the tool schemas and let it loop, calling a tool, seeing the result, calling the next.
- Choice & why: Rejected. Native tool use puts the model in the driver's seat and the plan exists only in retrospect, as a transcript. Here the model emits ONE artifact, a plan, and stops. That inversion is the architecture: the complete plan is reviewable before any query runs, static validation is a hard barrier between model output and the database, a rejected plan costs zero queries and zero tokens, plans are scoreable against a reference, and replay is exact because execution is deterministic given a plan.
- Tradeoff accepted: One lost round of adaptivity per run -- the model cannot see step 3's result before committing to step 4. Symbolic references recover most of it; the bounded replan loop handles the rest.

### Decision: The planner test suite runs entirely offline
- Choice & why: Every planner test uses a scripted client. The barrier between model output and the database is a property of the VALIDATOR, not of the model, so a test that needed a good model to pass would not be testing the barrier. The adversarial fixture set (hallucinated tools, invented departments, monetary parameters, forward references, injected instructions, oversized plans, wrong types) is written as an adversary and each case was verified to fail with a specific message rather than passing vacuously.
- Tradeoff accepted: Live model behaviour is a measurement, reported from real runs, not a test assertion. The live client is covered up to the network hop with a stubbed SDK: request shape, JSON mode, text extraction, usage accounting, and reasoning-model parameter handling.

### Decision: Symbolic references are type-checked before execution
- Context: Found by the first live gpt-4.1 run. The model produced: rank_variance_drivers(dimension="statement_line") then decompose_variance(department_id=$STEP_2.rows[0].member). Ranked by statement_line, "member" is "Operating Expenses" -- not a department. The plan passed static validation and aborted at execution.
- Why it mattered: The plan was GUARANTEED to fail and the barrier waved it through. "Static validation is the barrier" is only worth saying if the barrier catches guaranteed failures.
- Choice & why: Every tool now declares the semantic type of each field it returns, with rank_variance_drivers typing "member" dynamically from its dimension argument. validate_plan checks that a reference produces the kind of value the target parameter accepts. Undeclared types degrade to the previous behaviour rather than breaking every plan that references them, and a test asserts every tool declares them.
- The larger consequence: MONEY is a type no parameter accepts, so a reference to a monetary field is now rejected STATICALLY -- including into top_n, the one integer slot a figure could plausibly have reached. Previously it resolved to a float and failed incidentally as "not a valid department" at execution time. This closes the reference-shaped version of the no-figures-as-parameters rule at plan time.
- Outcome: With the constraint also stated in the planner prompt, gpt-4.1 produced a corrected plan on the first attempt -- and did not merely dodge the error. It kept the statement_line ranking as its own analytical view and bound the decomposition to the department ranking. Type-correct, and it completes.

### Decision: Plan quality is reported by analysis, not by section label
- Context: The first scoring implementation compared section labels. A live plan produced "top_department_account_decomposition" for what the reference calls "top_driver_decomposition" -- same analysis, different name -- and was scored as missing it.
- Choice & why: Section labels are planner-chosen, so a label diff over-reports omission. `analysis_coverage` compares (tool, dimension) pairs, which identify what was actually computed independent of naming. The label metric is retained but explicitly demoted in both the docstring and the CLI output. `promises_kept` / `promises_unbacked` are unaffected, since those compare a plan against its own steps.
- Tradeoff accepted: Two coverage numbers rather than one. Worth it: quoting an inflated omission count in an interview would be a self-inflicted wound.
- Measured on the live run: analysis_coverage 0.4, step_precision 1.0, zero unbacked promises. The model chose five correct tools and omitted six -- a defensible but thinner package than the deterministic plan. Every tool it chose was right; it simply covered less ground.

### Decision: Provider is selectable, but provider-agnosticism is not claimed
- Context: The copilot claims a provider-agnostic narrative client, which is fair because a completion call is nearly identical everywhere. Planning is not.
- Choice & why: OpenAI and Anthropic clients both exist and auto-detect from whichever API key is set, defaulting to gpt-4.1. But prompt adherence, JSON discipline, and failure modes differ enough that a swap is a porting job, not a config change. The eval is run against one configuration, and provider plus model id are recorded in every result so a figure is always attributable to the setup that produced it.
- Implementation notes: response_format={"type":"json_object"} is used where supported, which removes a class of parse failure -- but the strict parser stays, because JSON mode guarantees well-formed JSON, not a well-formed PLAN. Reasoning models (o1/o3/o4/gpt-5 prefixes) get max_completion_tokens and no temperature, detected by prefix so a new release degrades rather than erroring.

### Decision: Unknown pricing reports "unpriced", never zero
- Context: The planner captures real token usage so the agent-vs-pipeline cost comparison is measured rather than estimated from character counts.
- Choice & why: A model with no configured list price reports pricing_known=False and a cost of None. A silent 0.00 would corrupt the exact comparison this module exists to make -- and a cost of zero is the most flattering possible number to fabricate. The price table carries an as-of date and points at the providers' pricing pages, because a cost figure in an interview is only as good as the rate behind it.
- Tradeoff accepted: The table needs occasional maintenance. Verify gpt-4.1 rates before quoting a cost.


### Decision: The run clock stops when the run stops
- Context: Found in the first live --compare run. The deterministic pipeline reported 9.705 seconds of wall clock for work that takes about 0.05.
- Root cause: `elapsed_s` was a live property computed on read. The baseline ledger was created first, then read AFTER the 9.5-second planning call, so it absorbed the planner's latency.
- Why it mattered: a 200x overstatement in the single number the pipeline-vs-agent comparison exists to produce, and it happened to flatter the agent. Measurement code that flatters the thing being measured is worse than no measurement.
- Choice & why: the orchestrator calls `ledger.finish()` at run end and `elapsed_s` freezes there. A regression test sleeps and asserts the figure does not move.

### Decision: Planning cost is recorded in the run ledger
- Context: The agent's ledger reported "tokens 0, cost $0.0" for a run that had just called a model. The cost existed only in the comparison table printed beside it.
- Choice & why: planning is an action with a cost and belongs in the record of the run that incurred it. `record_planning()` captures tokens, cost, latency, model id, and attempt count; `cost_summary()` folds them in. A ledger reporting zero tokens for a model-backed run is lying by omission, and the ledger is the artifact a reviewer is pointed at.
- Unpriced models propagate `pricing_known: False` rather than a zero, consistent with the planner's own rule.

### Decision: Package sections are rendered by tool, not by label
- Context: The renderer keyed off section names from the hand-written plan ("operating_headline", "arr_bridge"). A live agent plan named its sections differently, so four of five sections were silently dropped and the package printed only the P&L. It looked empty although every step had succeeded.
- Options considered: (A) constrain planners to a fixed section vocabulary -- preserves the curated layout but restricts what a planner may produce; (B) a separate generic renderer for agent runs -- two code paths that will drift; (C) key rendering on the tool that produced the section.
- Choice & why: C. Presentation is a display concern and must not dictate what the planner is allowed to name or produce. A tool-keyed formatter registry means any plan renders, section order follows the plan's own sequence, repeated tools are disambiguated by their varying argument (ranking by department vs statement_line), and a tool added later is displayable the moment it is registered -- via its own formatter if one exists, or the generic table if not. A formatter that raises falls back to the generic table rather than dropping a section that succeeded.
- Tradeoff accepted: headings are derived rather than authored, so the planner cannot title its own sections. Worth it: the alternative silently discards correct output.

### Decision: The pipeline-vs-agent comparison matches by analysis, not by label
- Context: The comparison reported "1 section in common" for a run that agreed on four analyses, because it matched on planner-chosen section labels.
- Choice & why: comparison keys on (tool, dimension, department, metric) -- what was actually computed, independent of naming. Label matching counts a naming coincidence as agreement and identical figures as divergence, in both directions. The output now also lists what each side covered that the other did not, which is the substantive difference between a deterministic plan and an agent-authored one.
- Consequence worth stating plainly: both routes go through the same tools, so identical figures on shared analyses are the expected result. A divergence there would be an ENGINE BUG, not a difference of planning opinion, and the output says so.
- Measured on the live plan: 4 analyses in common, 4 identical, pipeline 0.026s and $0 vs agent 9.5s and $0.0071, pipeline covering 7 analyses the agent omitted and the agent adding 1 the pipeline did not.


### Decision (amended): the pipeline-vs-agent comparison keys on the FULL resolved parameter set
- Context: The first analysis-keyed implementation used (tool, dimension, department, metric) and omitted top_n. A live run where the agent asked for top_n=3 and the pipeline for top_n=5 was reported as DIVERGED -- on the one check whose message asserts that a divergence is an engine bug.
- Why it mattered: a false alarm on a signal that must never cry wolf. Same data, different query: the agent's three rows were the pipeline's first three, and the hashes differed only because the row counts did. A check that reports an engine bug when there is none trains you to ignore it, which is worse than not having it.
- Choice & why: the key is now (tool, every resolved argument). Divergence then means exactly what the message says -- identical query, different answer. Queries differing only in their arguments are reported in their own category ("same analysis, different arguments: rank_variance_drivers(department) [top_n: 5 vs 3]"), because that is useful information about how deep each side looked, not a discrepancy.
- Tradeoff accepted: "identical queries" is a stricter and therefore smaller number than the old "analyses in common". It is the honest one.


### Decision: Narration and auditing are orchestrator stages, not tools
- Context: The obvious design exposes `audit_narrative` as a tool and checks that the agent called it.
- Choice & why: Rejected. That makes the guardrail depend on the agent's cooperation and turns "did it audit?" into a runtime question. Narration and auditing are not in the registry, so no plan can include them, omit them, or reorder them. The agent plans retrieval; the orchestrator narrates, audits, and gates. **The agent cannot skip what it has no ability to invoke.** A test asserts no such verb exists in the registry and that a plan attempting one is rejected before execution.
- Related: there is no `publish` verb either. Publication is a human action taken outside the agent (Gate 1). What the narrative stage produces is a *candidate* carrying a verdict.

### Decision: `publishable` is False unless an audit RAN and passed
- Choice & why: A missing audit and a failed audit are deliberately the same answer. Any other choice makes forgetting to check equivalent to passing. `assert_publishable()` raises rather than returning a flag, so a caller cannot ignore the verdict by not looking at it.
- Tradeoff accepted: callers must handle an exception rather than read a boolean. That is the intent.

### Decision: The narrative fact pack is built from the ledger, not recomputed
- Context: `narrative.fact_pack.build_fact_pack` recomputes top drivers, comp decomposition, and revenue decomposition from pandas. It is correct, and the copilot uses it -- but wiring it into the agent would create a SECOND path to every number.
- Why that matters: two paths can drift. When they do, the audit passes -- because the prose matches the fact pack -- while the prose contradicts the package printed beside it. That is worse than an unaudited narrative, because it ships with a certificate.
- Choice & why: `agent/facts.py` builds the whitelist from `ledger.results`. Every allowed value traces to a specific step, recorded in `ledger_provenance`. The audit's whitelist and the package's figures are the same numbers by construction rather than by agreement. `_add` is imported from the copilot's fact_pack rather than reimplemented, so the sign/magnitude handling stays in one place -- reimplementing it would be a second path to the *whitelist*, the same mistake one level down.
- Emergent property worth stating in an interview: **the plan determines the narrative's permitted vocabulary.** If a plan never calls `get_arr_bridge`, no ARR figure enters the whitelist, so the model cannot state one. A thin plan yields a thin, correct commentary rather than a rich, partly invented one. A test proves this with the REAL ARR figure: correct, never retrieved, and rejected -- because provenance, not correctness, is what the audit can verify.

### Decision: Ledger rows are mapped into the copilot's canonical fact schema
- Context: The prompt and the deterministic injection narrative read a fixed schema (company, revenue_vs_budget, top_drivers, comp_decomposition, arr, headcount). Emitting facts under tool names produced an empty injection narrative.
- Choice & why: Map into the canonical schema so the agent reuses the flagship's prompt and fallback unchanged rather than growing a parallel copy that would drift. Anything the schema has no slot for still reaches the model under `additional_analysis` -- and still enters the whitelist. Whitelist coverage follows what was retrieved, never what the schema happens to name; otherwise a figure the model legitimately saw would be rejected as fabricated.

### Decision: Driver grains are kept apart
- Context: The first fact pack ranked department rollups and their own accounts in one list, producing "Corporate / Company $76.1K unfavorable; Subscription Revenue (CORP) $102.2K unfavorable" -- a parent beside its child, reading as two findings when it is one finding at two grains.
- Choice & why: `top_drivers` carries the rollup grain; `driver_detail_by_account` carries decompositions. A finance reader would catch the original immediately, and an interviewer reading the sample commentary is a finance reader.

### Decision (flagship fix): the magnitude suffix requires a trailing word boundary
- Context: Found while wiring Phase 4. A fully grounded model draft was rejected. The cause was in the FLAGSHIP audit, not the agent: `(?P<suffix>...|[kKmMbB])?` had no trailing boundary, so the "b" of "below", the "m" of "mainly", and the "k" of "killed" were consumed as magnitude suffixes. "$109,338 below plan" parsed as $109 trillion and was then rejected as fabricated.
- Blast radius: ordinary FP&A prose. "$X below budget", "$X base salary", "$1,200 monthly", "$43,112 Marketing overspend" -- where Marketing is a department name. Correct model output was being pushed into the deterministic fallback.
- Why it mattered: the audit was not permissive, it was NOISY, which is the failure mode that quietly makes a guardrail useless -- it never lets anything wrong through, and it never lets anything through either. The measured adversarial catch rate was unaffected (fabrications were still caught), so no existing eval surfaced it.
- Fix: a `(?![A-Za-z])` lookahead after the suffix group. The regex backtracks the optional suffix and the optional space, so real suffixes ("$2.5M budget", "$5 billion market", "$1.2bn") and both negative forms (`-$793K`, `($793K)`) still parse. 17 parametrized regression cases, and the flagship eval still reports 100% adversarial catch across 40 cases.
- How it was found: writing a realistic grounded draft and asking why it was rejected. No adversarial test would have surfaced it, because it is a false NEGATIVE on valid input and every eval measured false positives on invalid input.

### Note: what the audit does and does not guarantee
Guaranteed: every dollar, percent, and count in accepted prose matches a value the agent's tools returned, within MAX_REL_TOL = 0.005. Fabricated figures cannot survive.

Not guaranteed: that the CLAIMS are right. "Opex rose because the team hired ahead of plan" can be causally invented while every figure in it is correct. The audit verifies numbers, not reasoning. That residual is bounded by the human gate in Phase 5, and it is stated plainly rather than papered over -- a design claiming to have automated judgment would be overclaiming, and overclaiming is what an interviewer catches.


### Decision: anything the whitelist permits must be visible in the prompt
- Context: Found in the first live Phase-4 run. The package printed "ending ARR $28,809,278" while the commentary in the same output said "No retention or ARR figures were provided for this period."
- Root cause: the canonical-schema mapping hand-picked six fields from `get_operating_metrics` and silently dropped `ending_arr`, `cogs`, `gross_profit`, `opex_pct_revenue`, `arr_per_head`, and `revenue_per_head`. Those stayed in the audit whitelist -- so the audit would have accepted them -- but the model could not see them. The model's statement was correct from its own view and a flat contradiction on the page.
- Why it mattered: a reader comparing the package to the commentary sees the tool disagreeing with itself. Nothing was fabricated and no guardrail failed, which is precisely why it would have survived to an interview.
- Choice & why: canonical entries are now built from the full whitelisted numeric payload, with schema aliases layered on top rather than replacing it. The invariant is asserted: every whitelisted value must appear somewhere in `prompt_facts`. The single permitted exception is the "(magnitude)" entries -- absolute values derived for negatives so the model may write "$109,338 below plan" instead of "-$109,338" -- and a test pins that this is the *only* gap, so a future omission cannot hide behind it.
- Related fix: `facts["arr"]` now always carries the schema keys even when a value is None. TTM retention is undefined for the first twelve periods, and dropping a None-valued field removed the key entirely, raising KeyError in the injection narrative for every 2024 period.


### Decision: withhold a figure whose sign cannot be read at its grain
- Context: Found in a live Phase-4 commentary. The model wrote "Operating Expenses exceeded budget by $142,030.94... the largest unfavorable variances were in Corporate / Company ($76,065.38 over budget), Sales & Marketing..., Research & Development...". CORP holds Revenue and Cost of Revenue and NO opex at all, and its raw variance is -76,065 (revenue *under* budget), not +76,065 over.
- Why the audit did not catch it: it was not a numeric error. $76,065.38 is a real computed magnitude, so it verified. The *claim* was wrong -- wrong sign, wrong category. This is the documented residual (the audit verifies numbers, not reasoning) demonstrated on a real run.
- Root cause, which IS fixable: a department rollup can span revenue and expense lines whose "bad" directions have opposite signs, so its raw variance has no readable sign. Exposing such a number is an invitation to misread it.
- Choice & why: at department-rollup grain the raw variance is no longer whitelisted, and the `variance` key is re-pointed at `oi_impact` -- same magnitude, unambiguous sign, and both keys carry the SAME number so there is no wrong one to pick. Every driver also gains an explicit `direction` word ("favorable"/"unfavorable"); the boolean `favorable` had been available and was ignored, and a word beside a signed number is harder to misread than a flag. The fact pack now carries a `how_to_read_drivers` note stating that oi_impact is authoritative and that a department rollup is not necessarily an opex item.
- Scope: this was introduced by including department rollups in `top_drivers`. The flagship's `build_fact_pack` is account-grain, where the statement line is implied by the account and the raw sign is readable, so the copilot was never affected.
- Side benefit: it also corrected the deterministic narrative, which had been pairing the raw variance magnitude ($76.1K) with a direction word derived from OI impact -- right direction, wrong magnitude. It now reads $142.6K unfavorable.
- What this does NOT fix: causal claims. "Opex rose because the team hired ahead of plan" can still be invented with every figure correct. That residual is bounded by the human gate, not by the audit, and saying so plainly is more defensible than implying otherwise.


### Decision: the audit trace shows only the parentheses a figure owns
- Context: A live audit trace rendered "($91,995.40" for the sentence "Sales & Marketing ($91,995.40 over budget)". The money pattern captures an optional "(" before the figure and an optional ")" after it, because a matched pair is the accounting negative; when only one side is present the paren belongs to the surrounding sentence.
- Scope: display only. The VALUE was already correct in every case, because the negative branch requires both sides -- "($793K)" is -793,000 and "(revenue of $2.60M)" is +2,600,000, both before and after this change.
- Why it was worth fixing anyway: the audit trace is the signature UI element carried over from the copilot, and it is the thing an interviewer looks at. A dangling parenthesis reads as a broken accounting negative, which invites a question about whether the sign handling is sound -- on the one component whose entire job is to be trustworthy.
- Tests pin that genuine accounting negatives keep their parentheses, so the hygiene fix cannot quietly become a correctness regression.


### Note: an app test must control the environment it asserts about
- Context: `test_live_mode_without_a_key_informs_rather_than_crashes` passed in CI and failed on the development machine. The app detects a key from `os.environ`, and the shell had `OPENAI_API_KEY` exported from earlier live runs -- so the page correctly showed the run controls instead of the key prompt, and the test's assumption of an empty environment was wrong.
- Why it matters beyond the one test: a test that passes only on a machine with no credential configured is testing the machine, not the app. The exported-key case is the COMMON one for anyone who has used live mode, so the test was green in exactly the environment nobody develops in.
- Fix: the test builds its own app instance with the key environment set deliberately, and a complementary test asserts the with-key branch. The pair pins the actual behaviour instead of whatever the developer's shell happens to contain.


### Decision: every text-mode file open declares its encoding
- Context: `test_the_generator_contains_no_arithmetic_on_figures` passed in development and raised `UnicodeDecodeError: 'charmap' codec can't decode byte 0x90` on the Windows machine. `open(path)` in text mode uses the LOCALE encoding -- UTF-8 on Linux, cp1252 on a default Windows install -- so reading a source file containing an em dash works in one place and crashes in the other.
- The worse finding, which the crash led to: `run_agent.py` and `run_package.py` contain "�" (UTF-8 C2 B7), and cp1252 decodes those bytes without complaint as "·". So the model-boundary scan in test_agent_phase2 had been reading MOJIBAKE on Windows and passing. Not a crash -- a wrong read that still returns a string, on a test whose entire job is to inspect source text. The same silent-wrong-answer shape as the original build-hash defect.
- Also found by the same sweep: `test_formatting_and_signs.py` reads `app.py` (286 non-ASCII bytes) without an encoding and `exec`s a slice of it. It passes only because the mojibake happens to fall outside the sliced region.
- Choice & why: all thirteen text opens declare `encoding="utf-8"`, and a test walks the source with **AST** -- not a regex, which matched the word "open(" inside its own docstring -- failing on any text-mode open that omits an encoding. Binary opens are exempt because they need none.
- Related hardening: the CLIs reconfigure stdout/stderr to UTF-8 with `errors="replace"` at entry, so a console that cannot render a middle dot degrades to a placeholder instead of killing a completed run with UnicodeEncodeError. The subprocess test pins `encoding="utf-8"` rather than inheriting the locale.
- Verified by running the whole suite under `LC_ALL=C` with UTF-8 mode disabled, which reproduces the failure class without a Windows machine.


### Decision: the agent page follows the sidebar reporting month
- Context: every other page in the app responded to the sidebar month; the agent page did not. It was pinned to whichever runs happened to be cached, so changing the month changed the entire app EXCEPT the agent -- which reads as the agent being bolted on rather than part of the tool.
- Choice & why: the deterministic agent runs on demand for the selected month. Eleven tool calls take about 40ms and no credential, so there was never a reason for it not to follow. Cached replay stops being the only way to see anything and becomes what it is actually for -- demonstrating that a committed artifact re-verifies its hashes on load.
- Three named modes: **Run now** (deterministic, follows the sidebar month), **Verified replay** (committed runs, integrity re-checked, defaulting to a saved run for the selected month when one exists), and **Plan with a model** (LLM planner, period defaulting to the sidebar month).
- Gate 1 state is keyed on the month, so switching months cannot carry an approval onto a different period's package -- the same artifact-binding rule as the approval hash, applied to the UI.
- The run is cached on (period, mart build hash), so the cache invalidates when the data underneath moves rather than serving a stale package under a fresh label.
- Also corrected: the deck download is withheld until Gate 1 approval in this mode too, and the deck filename carries the selected month, so an approval can never attach to a different period's deck.

### Note: a delivery gap, and a self-inflicted one
The month-following behaviour was built but never shipped -- the `app.py` packaged in the Phase 6 script was an earlier revision, so the improvement existed locally and not on the machine running it. Worth recording because it is a failure mode no test catches: the tests passed against a working copy that was not what got delivered.

Compounding it, recovering from that state included a `git checkout` on a file with uncommitted work, which destroyed the implementation. The tests survived, and because they were written as a specification rather than as assertions about the code, they were sufficient to rebuild it exactly. That is the strongest argument for spec-shaped tests in this whole project: they were the only surviving record of intended behaviour.


### Decision: a figure must START at a word boundary
- Context: found by verifying three real decks against the canonical computation. One deck's approval hash was `d9434ff7908b617d`, which contains "7908b". The trailing-boundary rule passed it -- the "b" is followed by a digit, not a letter -- so it parsed as $7.908 TRILLION and showed up as a figure with no computed source.
- Why it is worse than it looks: it is DATA-DEPENDENT. It fires only when a random 16-hex digest happens to contain a digit run followed by k/m/b. Two of the three decks were clean and the third was not, so the deck traceability test passed on the committed demo runs and would have failed intermittently on real ones. An intermittent failure in a verification layer is the kind that gets dismissed as a fluke.
- Choice & why: a leading `(?<![A-Za-z0-9])` boundary, so a figure must begin at one. Digests, run ids, and "FY2025" now yield nothing, while every legitimate form still parses -- accounting negatives in both notations, magnitude suffixes, percentages. The flagship eval still reports 100% adversarial catch across 40 cases, so the fix removes phantoms without weakening detection.
- This is the third boundary defect in the same regex (trailing suffix, unbalanced parens, and now leading). The pattern is that a number-extractor is mostly a specification of what is NOT a number, and the negative space is where the defects live.

### Note: verifying the decks against the data, not against the generator
Three decks were checked by recomputing ground truth from `run_pipeline.compute(load())` -- deliberately not through the marts, the agent, or the deck module's own provenance list, since checking a module against its own bookkeeping proves only self-consistency.

Three questions, in order of severity: does every figure exist in its own month's computation; do the headline figures match exactly; and could the deck belong to a DIFFERENT month. That last one is the failure that would survive every internal check -- a deck that is internally consistent and for the wrong period.

Result: 100% of figures matched (72, 72, and 76 text figures; 37 charted values each), headline figures exact, and each deck best-matched its own month by a wide margin (69/69 vs 8 for the next best, 68/68 vs 9, 74/74 vs 13). No cross-month contamination.

The first run of the checker reported the decks' correct driver shares as unmatched, because the shares are derived in SQL and absent from the pandas outputs used as ground truth. A gap in the CHECKER, not the deck -- and a reminder that a verification script needs the same scrutiny as the thing it verifies. `verify_decks.py` is committed so the check is repeatable rather than a one-off.


### Note: a false green in my own verification harness
The script that shipped the hex-digest fix printed "DECKS VERIFIED - each matches its own month" immediately after `verify_decks.py` had crashed with a traceback.

Two causes, both mine. `verify_decks.py` had a sandbox path hardcoded, so it could not run on the target machine at all. And the PowerShell wrapper never checked `$LASTEXITCODE` after that step -- `$ErrorActionPreference = "Stop"` traps cmdlet errors but NOT a non-zero exit from a native executable, so the script sailed past a failed verification and printed the success banner.

That is the same defect class this project keeps finding, now in the harness that reports on the other checks: a verifier that fails green. It is worth recording rather than quietly fixing, because "how do you know your guardrails work?" has a better answer when the honest one includes the times they did not.

Fixes: `verify_decks.py` discovers decks in the repo root, infers each period from the filename with a title-slide fallback, and returns 0 / 1 / 2 so it is usable as a build gate. Every step in the wrapper now tests its own exit code, and the success banner is unreachable unless verification returned 0. The checker itself was proved by running it against a deck deliberately labelled for the wrong month -- it reported 3 of 69 figures matching and flagged cross-month contamination -- because a check that has never failed is not known to work.


### Decision: the agent shows where to look; it does not recommend what to do
- Context: the live model path produced a thinner copy of the free deterministic run -- slower, costlier, less complete. The obvious fix is to have the model recommend actions ("cut paid marketing 15% next quarter"), which is what most agent demos do.
- Why that was rejected: a recommendation is not a retrieved number. Nothing can trace it, the audit has nothing to check it against, and the publication gate would pass it through untouched -- every guardrail here catches a wrong FIGURE. We had already watched the milder version fail live: the model wrote "Operating Expenses ... Corporate / Company ($76,065.38 over budget)" when CORP holds no opex and the sign was inverted. Every magnitude was real, so the audit passed it. Prescriptive advice is that failure mode promoted to a feature.
- The second reason is what it costs in the room. "What happens when it recommends something wrong?" currently has a strong answer -- the model never asserts anything unverifiable. With recommendations the answer becomes "a human reviews it", which is what everyone says.
- Choice & why: `briefing.py` answers a different question in code -- where did the money move, how much, and what is underneath it. Ranking, the materiality cut (Pareto to 80% coverage, capped at four, 3% floor), and which detail attaches to which driver are all computed. Every displayed figure is a ledger value carried by reference. The cumulative share that decides the cut is used for SELECTION only and never displayed, because publishing it would mean publishing a figure this module derived.
- The framing that makes it strong rather than a limitation: this is an automated, trustworthy, very fast way to put an analyst in front of the exact evidence a decision needs. The judgment stays with the analyst; the tool removes the hours of assembly. Tests assert the module states no imperative and performs no arithmetic on a figure.

### Decision: supporting groups carry their basis
- Context: the first briefing printed "Salaries ($67,061)" -- operating-income basis, negative is unfavourable -- directly beside "salary variance $67,061" -- expense basis, positive means spent above plan. Same department, same month, opposite signs, no explanation.
- Choice & why: account detail, compensation, headcount, and the revenue split are kept as separate labelled groups rather than flattened into one list, each stating its basis. This is the same class of confusion that produced the CORP-as-opex error earlier, caught this time before it reached a reader.

### Decision: the live path demonstrates goal generality, not the same package again
- Context: the goal box was prefilled with "prepare the variance package for this period", which is exactly what the free deterministic run already does -- so the model path looked redundant and expensive.
- Choice & why: the page now offers example questions that show range ("why did operating expenses miss plan?", "is the revenue miss a volume problem or a price problem?"), because answering a question nobody anticipated is the capability an agent actually adds. The header says plainly that the deterministic run answers one fixed question and this answers a new one, so a visitor understands when each is the right tool.

### Decision: Gate 0 is off by default, and the plan table reads in execution order
- Context: Gate 0 defaulted on, so a plan-approval table was the first thing a visitor saw -- before any result existed to give it meaning. Worse, the grid sorted by section name, so a five-step plan rendered 3, 2, 4, 1, 5.
- Choice & why: sequence is the entire content of a plan; a plan you cannot read in order is not a plan. The table now sorts on step and shows full arguments. Gate 0 moved into a Run controls expander, defaulted off, and is framed for what it is -- inspectability on a read-only surface, and a seam that a write-capable registry would require.

### Decision: every output section is titled and explained
- Context: someone landing on the page cold had to infer what each block was.
- Choice & why: each section carries a heading and one line on what it is and why it matters -- what the model was asked and what it did, where to look, the full performance overview, the commentary and how it was verified, and what Gate 1 is for. The explanations state the guarantees in the place a reader encounters them rather than leaving them to a README nobody opens.


### Decision: the tool surface was the reason plans looked canned, not the prompt
- Context: every question given to the planner produced roughly the same four or five calls, so "Plan with a model" read as a slower, costlier copy of the fixed monthly close.
- Diagnosis: all eleven original tools answered variations of ONE question -- what happened this month against plan. A planner cannot produce a distinctive plan for "is this a one-off or a pattern?" when nothing in the registry can look across months. No amount of prompting fixes a surface that cannot express the question.
- Choice & why: six comparative and trend tools, each mapped to a question an analyst actually asks and each reading a mart the deterministic package never touched: `compare_periods`, `get_ytd_summary`, `rank_persistent_drivers`, `get_account_trend`, `get_opex_ratio_trend`, `rank_mom_movers`. Seventeen tools total.
- The most valuable is `rank_persistent_drivers`, which ranks by how many of the last N months a line was unfavourable rather than by this month's size: "Subscription Revenue missed in 6 of 6 months, cumulative -$437,617" is a different finding from "this month's biggest driver", and separating a pattern from a one-off is exactly the judgment a monthly package cannot make for you.
- Held to the same standard as the originals: parity against the canonical pandas layer, operating-income impact rather than raw variance, bounded windows, declared output types for reference type-checking.

### Decision: refusal is a planner outcome, not a planner failure
- Context: a narrow tool surface means some questions cannot be answered. The behaviour that must not happen is answering a DIFFERENT question the tools do support and presenting it as though it were the one asked -- the plausible-and-wrong failure this architecture exists to prevent, one level up from a fabricated number.
- Choice & why: the planner may return `{"refusal": "..."}`, which raises `PlannerRefusal` -- deliberately distinct from `PlannerError`, because failing to plan is a defect while declining an out-of-scope question is correct. A refusal with steps attached is NOT honoured as a refusal, so a model cannot decline in prose while still querying.
- One of the example questions ("what is our cash runway?") is deliberately unanswerable, so the behaviour is demonstrable rather than described.

### Decision: follow-ups plan the delta, with summaries and never rows
- Context: a one-shot planner is a form that takes a question. An agent is something you can follow up with.
- Choice & why: prior runs in the session are passed to the planner as a compact digest -- the question asked, the tools called, and a few row labels. Never the rows: replaying full results would grow the prompt without bound across a conversation and would invite the model to quote a figure from context rather than retrieve it, which is precisely the transcription failure symbolic references were built to prevent. The prompt states explicitly that `$STEP_n` cannot reach an earlier run's ledger.
- Every follow-up still produces a plan that static validation gates before a single query runs, so the conversation is bounded exactly like a first question.

### Decision: two modes, and the plan-review gate is not one of them
- Context: three modes, two of which rendered an identical package. And the plan-review table was the first thing a visitor saw -- internal vocabulary, presented as a control they were expected to operate.
- Choice & why: the modes are now "Standard monthly close" and "Ask a question", which are obviously different things. The saved-run viewer was deleted rather than left as dead UI code that would invite itself back; the replay MACHINERY is untouched, because the hash verification and tamper tests are what the "these numbers are real" claim rests on -- it simply does not need a tab. The plan-review gate stays in the orchestrator and the decision log; the plan is still shown after a run, as evidence rather than as a gate.

### Decision: the sign-off is written for a reader, not for the codebase
- Context: "Gate 1 � pre-publication approval" tells someone outside the project nothing about what they are being asked to do.
- Choice & why: the instruction is now the heading -- "Sign off before this is used" -- and the body states the actual division of labour: the figures are already verified against the data, and what a human confirms is that the READING is right, because a conclusion can be wrong while every number in it is correct. Section headings and body copy moved from muted grey to near-black, because this is the copy a first-time reader depends on and grey at that size is hard to read on a projector.


### Decision: a tool's output fields must be registered everywhere downstream, and a test enforces it
- Context: "how does this month compare with last month?" ran perfectly -- the plan validated, `compare_periods` returned five rows -- and then produced "insufficient data for variance commentary" with 0 figures verified.
- Root cause: six comparative tools were added and NONE of their output fields were registered in `facts._FIELD_KINDS`. The tools ran, returned rows, and contributed nothing to the audit whitelist, so the model could see the data and was not permitted to state any of it. A field the model can see but cannot state is worse than one it cannot see at all: the run looks successful and the output is empty.
- The systemic problem: adding a tool required touching the whitelist, the canonical fact mapping, the briefing, the app renderer, and the deck -- five places, none of which failed loudly when missed.
- Choice & why: a parametrised test now runs EVERY registered tool, reads back every numeric field it returns, and fails on any that `_FIELD_KINDS` does not know about or that is not explicitly listed as non-narratable. A second test asserts every tool contributes to a usable fact pack, and a third asserts the tool list in the test matches the registry -- so adding a tool forces confirmation that the layers below it handle it.
- It found four more gaps immediately, in the ORIGINAL tools: `get_account_trend` returns `budget` (the rest of the codebase calls the same concept `base`), so a trend could be narrated with its actuals but not its plan; and the `bridge_diff` / `decomp_residual` remainders were unstatable, though "the split ties exactly" is a real claim worth being able to make.

### Decision: any ranking tool can seed the briefing, and the basis is stated
- Context: the briefing only recognised `rank_variance_drivers`, so a persistence or month-over-month question ran successfully and then reported "nothing to prioritise" -- refusing to summarise a run that had produced exactly the ranking it needed.
- Choice & why: four ranking tools can seed it, each declaring which field carries the impact, because `compare_periods` ranks on the change while persistence ranks on the cumulative total. The briefing now carries a `basis` string and states it -- "ranked by impact of the change since last month" versus "ranked by impact on operating income versus plan" -- because those answer different questions and a reader must not have to infer which one they are seeing.

### Decision: direction follows the impact sign, not a flag that may not exist
- Context: `favorable` is returned by the versus-plan tools but not by the comparative ones, so reading it directly labelled every month-over-month row "unfavorable" -- including Subscription Revenue, which had RISEN $25,633.
- Choice & why: direction is derived from the sign of whichever impact field the ranking is built on, which is defined for every ranking tool. Third instance of the same class in this build -- raw variance at rollup grain, expense basis beside operating-income basis, and now a missing flag -- and the lesson each time is that a sign convention has to be carried explicitly rather than inferred from whatever column happens to be present.
- Also fixed: account names repeat across departments, so a briefing could list "Salaries" twice with different figures. Colliding names now carry their member id.

### Decision: the deterministic narrative falls back to the briefing
- Context: the copilot's `injection_narrative` reads a fixed schema -- revenue versus budget, top drivers, the ARR bridge -- and produces nothing for a run that filled none of those slots. So a comparative question fell back to empty prose and was unpublishable.
- Choice & why: when the canonical narrative yields nothing, the text is generated from the briefing instead. Every figure there is already a ledger value carried by reference, so the audit verifies it identically -- and it states no recommendation, for the same reason the briefing does not.


### Decision: three Word deliverables, with the packet as the point
- Context: the deck is the board-facing artifact. The documents that actually circulate in a monthly close are Word: a flash to the CFO on day three or four, the monthly variance commentary, and a budget-owner packet per department.
- Choice & why: `reports.py` builds all three. The **packet** is the reason the module exists -- writing five of them by hand every month, pulling one owner's lines out of the close pack, formatting, mailing, chasing the reply, is the highest-volume and lowest-judgment task in the cycle, and it is the one automation should remove entirely. It is also the clearest hours-saved story: five documents from one run, each containing only that owner's numbers, each with a sign-off block.
- Same rule as the deck, for the same reason: exactly one way a number reaches a page (`Report.fig()`), no arithmetic on a financial value anywhere in the module, and a test that re-opens each generated `.docx`, reads every paragraph and table cell back out, and audits them with the same numeric auditor used on the commentary.
- A test also asserts a packet contains only its own department's accounts. In a real organisation that is not a formatting preference.

### Decision: the recommendation section is present and deliberately blank
- Context: the standard variance-commentary format ends with a recommendation. This tool does not generate one, because a recommendation is not a retrieved number and nothing could verify it.
- Choice & why: every document carries the section as an explicitly empty box with a grey note: "Intentionally blank. This tool reports what the data shows and does not recommend action -- that judgment belongs to the analyst, who signs below." That is not a gap being papered over; it is the division of labour made visible on the page, and it is the honest description of the workflow -- the tool assembles every fact, the analyst supplies the judgment and signs for it.
- Tests assert the section exists in all three document types AND that no document quietly advises in its prose while the box claims it does not.

### Decision: the monthly plan decomposes every department, not the top two
- Context: the plan stopped at two on the reasoning that a monthly package only needs the largest drivers.
- Why that was wrong: it cost about ten milliseconds to fix -- three additional local queries -- and it left the package genuinely incomplete. A close pack that explains two of five departments is not the whole picture, and it cannot produce a variance packet for the three owners it skipped. Fourteen steps, still around forty milliseconds.
- Consequence caught immediately by an existing invariant: `driver_detail_by_account` was capped at ten rows while all twenty-four were whitelisted, so the model was permitted to state figures it could not read -- the same silent failure as an unregistered field. The cap is gone.

### Note: a sign-off that leaked between sessions
`_agent_run_for` is cached with `st.cache_resource`, which is shared across every session on the server, and it originally returned the `PublicationPacket` as well as the run. So one visitor signing off would make the package appear signed to every other visitor of the deployed app -- an approval attached to nobody, on the one artifact whose entire purpose is that a named human accepted it.

Found by a test that signed off in one app instance and then found the documents already unlocked in a fresh one. The run is shared because it is deterministic and identical for everyone; the decision is not, and is now built per session.

### Note: two silent no-op edits in one session
Two source edits in this session did nothing and reported success anyway -- a `.replace()` whose target string had drifted, printing "truncation removed" when it had removed nothing, and an app wiring whose target no longer existed. Both were caught only because the next check disagreed with the message.

Every scripted edit now asserts that it changed the file and that the intended state holds afterwards, rather than printing a success line unconditionally. It is the same lesson as the rest of this project in miniature: the report that something worked is not evidence that it worked.


### Decision: one Deliverables section, four buttons, each explained before it is pressed
- Context: the deck sat under one heading and the Word documents under another, so a reader had to discover that the tool produced four different artifacts, and neither section said what a button would produce until after it had been pressed.
- Choice & why: a single **Deliverables** heading with four blocks -- board deck, flash, monthly memo, and all budget-owner packets -- each carrying a sentence on what it produces and who it is for BEFORE the button. They are one idea (what this run can hand you) and belong under one heading; splitting them by file format was organising the page around the implementation rather than the reader.
- Each build is independent, so producing the deck does not rebuild the documents, and the download button is labelled with the filename itself rather than the word "Download" wrapped around it -- the artifact is the thing, not the instruction.
- The packet block states the count from the run rather than assuming five, so a period with fewer departments reads correctly.
- Everything stays behind sign-off. An unsigned deck or packet is exactly the artifact that should not be leaving the building, and that rule now applies to all four in one place instead of two.


### Decision: metric rows size themselves to their longest value
- Context: the ARR block rendered five metrics in one row and Streamlit truncated the eight-figure balances to "$28,501,6\u2026". A cut-off number in a tool whose entire claim is that its figures are exact and traceable.
- The constraint is COLUMNS, not characters. Every metric value in the app is eleven characters or fewer; a four-column row holds eleven comfortably and a five-column row does not. Patching the one row would have left the next five-column layout free to do the same thing.
- Choice & why: `_metric_row()` takes the label/value pairs, measures the LONGEST value, picks the row width from it (four up to twelve characters, three up to sixteen, two beyond), and wraps rather than squeezes. Absent values are dropped rather than rendered blank. Every metric row on the agent page goes through it.
- Abbreviating to $28.5M was the alternative and was rejected: it would have hidden the truncation rather than fixed it, in the one place precision is the point. Balances and flows are also laid out separately, which is the same split the deck already makes for the same reason -- they differ by two orders of magnitude.
- Mechanism rather than care: a unit test drives `_metric_row` directly with short, long and absent values; a source check fails on any row wider than four columns; and a rendered check across four periods asserts no metric contains an ellipsis and none exceeds the four-column budget. ARR climbs across the dataset, so a layout that fits in January may not in December -- the test covers both ends.


### Decision: the agent builds its own marts on a cold start
- Context: the deployed app failed on the agent page with `IO Error: Cannot open database "data/processed/fpa.duckdb" in read-only mode: database does not exist`. Every other page worked.
- Root cause: `data/processed/*.duckdb` is gitignored, correctly -- it is a build artifact derived from the committed CSVs, not source. So a fresh checkout has no database, and the agent opens it READ-ONLY, which cannot create one. The other pages compute from the CSVs in memory and never touch DuckDB, which is why the failure was isolated to the agent.
- Why local testing could never have caught it: anyone who has run the project once already has the file. The defect is invisible on every machine except a brand-new one, which in practice means the deployment and nowhere else -- the same shape as the CRLF hash and the cp1252 encoding defects, and the third time in this build that "works on my machine" was the whole problem.
- Choice & why: `materialize.ensure_ready()` builds the database and marts when they are absent, unreadable, or stale -- all three answered the same way, by rebuilding from the committed CSVs. The app calls it once per server behind `st.cache_resource`, with a spinner on the cold start and a hash comparison on every call after. Materialization already knew how to build from source; it simply had never been invoked on a machine that had never run it.
- The failure path is explicit rather than silent: if preparation fails, the agent page says so and notes that the other pages are unaffected, instead of surfacing a DuckDB stack trace.
- Mechanism: a test points `DB` at an empty temporary directory, asserts that a read-only connect raises (reproducing the deployed failure), then asserts `ensure_ready` builds it, that the marts verify, and that a second call rebuilds nothing. A second test asserts the committed demo runs are not gitignored, since the public link has nothing to replay if they are.
