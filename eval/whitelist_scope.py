"""
whitelist_scope.py
==================
Measures -- rather than asserts -- two things the guarantee depends on:

  1. How much a WIDE whitelist weakens the audit. The numeric audit checks each
     figure against a whitelist of computed values. The more values in the
     whitelist, the more likely a fabricated figure lands near one by
     coincidence and is waved through. This is the price of handing the chat the
     whole dataset in one payload instead of a relevant slice.

  2. How much WRITE PRECISION weakens the audit. "$2M" carries a rounding window
     of +/-$500K. Against a dense set of computed values, almost any number sits
     within some window, so "verified" would mean nothing. This is why the audit
     rejects figures below 3 significant figures outright (numeric_audit.
     MAX_REL_TOL) rather than blessing them.

Method: draw plausible dollar figures at random across the range the company's
real numbers occupy, write each at a given number of significant figures, and
count how many each whitelist falsely verifies. Lower is better.

Caveat stated plainly: a real model's fabrications are not uniformly random --
they cluster near plausible values -- so treat these as a directional measure of
the whitelist/precision tradeoff, not a probability of being fooled in practice.
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

SIG_FIGS = [(1, "$2M"), (2, "$1.7M"), (3, "$1.73M"), (4, "$1.730M"), (6, "$1,730,055")]


def tol_for(value: float, sig: int, floor: float = 1.0) -> float:
    """Tolerance implied by writing `value` at `sig` significant figures."""
    if value <= 0:
        return floor
    exp = int(np.floor(np.log10(abs(value))))
    lsd = 10.0 ** (exp - (sig - 1))
    return max(0.5 * lsd, floor)


def _false_verify(draws: np.ndarray, whitelist: np.ndarray, sig: int) -> float:
    if len(whitelist) == 0:
        return 0.0
    w = np.sort(whitelist)
    hits = 0
    for v in draws:
        tol = tol_for(v, sig)
        i = np.searchsorted(w, v)
        lo = w[max(i - 1, 0)]
        hi = w[min(i, len(w) - 1)]
        if abs(v - lo) <= tol or abs(v - hi) <= tol:
            hits += 1
    return hits / len(draws)


def measure(index: list, slice_allowed: list, n: int = 3000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    whole = np.array(sorted({abs(r.value) for r in index
                             if r.kind == "dollar" and r.value != 0}))
    sliced = np.array(sorted({abs(a.value) for a in slice_allowed
                              if a.kind == "dollar" and a.value != 0}))
    if len(whole) == 0:
        return pd.DataFrame()

    lo, hi = np.percentile(whole, 5), np.percentile(whole, 95)
    draws = np.exp(rng.uniform(np.log(max(lo, 1.0)), np.log(max(hi, 10.0)), n))

    rows = []
    for sig, example in SIG_FIGS:
        rows.append({
            "written_as": f"{sig} sig figs  e.g. {example}",
            "accepted_by_audit": sig >= 3,
            "false_verify_sliced": round(_false_verify(draws, sliced, sig), 4),
            "false_verify_whole_dataset": round(_false_verify(draws, whole, sig), 4),
        })
    df = pd.DataFrame(rows)
    df.attrs["slice_n"] = len(sliced)
    df.attrs["whole_n"] = len(whole)
    return df
