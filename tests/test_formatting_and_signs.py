"""
test_formatting_and_signs.py
============================
Regression guards for two defects found by reviewing the deployed UI.

1. The display formatter emitted "$-793K" (sign glued between the currency
   symbol and the digits) instead of "-$793K".

2. More seriously: the numeric audit's money regex accepted a minus only AFTER
   the "$". "-$793K" -- the conventional form, and the one a model writes
   unprompted -- was parsed as +793,000, so the audit was sign-blind to the
   common notation. A sign error on a variance inverts favourable and
   unfavourable, so it must be read correctly.

These tests fail if either behaviour is reintroduced.
"""
from __future__ import annotations
import os
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, SRC)

from guardrails.numeric_audit import extract_mentions  # noqa: E402


# ---------------------------------------------------------------------------
# 1. display formatter
# ---------------------------------------------------------------------------
def _load_formatters():
    """Pull money()/pct() out of app.py without importing streamlit."""
    src = open(os.path.join(APP, "app.py")).read()
    start = src.index("def money(")
    end = src.index("def month_label(")
    ns = {"pd": pd}
    exec(src[start:end], ns)
    return ns["money"], ns["pct"]


def _load_money():
    return _load_formatters()[0]


def _load_pct():
    return _load_formatters()[1]


def test_money_uses_accounting_parentheses_for_negatives():
    money = _load_money()
    assert money(-793_000) == "($793K)"
    assert money(-112_386.4) == "($112K)"
    assert money(-2_604_611.2) == "($2.60M)"
    assert money(-950) == "($950)"
    assert money(-7_000) == "($7K)"
    # positives are unchanged
    assert money(2_604_611.2) == "$2.60M"
    assert money(29_210_000) == "$29.21M"
    # never emit the "$-" form anywhere
    for v in (-1, -999, -1_000, -1e6, -1.5e6):
        assert "$-" not in money(v)


def test_money_does_not_render_negative_zero():
    money = _load_money()
    assert money(-0.0) == "$0"


def test_money_can_fall_back_to_a_leading_minus():
    money = _load_money()
    assert money(-793_000, parens=False) == "-$793K"
    assert money(793_000, parens=False) == "$793K"


def test_pct_uses_accounting_parentheses_for_negatives():
    pct = _load_pct()
    assert pct(-0.30) == "(30.0%)"
    assert pct(-0.041) == "(4.1%)"
    assert pct(0.81) == "81.0%"
    assert pct(0.922) == "92.2%"
    assert pct(None) == "n/a"
    assert pct(-0.30, parens=False) == "-30.0%"


@pytest.mark.parametrize("text,expected,kind", [
    ("operating income of ($793K)", -793_000.0, "dollar"),
    ("a variance of ($112.4K)", -112_400.0, "dollar"),
    ("($1,250,000) of exposure", -1_250_000.0, "dollar"),
    ("margin of (30.0%)", -0.30, "percent"),
])
def test_audit_reads_accounting_parentheses_as_negative(text, expected, kind):
    """($793K) is -793,000. Reading it as positive is the same class of bug as
    reading -$793K as positive: a figure with inverted favourability would
    verify green against the computed magnitude."""
    (m,) = [m for m in extract_mentions(text) if m.kind == kind]
    assert m.value == pytest.approx(expected)


def test_ordinary_parentheses_do_not_flip_the_sign():
    """Only a MATCHED pair around the figure means negative."""
    (m,) = [m for m in extract_mentions("(revenue of $2.60M") if m.kind == "dollar"]
    assert m.value > 0
    (p,) = [m for m in extract_mentions("margin fell (down from 81.0%")
            if m.kind == "percent"]
    assert p.value > 0


# ---------------------------------------------------------------------------
# 2. audit sign parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("operating income of -$793K", -793_000.0),
    ("operating income of $-793K", -793_000.0),
    ("a -$112.4K miss", -112_400.0),
    ("down $-112.4K", -112_400.0),
    ("the effect was -$24,166.67", -24_166.67),
    ("revenue of $2.60M", 2_600_000.0),
])
def test_audit_reads_the_sign_from_either_side_of_the_dollar(text, expected):
    mentions = [m for m in extract_mentions(text) if m.kind == "dollar"]
    assert len(mentions) == 1, f"expected one dollar mention in {text!r}"
    assert mentions[0].value == pytest.approx(expected)


def test_leading_minus_form_is_not_read_as_positive():
    """The specific regression: '-$X' must NOT come back as +X."""
    (m,) = [m for m in extract_mentions("came in at -$62,600.00") if m.kind == "dollar"]
    assert m.value < 0


def test_sign_parsing_does_not_disturb_non_currency_numbers():
    # bare year is still not audited as currency
    assert [m for m in extract_mentions("the year 2025 was hard")] == []
    # a bare sub-1 decimal is still classified as a ratio
    (m,) = extract_mentions("NRR of 0.923")
    assert m.kind == "percent"
    # negative percentages still parse as percentages
    (p,) = [m for m in extract_mentions("margin fell -4.1%") if m.kind == "percent"]
    assert p.value == pytest.approx(-0.041)
