"""
numeric_audit.py
================
The core guardrail. Given a narrative string and a FactPack, extract every
monetary and percentage figure the model wrote and verify each one matches a
computed value in the whitelist. Any figure that matches nothing is a
*fabrication* and fails the audit.

Design points a skeptical interviewer will probe:

* Precision-aware tolerance. A model may legitimately round: "$1.7M" for
  1,730,054.57 must PASS; "$2.1M" must FAIL. We infer the least-significant
  place of the *written* number and allow a match only within half a unit of
  that place (so a coarser mention gets a coarser, but still bounded, window).
* Scope. We audit decision-facing figures: currency amounts and percentages
  (incl. numbers carrying $, %, or a k/M/B magnitude suffix, and comma/decimal
  grouped magnitudes). Bare small integers and years (e.g. "12 months", "2025",
  "top 3") are not audited as financial claims -- they are contextual, not
  numbers a decision-maker would act on. This keeps the audit from raising false
  fabrications on ordinary prose while still catching every $ / % figure.
* Traceability. Every matched figure is tied back to the whitelist label, which
  is what powers the "show the math" view.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# ---- number extraction ----------------------------------------------------
_SUFFIX = {
    "k": 1e3, "K": 1e3, "thousand": 1e3,
    "m": 1e6, "M": 1e6, "mm": 1e6, "MM": 1e6, "million": 1e6,
    "b": 1e9, "B": 1e9, "bn": 1e9, "billion": 1e9,
}
# currency / magnitude: optional $, grouped or plain digits, optional decimals,
# optional magnitude suffix word/letter.
# The minus may sit EITHER side of the currency symbol. "-$793K" is the
# conventional finance form and is what a model writes unprompted; "$-793K" is
# what naive f-string formatting produces. Both must parse to -793000. Matching
# only the latter made the audit sign-blind to the common form: "-$793K" was
# read as +793,000, so a figure with the sign flipped could be VERIFIED against
# the computed magnitude. A sign error on a variance is a material error -- it
# inverts favourable and unfavourable -- so it has to be caught, not tolerated.
_NUM = (r"-?\$?\s?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
        r"|-?\$?\s?-?\d+(?:\.\d+)?")
# Accounting parentheses mean NEGATIVE: ($793K) is -793,000, and (30.0%) is
# -30%. This is the house convention in the UI and on every audited statement,
# so a model that has seen finance writing produces it unprompted. Without the
# optional (?P<open>\() / (?P<close>\)) groups the audit read "($793K)" as
# +793,000 -- the same sign-blindness as the "-$" case above, and the same
# consequence: a figure with inverted favourability verifying green against the
# computed magnitude.
_MONEY_RE = re.compile(
    rf"(?P<open>\()?(?P<num>{_NUM})\s?"
    rf"(?P<suffix>thousand|million|billion|mm|bn|[kKmMbB])?(?P<close>\))?",
)
_PCT_RE = re.compile(
    r"(?P<open>\()?(?P<num>-?\d+(?:\.\d+)?)\s?"
    r"(?:%|percent|percentage points|pts|ppt)(?P<close>\))?")


@dataclass
class Mention:
    text: str
    value: float          # canonical: dollars, or ratio for percents
    kind: str             # "dollar" | "percent"
    lsd: float            # absolute least-significant-digit unit of the mention


@dataclass
class Violation:
    mention: str
    value: float
    kind: str
    reason: str


@dataclass
class AuditResult:
    passed: bool
    violations: list = field(default_factory=list)
    matched: list = field(default_factory=list)   # (mention_text, value, label)

    @property
    def n_fabricated(self) -> int:
        return len(self.violations)


def _lsd_from_literal(num_str: str, scale: float) -> float:
    """Least-significant-digit unit implied by how the number was written."""
    s = num_str.replace("$", "").replace(",", "").replace(" ", "").lstrip("-")
    if "." in s:
        decimals = len(s.split(".")[1])
        base = 10 ** (-decimals)
    else:
        base = 1.0
    return base * scale


def extract_mentions(text: str) -> list:
    mentions: list = []
    spans: list = []

    # percentages first (so "4.7%" isn't also caught as a bare number)
    for m in _PCT_RE.finditer(text):
        raw = m.group("num")
        val = float(raw) / 100.0
        # A MATCHED pair of parentheses means negative. Require both sides, so
        # an ordinary parenthetical -- "margin fell (down from 81.0%)" -- is not
        # misread as a negative.
        if m.group("open") and m.group("close"):
            val = -abs(val)
        lsd = _lsd_from_literal(raw, 1.0) / 100.0
        mentions.append(Mention(m.group(0).strip(), val, "percent", lsd))
        spans.append((m.start(), m.end()))

    def overlaps(a, b):
        return any(not (b <= s or a >= e) for s, e in spans)

    for m in _MONEY_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        num_raw = m.group("num")
        suffix = m.group("suffix")
        # Strip the currency symbol/grouping, then fold a sign from EITHER side
        # of the "$" into the value ("-$793" and "$-793" both -> -793).
        cleaned = num_raw.replace("$", "").replace(",", "").replace(" ", "")
        negative = cleaned.count("-") % 2 == 1
        cleaned = cleaned.replace("-", "")
        # A MATCHED pair of parentheses is the accounting negative: ($793K).
        # Both sides required, so "(revenue of $2.60M)" stays positive.
        if m.group("open") and m.group("close"):
            negative = True
        try:
            base_val = float(cleaned)
        except ValueError:
            continue
        if negative:
            base_val = -base_val
        has_dollar = "$" in m.group(0)
        has_suffix = bool(suffix)
        has_group = "," in num_raw
        has_decimal = "." in cleaned
        # Qualify as a financial figure only if it carries a financial signal:
        # a $, a magnitude suffix (k/M/B), comma-grouping, or a decimal point.
        # Bare ungrouped integers (years like 2025, ordinals, small counts) are
        # contextual, not decision-facing currency, and are not audited here.
        # Models format money conventionally ($, grouping, or suffix), so this
        # does not weaken detection of fabricated dollar figures.
        if not (has_dollar or has_suffix or has_group or has_decimal):
            continue
        scale = _SUFFIX.get(suffix, 1.0) if suffix else 1.0
        value = base_val * scale
        lsd = _lsd_from_literal(num_raw, scale)
        # A bare decimal below 1 (no $, comma, or magnitude suffix) is a ratio,
        # not a sub-dollar amount -- e.g. a model writing NRR as 0.923 instead of
        # 92.3%. Audit it against the percent set so it matches the right computed
        # value (and a fabricated ratio is still caught), rather than being
        # absorbed by a near-zero dollar value.
        bare_decimal = has_decimal and not (has_dollar or has_suffix or has_group)
        if bare_decimal and abs(value) < 1:
            mentions.append(Mention(m.group(0).strip(), value, "percent", lsd))
            continue
        mentions.append(Mention(m.group(0).strip(), value, "dollar", lsd))
    return mentions


# A dollar figure written so coarsely that its rounding window swallows a large
# share of the computed range cannot be verified: at 1-2 significant figures a
# FABRICATED number lands within tolerance of some real value most of the time
# (measured: ~93% at "$2M", ~51% at "$1.7M" -- see eval/whitelist_scope.py).
# Verification there would be a rubber stamp, so such figures are REJECTED
# rather than blessed. 0.005 admits 3+ significant figures and excludes 1-2.
MAX_REL_TOL = 0.005


def audit(text: str, fact_pack, dollar_floor: float = 1.0,
          pct_floor: float = 0.0005, max_rel_tol: float = MAX_REL_TOL) -> AuditResult:
    """Verify every $ / % figure in `text` against the fact pack whitelist.

    A figure only passes if (a) it is precise enough to be meaningfully checked
    and (b) a computed value sits within its rounding window.
    """
    result = AuditResult(passed=True)
    for kind, floor in (("dollar", dollar_floor), ("percent", pct_floor)):
        allowed = fact_pack.allowed_by_kind(kind)
        for men in [x for x in extract_mentions(text) if x.kind == kind]:
            tol = max(0.5 * men.lsd, floor) * (1 + 1e-9)

            # precision gate (dollars only; percents are bounded, so their
            # absolute floor already keeps the window tight)
            if kind == "dollar" and max_rel_tol and abs(men.value) > 0:
                if tol / abs(men.value) > max_rel_tol:
                    result.violations.append(
                        Violation(men.text, men.value, kind,
                                  f"written too coarsely to verify "
                                  f"(+/-{tol:,.0f} on {men.value:,.0f}); "
                                  f"state at least 3 significant figures"))
                    continue

            hit = None
            for a in allowed:
                if abs(men.value - a.value) <= tol:
                    hit = a
                    break
            if hit is None:
                result.violations.append(
                    Violation(men.text, men.value, kind,
                              f"no computed {kind} within +/-{tol:g} of {men.value:g}"))
            else:
                result.matched.append((men.text, men.value, hit.label))
    result.passed = len(result.violations) == 0
    return result
