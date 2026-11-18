"""Validation of the structured reports subagents write at the end of each task.

Blocking errors (the SubagentStop hook refuses to let the subagent finish):
  missing/invalid JSON, wrong task/agent, missing output files, untyped claims, missing required keys.
Non-blocking downgrades (recorded as findings by `mae submit`):
  result claims without valid evidence IDs are downgraded to "unverified";
  agents can never self-assert "verified_fact" (only a passed gate creates one).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .evidence import ClaimType, EvidenceLedger, RunStatus

AGENT_CLAIM_TYPES = {c.value for c in ClaimType} - {ClaimType.FAILED_EXECUTION.value}
RESULT_WORDS = re.compile(r"\b(pass(es|ed)?|passing|meets?|met|MHz|GHz|ns|slack|cells?|area|power|coverage|proven|proved|clean)\b", re.I)


def validate(report_path: Path, task: str, agent: str, expected_outputs: list[str], root: Path,
             ledger: EvidenceLedger, need: tuple[str, ...] = ()) -> tuple[list[str], list[dict], dict | None]:
    errors: list[str] = []
    downgrades: list[dict] = []
    if not report_path.is_file():
        return [f"report {report_path.relative_to(root)} not found: write it before finishing"], [], None
    try:
        rep = json.loads(report_path.read_text())
    except json.JSONDecodeError as e:
        return [f"report is not valid JSON: {e}"], [], None
    if rep.get("task") != task:
        errors.append(f"report.task must be '{task}'")
    if rep.get("agent") != agent:
        errors.append(f"report.agent must be '{agent}'")
    outs = rep.get("outputs", [])
    if sorted(outs) != sorted(expected_outputs):
        errors.append(f"report.outputs must list exactly {expected_outputs}")
    for o in expected_outputs:
        if not (root / o).is_file():
            errors.append(f"declared output {o} does not exist")
    for key in need:
        if key not in rep:
            errors.append(f"report.{key} is required for this task")
    claims = rep.get("claims")
    if not isinstance(claims, list):
        errors.append("report.claims must be a list (may be empty)")
        claims = []
    for i, c in enumerate(claims):
        if not isinstance(c, dict) or "text" not in c or "type" not in c:
            errors.append(f"claims[{i}] needs 'text' and 'type'")
            continue
        if c["type"] not in AGENT_CLAIM_TYPES:
            errors.append(f"claims[{i}].type '{c['type']}' invalid; use one of {sorted(AGENT_CLAIM_TYPES)}")
            continue
        refs = c.get("evidence_refs") or []
        if c["type"] in (ClaimType.TOOL_RESULT.value, ClaimType.VERIFIED_FACT.value):
            bad = [r for r in refs if r not in {x.evidence_id for x in ledger.records()}]
            if not refs or bad:
                downgrades.append({"claim": c["text"], "from": c["type"], "to": ClaimType.UNVERIFIED.value,
                                   "why": "no evidence_refs" if not refs else f"unknown evidence {bad}"})
                c["type"] = ClaimType.UNVERIFIED.value
                continue
            if c["type"] == ClaimType.VERIFIED_FACT.value:
                downgrades.append({"claim": c["text"], "from": "verified_fact", "to": "tool_result",
                                   "why": "only a passed gate can promote a result to verified_fact"})
                c["type"] = ClaimType.TOOL_RESULT.value
            failing = [r for r in refs if ledger.get(r).status is not RunStatus.PASS]
            if failing and re.search(r"\bpass", c["text"], re.I):
                downgrades.append({"claim": c["text"], "from": "tool_result", "to": "unverified",
                                   "why": f"contradicted: cited evidence {failing} did not PASS"})
                c["type"] = ClaimType.UNVERIFIED.value
        elif c["type"] in (ClaimType.UNVERIFIED.value, ClaimType.ASSUMPTION.value) and RESULT_WORDS.search(c["text"]):
            downgrades.append({"claim": c["text"], "from": c["type"], "to": c["type"],
                               "why": "result-like wording without evidence; kept as non-evidence"})
    for i, f in enumerate(rep.get("findings", []) or []):
        if not {"severity", "artifact", "text"} <= set(f):
            errors.append(f"findings[{i}] needs severity, artifact, text")
        elif f["severity"] not in (1, 2, 3):
            errors.append(f"findings[{i}].severity must be 1, 2 or 3")
    return errors, downgrades, rep
