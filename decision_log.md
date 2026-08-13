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
