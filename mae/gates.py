"""Acceptance gates: machine-checkable predicates over the evidence ledger.

Semantics that matter:
  * missing evidence -> FAIL ("no evidence"), never PASS;
  * evidence whose input hashes differ from the current artifact heads -> STALE -> FAIL;
  * NOT_RUN / ERROR evidence -> FAIL;
  * a human waiver is the only way past a FAIL without new evidence, and it is itself a ledger-adjacent record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .workspace import Workspace
from .evidence import EvidenceLedger, EvidenceRecord, RunStatus, VerificationStatus


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    evidence_id: str | None = None


@dataclass
class GateResult:
    gate: str
    passed: bool
    checks: list[Check] = field(default_factory=list)
    waived_by: str | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


Predicate = Callable[[EvidenceLedger, Workspace], list[Check]]


def is_fresh(rec: EvidenceRecord, store: Workspace) -> tuple[bool, list[str]]:
    changed = [n for n, h in rec.input_hashes.items() if store.hash(n) != h]
    return (not changed), changed


def latest_fresh(ledger: EvidenceLedger, store: Workspace, kind: str, artifact_id: str) -> EvidenceRecord | None:
    """Newest record whose inputs match the files on disk now; falls back to the newest record (which will be stale).
    Reusing fresh older evidence matters after a revert: the pre-fix runs are valid again."""
    newest = None
    for rec in reversed(list(ledger.records())):
        if rec.kind != kind or rec.artifact_id != artifact_id:
            continue
        newest = newest or rec
        if is_fresh(rec, store)[0]:
            return rec
    return newest


def tool_passed(kind: str, artifact_id: str) -> Predicate:
    def pred(ledger: EvidenceLedger, store: Workspace) -> list[Check]:
        rec = latest_fresh(ledger, store, kind, artifact_id)
        name = f"{kind}:{artifact_id}"
        if rec is None:
            return [Check(name, False, "no evidence (tool never ran) - missing evidence is a failure")]
        fresh, changed = is_fresh(rec, store)
        if not fresh:
            if ledger.status_of(rec.evidence_id) is not VerificationStatus.STALE:
                ledger.annotate(rec.evidence_id, VerificationStatus.STALE, "gate-engine", f"inputs changed: {changed}")
            return [Check(name, False, f"stale: inputs changed since run ({', '.join(changed)})", rec.evidence_id)]
        if rec.status is not RunStatus.PASS:
            return [Check(name, False, f"tool status {rec.status.value}: {rec.summary.get('reason') or rec.summary.get('result') or ''}".strip(),
                          rec.evidence_id)]
        return [Check(name, True, "tool PASS on current inputs", rec.evidence_id)]
    return pred


def every_requirement_verified(req_ids: list[str], artifact_id: str) -> Predicate:
    """Traceability: each requirement needs a passing, non-vacuous check in fresh simulation evidence."""
    def pred(ledger: EvidenceLedger, store: Workspace) -> list[Check]:
        rec = latest_fresh(ledger, store, "sim", artifact_id)
        if rec is None:
            return [Check(f"trace:{r}", False, "no simulation evidence") for r in req_ids]
        fresh, _ = is_fresh(rec, store)
        reqs = rec.summary.get("requirements", {})
        out = []
        for r in req_ids:
            v = reqs.get(r)
            if v is None:
                out.append(Check(f"trace:{r}", False, "requirement has no test in the testbench", rec.evidence_id))
            elif v["checks"] == 0:
                out.append(Check(f"trace:{r}", False, "vacuous: test exists but performed 0 checks", rec.evidence_id))
            else:
                ok = fresh and v["result"] == "PASS"
                out.append(Check(f"trace:{r}", ok, f"{v['result']} ({v['checks']} checks, {v['errors']} errors)"
                                 + ("" if fresh else " [stale]"), rec.evidence_id))
        return out
    return pred


def coverage_bins_hit(bins: list[str], artifact_id: str, minimum: int = 1) -> Predicate:
    def pred(ledger: EvidenceLedger, store: Workspace) -> list[Check]:
        rec = latest_fresh(ledger, store, "sim", artifact_id)
        fresh = bool(rec) and is_fresh(rec, store)[0]
        cov = rec.summary.get("coverage", {}) if (rec and fresh) else {}
        return [Check(f"cover:{b}", cov.get(b, 0) >= minimum, f"hits={cov.get(b, 0)} (need >= {minimum})",
                      rec.evidence_id if rec else None) for b in bins]
    return pred


@dataclass
class Gate:
    name: str
    predicates: list[Predicate]
    waivable: bool = False

    def evaluate(self, ledger: EvidenceLedger, store: Workspace, waivers: dict[str, str]) -> GateResult:
        checks: list[Check] = []
        for p in self.predicates:
            checks.extend(p(ledger, store))
        passed = all(c.passed for c in checks)
        res = GateResult(self.name, passed, checks)
        if not passed and self.waivable and self.name in waivers:
            res.passed, res.waived_by = True, waivers[self.name]
        for c in checks:
            if c.evidence_id and ledger.status_of(c.evidence_id) in (VerificationStatus.RAW, VerificationStatus.STALE) \
                    and is_fresh(ledger.get(c.evidence_id), store)[0]:
                ledger.annotate(c.evidence_id, VerificationStatus.ACCEPTED if c.passed else VerificationStatus.REJECTED,
                                f"gate:{self.name}", c.detail)
        return res
