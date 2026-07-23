# Deploying the demo

The app is designed to work in two interview situations from one codebase. The
only difference between them is whether an API key is present.

| Situation        | What runs                              | API key            |
|------------------|----------------------------------------|--------------------|
| Send a link      | Deterministic narrative + full eval    | none (safe to share)|
| Screen-share live| Above **plus** live model generation   | your key, locally or in secrets |

Everything a decision-maker sees — the computed metrics, the deterministic
commentary, and the "0 fabricated numbers" eval proof — is key-free. Live model
generation is an optional enhancement.

---

## Run locally (screen-share)

```bash
pip install -r requirements.txt
streamlit run app/app.py
```

To generate live commentary, either paste a key into the sidebar (kept only in
the session), or set one before launching:

```powershell
$env:OPENAI_API_KEY = "sk-..."      # or $env:ANTHROPIC_API_KEY
streamlit run app/app.py
```

The Commentary page's "Live model narrative" section then generates with the
chosen provider and shows the audit re-checking every figure.

---

## Deploy a shareable link (Streamlit Community Cloud)

1. Push this repo to GitHub (public or private).
2. At share.streamlit.io, create an app pointing at this repo, branch, and the
   entrypoint **`app/app.py`**.
3. Deploy. The app boots by running the deterministic pipeline in-process, so no
   data files need to be committed — it regenerates the synthetic dataset from
   the seeded generator on first load.

**Do not put your API key in a public deployment.** The shared link intentionally
runs in deterministic mode: the deterministic narrative and the eval page fully
demonstrate the guarantee without a key, and nobody can spend your credits.

### Optional: a private live-demo deployment

If you want the live-model section active on a *private* deployment you control
(e.g. for a scheduled screen-share), add the key via Streamlit secrets instead of
committing it. In the app's **Settings → Secrets**:

```toml
OPENAI_API_KEY = "sk-..."
# or
ANTHROPIC_API_KEY = "sk-ant-..."
```

Keep that deployment private and rotate the key afterward. The app reads keys in
this order: pasted-in-session → `st.secrets` → environment variable.

---

## What the interviewer clicks through

- **Overview** — company P&L and KPI snapshot for the selected month.
- **Variance** — ranked drivers (sign-aware favorable/unfavorable) and OI-impact chart.
- **Forecast** — actual vs budget over time, plus a projected continuation whose
  method (linear trend / run-rate / driver-based ARR) is an explicit, labeled choice.
- **Commentary** — the guarded narrative, every figure tied to a computed value,
  plus the guarded analyst chat (needs a key).
- **Guardrails & Eval** — the proof: 0 fabricated numbers across generations, 100%
  adversarial catch rate, the measured whitelist/precision tradeoff, and a live
  "audit any commentary" box.
- **Decision log** — the "why" behind each architectural choice.

## Scenario inputs (the live demo)

The sidebar exposes every **input**: headcount plan, average comp, spend
baselines, churn, the FY25 story multipliers, starting ARR. Derived values
(salaries, revenue, every variance) are always recomputed — you cannot edit them,
by design. Moving an input recomputes the whole app in ~0.2s with all 11
validation checks still passing, so you can change the company mid-conversation
and the reconciliations still hold. "Reset to baseline" restores the canonical
dataset.

Note: the Guardrails & Eval page always runs against the **baseline** dataset, so
its headline figures stay stable no matter what you set in the sidebar.
