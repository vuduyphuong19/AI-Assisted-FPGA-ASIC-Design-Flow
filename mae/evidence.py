"""Evidence ledger: the only place where engineering results live.

Design rules enforced here (not by convention):
  * Only a ToolRunner holding the runner key can produce a record the ledger accepts
    (HMAC signature). Agents never hold that key, so they cannot fabricate evidence.
  * The ledger is append-only and hash-chained; `audit()` detects edits or deletions.
  * Every record carries the input artifact hashes it consumed, so a record becomes
    STALE automatically when an input artifact changes.
  * Failed, crashed and not-run executions are recorded too. Missing evidence is never PASS.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class ClaimType(str, Enum):
    VERIFIED_FACT = "verified_fact"        # tool result that has passed an acceptance gate
    TOOL_RESULT = "tool_result"            # produced by a runner, signed, not yet gated
    HYPOTHESIS = "hypothesis"              # agent reasoning, e.g. a suspected root cause
    ASSUMPTION = "assumption"              # taken as true without proof; needs approval if it constrains acceptance
    ESTIMATE = "estimate"                  # a number not measured by the authoritative tool
    UNVERIFIED = "unverified"              # an assertion with no evidence attached
    FAILED_EXECUTION = "failed_execution"  # a tool run that crashed, timed out or was not run


class RunStatus(str, Enum):
    PASS = "PASS"            # tool ran to completion and its own criteria passed
    FAIL = "FAIL"            # tool ran to completion and reported failure
    ERROR = "ERROR"          # tool crashed, timed out, or produced unparseable output
    NOT_RUN = "NOT_RUN"      # tool unavailable or prerequisites missing


class VerificationStatus(str, Enum):
    RAW = "raw"              # recorded, not yet consumed by a gate
    ACCEPTED = "accepted"    # used by a gate that passed
    REJECTED = "rejected"    # used by a gate that failed
    STALE = "stale"          # an input artifact has changed since this run


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


@dataclass
class EvidenceRecord:
    evidence_id: str
    task_id: str
    artifact_id: str                 # logical artifact this evidence is about, e.g. "rtl/sync_fifo.v"
    kind: str                        # lint | sim | formal | synth | sta | ...
    provenance: dict[str, Any]       # runner id, host, container image digest
    tool: str
    tool_version: str
    command: list[list[str]]         # every command executed, exactly as run
    config: dict[str, Any]           # parameters, seeds, constraints file hashes
    input_hashes: dict[str, str]     # logical artifact name -> sha256 consumed
    output_hashes: dict[str, str]    # raw file name -> sha256
    exit_codes: list[int]
    status: RunStatus
    summary: dict[str, Any]          # produced by a deterministic parser, never by an LLM
    raw_location: str
    verification_status: VerificationStatus = VerificationStatus.RAW
    claim_type: ClaimType = ClaimType.TOOL_RESULT
    started_at: float = 0.0
    finished_at: float = 0.0
    prev_hash: str = ""
    record_hash: str = ""
    signature: str = ""

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("record_hash", "signature", "verification_status"):
            d.pop(k)
        d["status"] = self.status.value
        d["claim_type"] = self.claim_type.value
        return d

    def canonical(self) -> bytes:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":")).encode()


class LedgerError(Exception):
    pass


class EvidenceLedger:
    """Append-only, hash-chained, signature-checked evidence store (JSONL on disk).

    Verification status changes (raw -> accepted/stale) are recorded as separate
    annotation lines, so the signed body of a record is never rewritten.
    """

    def __init__(self, path: Path, runner_keys: dict[str, bytes]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._runner_keys = dict(runner_keys)  # runner_id -> key (public-key crypto in production)
        self._records: dict[str, EvidenceRecord] = {}
        self._order: list[str] = []
        self._status: dict[str, VerificationStatus] = {}
        self._last_hash = "GENESIS"
        self._load()

    def _load(self) -> None:
        """Rebuild in-memory state from disk (each CLI call is a fresh process)."""
        self._records.clear(); self._order.clear(); self._status.clear(); self._last_hash = "GENESIS"
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            obj = json.loads(line)
            if obj["type"] == "record":
                r = dict(obj["record"])
                rec = EvidenceRecord(**{**r, "status": RunStatus(r["status"]), "claim_type": ClaimType(r["claim_type"])})
                self._records[rec.evidence_id] = rec
                self._order.append(rec.evidence_id)
                self._status[rec.evidence_id] = VerificationStatus.RAW
                self._last_hash = rec.record_hash
            elif obj["type"] == "annotation" and obj["evidence_id"] in self._records:
                self._status[obj["evidence_id"]] = VerificationStatus(obj["status"])

    @contextlib.contextmanager
    def locked(self):
        """Serialise writers across processes (parallel `mae run` calls from background subagents)."""
        lock = self.path.with_suffix(".lock")
        with lock.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                self._load()
                yield self
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -- writes ---------------------------------------------------------------
    def append(self, rec: EvidenceRecord) -> EvidenceRecord:
        runner_id = rec.provenance.get("runner_id", "")
        key = self._runner_keys.get(runner_id)
        if key is None:
            raise LedgerError(f"unknown runner '{runner_id}': evidence rejected")
        if rec.prev_hash != self._last_hash:
            raise LedgerError("prev_hash does not match ledger head: evidence rejected")
        expected_hash = sha256_bytes(rec.canonical())
        if rec.record_hash != expected_hash:
            raise LedgerError("record_hash mismatch: evidence rejected")
        expected_sig = hmac.new(key, rec.record_hash.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(rec.signature, expected_sig):
            raise LedgerError("bad signature: evidence was not produced by a registered runner")
        if rec.evidence_id in self._records:
            raise LedgerError("duplicate evidence id")
        raw = Path(rec.raw_location)
        for name, digest in rec.output_hashes.items():
            if sha256_file(raw / name) != digest:
                raise LedgerError(f"raw file {name} does not match its recorded hash")
        line = {"type": "record", "record": {**rec.body(), "record_hash": rec.record_hash, "signature": rec.signature}}
        with self.path.open("a") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        self._records[rec.evidence_id] = rec
        self._order.append(rec.evidence_id)
        self._status[rec.evidence_id] = VerificationStatus.RAW
        self._last_hash = rec.record_hash
        return rec

    def annotate(self, evidence_id: str, status: VerificationStatus, by: str, reason: str) -> None:
        if evidence_id not in self._records:
            raise LedgerError(f"no such evidence {evidence_id}")
        self._status[evidence_id] = status
        with self.path.open("a") as f:
            f.write(json.dumps({"type": "annotation", "evidence_id": evidence_id, "status": status.value,
                                "by": by, "reason": reason, "at": time.time()}) + "\n")

    # -- reads ----------------------------------------------------------------
    @property
    def head(self) -> str:
        return self._last_hash

    def get(self, evidence_id: str) -> EvidenceRecord:
        return self._records[evidence_id]

    def status_of(self, evidence_id: str) -> VerificationStatus:
        return self._status[evidence_id]

    def records(self) -> Iterable[EvidenceRecord]:
        return (self._records[i] for i in self._order)

    def latest(self, kind: str, artifact_id: str | None = None) -> EvidenceRecord | None:
        for eid in reversed(self._order):
            r = self._records[eid]
            if r.kind == kind and (artifact_id is None or r.artifact_id == artifact_id):
                return r
        return None

    def audit(self) -> list[str]:
        """Re-read the file from disk and verify chain + signatures + raw files. Returns problems."""
        problems: list[str] = []
        prev = "GENESIS"
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            obj = json.loads(line)
            if obj["type"] != "record":
                continue
            r = obj["record"]
            rh = r.pop("record_hash")
            signature = r.pop("signature")
            body = json.dumps(r, sort_keys=True, separators=(",", ":")).encode()
            if sha256_bytes(body) != rh:
                problems.append(f"line {n}: record body was modified after signing")
            if r["prev_hash"] != prev:
                problems.append(f"line {n}: hash chain broken (record inserted or deleted)")
            key = self._runner_keys.get(r["provenance"].get("runner_id", ""))
            if key is None or not hmac.compare_digest(
                    signature, hmac.new(key, rh.encode(), hashlib.sha256).hexdigest()):
                problems.append(f"line {n}: signature invalid")
            for name, digest in r["output_hashes"].items():
                p = Path(r["raw_location"]) / name
                if not p.exists() or sha256_file(p) != digest:
                    problems.append(f"line {n}: raw evidence file {name} missing or modified")
            prev = rh
        return problems


def new_evidence_id() -> str:
    return "EV-" + uuid.uuid4().hex[:10]


def runner_key_from_env(runner_id: str) -> bytes:
    """In production the key lives in the runner's secret store, never in agent containers."""
    env = os.environ.get(f"MAE_RUNNER_KEY_{runner_id.upper().replace('-', '_')}")
    return env.encode() if env else os.urandom(32)
