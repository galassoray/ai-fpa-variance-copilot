"""
replay.py
=========
Saving and reloading complete runs, so a demo needs no API key and no network.

WHY CACHED REPLAY IS THE DEFAULT, NOT THE FALLBACK
---------------------------------------------------
A live agent run costs money, takes six to ten seconds, and plans differently
each time. All three are fine in development and all three are bad in front of
an interviewer: the demo pauses on a spinner, and the plan on screen may not be
the plan discussed.

So a saved run is the default path. It renders in milliseconds, needs no
credential, and shows the same plan every time. Live mode is a toggle for
someone who wants to see it think.

THE INTEGRITY REQUIREMENT
-------------------------
A cached artifact is only worth showing if it is provably the real output of a
real run. Every saved run carries per-step result hashes; ``load_run``
recomputes them and refuses to return a run whose figures were edited after the
fact.

That matters more than it sounds. The one accusation this whole portfolio has
to withstand is "you could have typed those numbers in." A cached run that
verifies against hashes computed at execution time is the answer, and an
unverified JSON file is the accusation.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from agent.ledger import result_hash, verify_replay

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

#: Committed to the repo. These are what the deployed app serves with no key.
RUN_STORE = os.path.join(REPO, "data", "agent_runs")

FORMAT_VERSION = 2


class CorruptRun(RuntimeError):
    """A saved run failed integrity verification and will not be served."""


def _candidate_payload(candidate) -> dict:
    if candidate is None:
        return {}
    return {
        "status": candidate.status,
        "text": candidate.text,
        "source": candidate.source,
        "attempts": candidate.attempts,
        "audit_ran": candidate.audit_ran,
        "audit_passed": candidate.audit_passed,
        "deterministic_by_choice": candidate.deterministic_by_choice,
        "violations": list(candidate.violations),
        "entity_flags": list(candidate.entity_flags),
        "matched": [list(m) for m in candidate.matched],
        "reason": candidate.reason,
    }


def save_run(result, candidate=None, packet=None, label: str = "",
             directory: str = RUN_STORE) -> str:
    """Persist a run as a replayable artifact. Returns the file path."""
    os.makedirs(directory, exist_ok=True)
    period = (result.ledger.goal.get("period") or "unknown")[:7]
    name = label or f"{period}-{result.ledger.run_id}"
    path = os.path.join(directory, f"{name}.json")

    payload = {
        "format_version": FORMAT_VERSION,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "label": name,
        "ledger": json.loads(result.ledger.to_json()),
        "sections": {
            k: {"step": v["step"], "tool": v["tool"],
                "params": v["params"], "rows": v["rows"],
                "result_hash": v["result_hash"],
                "dimension": v.get("dimension")}
            for k, v in result.sections.items()
        },
        "outcome": result.outcome,
        "missing_sections": list(result.missing_sections),
        "refusal_reason": result.refusal_reason,
        "narrative": _candidate_payload(candidate),
        "gates": packet.to_dict() if packet is not None else {},
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


def list_runs(directory: str = RUN_STORE) -> list:
    """Saved runs, newest first, with enough metadata to choose one."""
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        led = d.get("ledger", {})
        cost = led.get("cost", {})
        out.append({
            "path": path,
            "label": d.get("label", name[:-5]),
            "period": (led.get("goal") or {}).get("period", ""),
            "outcome": d.get("outcome", ""),
            "steps": cost.get("steps", 0),
            "planner_model": cost.get("planner_model", "") or "none (deterministic)",
            "cost_usd": cost.get("cost_usd", 0.0),
            "saved_at_utc": d.get("saved_at_utc", ""),
            "has_narrative": bool((d.get("narrative") or {}).get("text")),
        })
    return sorted(out, key=lambda r: r["saved_at_utc"], reverse=True)


def load_run(path: str, verify: bool = True) -> dict:
    """Load a saved run, verifying its figures were not edited after the fact.

    Two independent checks, because they catch different tampering:
      * the ledger's own step hashes (``verify_replay``), and
      * each stored section against a hash recomputed from its rows here.

    A run failing either is not served. Refusing to display a corrupt artifact
    is the only defensible behavior for a tool whose entire claim is that its
    numbers are traceable.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    if not verify:
        return data

    ok, problems = verify_replay(data.get("ledger", {}))
    if not ok:
        raise CorruptRun(f"{os.path.basename(path)}: ledger integrity failed: "
                         f"{'; '.join(problems[:3])}")

    for name, sec in (data.get("sections") or {}).items():
        recomputed = result_hash(sec.get("rows", []))
        if recomputed != sec.get("result_hash"):
            raise CorruptRun(
                f"{os.path.basename(path)}: section '{name}' was modified "
                f"(stored {sec.get('result_hash')}, recomputed {recomputed})"
            )
    return data


class ReplayedRun:
    """A saved run presented with the same surface the live objects have.

    Lets the UI render a cached run through exactly the same code path as a live
    one. Two rendering paths would drift, and the cached path -- the one an
    interviewer actually sees -- would be the one that drifted unnoticed.
    """

    def __init__(self, data: dict):
        self.data = data
        self.sections = data.get("sections", {})
        self.outcome = data.get("outcome", "")
        self.missing_sections = list(data.get("missing_sections", []))
        self.refusal_reason = data.get("refusal_reason", "")
        self.ledger = _ReplayLedger(data.get("ledger", {}))

    @property
    def complete(self) -> bool:
        return self.outcome == "COMPLETED" and not self.missing_sections

    @property
    def narrative(self) -> dict:
        return self.data.get("narrative") or {}

    @property
    def gates(self) -> dict:
        return self.data.get("gates") or {}


class _ReplayLedger:
    """Read-only stand-in exposing what the renderer needs."""

    def __init__(self, d: dict):
        self._d = d
        self.run_id = d.get("run_id", "")
        self.goal = d.get("goal", {})
        self.notes = list(d.get("notes", []))
        self.results = {int(k): v for k, v in (d.get("results") or {}).items()}
        self._entries = [_ReplayEntry(e) for e in d.get("steps", [])]

    @property
    def entries(self) -> list:
        return list(self._entries)

    def entry(self, step_idx: int):
        for e in self._entries:
            if e.step_idx == step_idx:
                return e
        return None

    def cost_summary(self) -> dict:
        return dict(self._d.get("cost", {}))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self._d, indent=indent, default=str)


class _ReplayEntry:
    def __init__(self, d: dict):
        for k, v in d.items():
            setattr(self, k, v)
        self.params_declared = d.get("params_declared", {})
        self.params_resolved = d.get("params_resolved", {})
        self.valid_alternatives = d.get("valid_alternatives", [])

    @property
    def used_symbolic_reference(self) -> bool:
        return any(isinstance(v, str) and v.startswith("$")
                   for v in self.params_declared.values())
