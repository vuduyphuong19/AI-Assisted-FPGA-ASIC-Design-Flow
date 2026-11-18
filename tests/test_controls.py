"""Tests for the controls that keep agents honest. Run: python3 -m unittest discover -s tests -v"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mae.evidence import (ClaimType, EvidenceLedger, EvidenceRecord, LedgerError, RunStatus,  # noqa: E402
                          new_evidence_id, sha256_bytes)
from mae.gates import Gate, tool_passed  # noqa: E402
from mae.reports import validate  # noqa: E402
from mae.runner import ToolRunner, parse_sim  # noqa: E402
from mae.workspace import Workspace  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.dir)
        (self.dir / "rtl").mkdir()
        (self.dir / "rtl/x.v").write_text("module x; endmodule\n")
        self.key = self.ws.runner_key()
        self.ledger = EvidenceLedger(self.ws.state_dir / "ledger.jsonl", {"runner-local": self.key})
        self.runner = ToolRunner("runner-local", self.key, self.ledger, self.ws)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def not_run_record(self):
        # 'sta' is not installed here, so this exercises the NOT_RUN path without EDA tools
        return self.runner.run("sta", "sta", "rtl/x.v", {"rtl": "rtl/x.v"}, {"sta_script": "none"})


class TestLedger(Base):
    def test_agent_cannot_fabricate_evidence(self):
        raw = self.dir / "fake"
        raw.mkdir()
        rec = EvidenceRecord(new_evidence_id(), "sim", "rtl/x.v", "sim", {"runner_id": "runner-local"}, "iverilog", "12",
                             [["vvp"]], {}, {"rtl/x.v": self.ws.hash("rtl/x.v")}, {}, [0], RunStatus.PASS,
                             {"requirements": {"REQ-1": {"result": "PASS", "checks": 9, "errors": 0}}}, str(raw),
                             prev_hash=self.ledger.head)
        rec.record_hash = sha256_bytes(rec.canonical())
        rec.signature = hmac.new(b"agent-guess", rec.record_hash.encode(), hashlib.sha256).hexdigest()
        with self.assertRaises(LedgerError):
            self.ledger.append(rec)

    def test_unknown_runner_rejected(self):
        rec = self.not_run_record()
        rec2 = EvidenceRecord(**{**rec.__dict__, "evidence_id": new_evidence_id(), "provenance": {"runner_id": "me"}})
        with self.assertRaises(LedgerError):
            self.ledger.append(rec2)

    def test_audit_detects_edited_result(self):
        self.not_run_record()
        lines = self.ledger.path.read_text().splitlines()
        obj = json.loads(lines[0])
        obj["record"]["status"] = "PASS"                     # someone hides a failure
        self.ledger.path.write_text(json.dumps(obj) + "\n")
        self.assertTrue(any("modified" in p for p in self.ledger.audit()))

    def test_audit_detects_deleted_raw_log(self):
        rec = self.not_run_record()
        for f in Path(rec.raw_location).iterdir():
            f.unlink()
        self.assertTrue(any("raw evidence" in p for p in self.ledger.audit()))


class TestGates(Base):
    def test_missing_evidence_fails(self):
        r = Gate("g", [tool_passed("lint", "rtl/x.v")]).evaluate(self.ledger, self.ws, {})
        self.assertFalse(r.passed)
        self.assertIn("no evidence", r.failures[0].detail)

    def test_not_run_fails(self):
        self.not_run_record()
        r = Gate("g", [tool_passed("sta", "rtl/x.v")]).evaluate(self.ledger, self.ws, {})
        self.assertFalse(r.passed)
        self.assertIn("NOT_RUN", r.failures[0].detail)

    def test_stale_evidence_fails_after_change(self):
        self.not_run_record()
        (self.dir / "rtl/x.v").write_text("module x; wire a; endmodule\n")
        r = Gate("g", [tool_passed("sta", "rtl/x.v")]).evaluate(self.ledger, self.ws, {})
        self.assertIn("stale", r.failures[0].detail)

    def test_only_waivable_gate_can_be_waived(self):
        self.assertFalse(Gate("g", [tool_passed("lint", "rtl/x.v")]).evaluate(self.ledger, self.ws, {"g": "alice"}).passed)
        self.assertTrue(Gate("g", [tool_passed("lint", "rtl/x.v")], waivable=True).evaluate(self.ledger, self.ws, {"g": "alice"}).passed)


class TestParsersAndReports(Base):
    def test_incomplete_sim_is_error_not_pass(self):
        d = self.dir / "run"
        d.mkdir()
        (d / "cmd1.stdout").write_text("REQ_RESULT REQ-1 PASS checks=5 errors=0\n")   # no SUMMARY: crashed
        status, _ = parse_sim(d, [0, 0], {})
        self.assertIs(status, RunStatus.ERROR)

    def test_vacuous_requirement_fails(self):
        d = self.dir / "run"
        d.mkdir()
        (d / "cmd1.stdout").write_text("REQ_RESULT REQ-1 PASS checks=0 errors=0\nSUMMARY PASS\n")
        status, s = parse_sim(d, [0, 0], {})
        self.assertIs(status, RunStatus.FAIL)
        self.assertEqual(s["vacuous_requirements"], ["REQ-1"])

    def test_claims_downgraded(self):
        (self.dir / "reports").mkdir()
        rep = {"task": "rtl", "agent": "rtl-designer", "outputs": ["rtl/x.v"], "claims": [
            {"text": "all tests pass", "type": "tool_result"},
            {"text": "proven", "type": "verified_fact", "evidence_refs": ["EV-nope"]}]}
        (self.dir / "reports/rtl.json").write_text(json.dumps(rep))
        errors, down, _ = validate(self.dir / "reports/rtl.json", "rtl", "rtl-designer", ["rtl/x.v"], self.dir, self.ledger)
        self.assertEqual(errors, [])
        self.assertEqual([d["to"] for d in down], ["unverified", "unverified"])

    def test_invalid_claim_type_blocks(self):
        (self.dir / "reports").mkdir()
        rep = {"task": "rtl", "agent": "rtl-designer", "outputs": ["rtl/x.v"], "claims": [{"text": "x", "type": "fact"}]}
        (self.dir / "reports/rtl.json").write_text(json.dumps(rep))
        errors, _, _ = validate(self.dir / "reports/rtl.json", "rtl", "rtl-designer", ["rtl/x.v"], self.dir, self.ledger)
        self.assertTrue(errors)


class TestHooks(unittest.TestCase):
    def hook(self, script, payload):
        p = subprocess.run([sys.executable, str(ROOT / ".claude/hooks" / script)], input=json.dumps(payload),
                           capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)})
        return p.returncode

    def t(self, agent, tool, **inp):
        return self.hook("guard_tools.py", {"agent_type": agent, "tool_name": tool, "tool_input": inp, "cwd": str(ROOT)})

    def test_barriers_and_scopes(self):
        self.assertEqual(self.t("dv-engineer", "Read", file_path=str(ROOT / "rtl/sync_fifo.v")), 2)
        self.assertEqual(self.t("dv-engineer", "Write", file_path=str(ROOT / "dv/tb.v")), 0)
        self.assertEqual(self.t("rtl-designer", "Write", file_path=str(ROOT / "dv/tb.v")), 2)
        self.assertEqual(self.t("rtl-designer", "Write", file_path=str(ROOT / ".claude/hooks/guard_tools.py")), 2)
        self.assertEqual(self.t("ic-orchestrator", "Write", file_path=str(ROOT / "rtl/x.v")), 2)
        self.assertEqual(self.t("rtl-designer", "Read", file_path=str(ROOT / ".mae/runner.key")), 2)

    def test_bash_policy(self):
        self.assertEqual(self.t("ic-orchestrator", "Bash", command="python3 tools/mae.py approve signoff --signer x --reason y"), 2)
        self.assertEqual(self.t("rtl-designer", "Bash", command="python3 tools/mae.py run lint"), 0)
        self.assertEqual(self.t("rtl-designer", "Bash", command="python3 tools/mae.py run lint > rtl/x.v"), 2)
        self.assertEqual(self.t("dv-engineer", "Bash", command="cat rtl/sync_fifo.v"), 2)

    def test_spawn_requires_brief(self):
        self.assertEqual(self.hook("guard_spawn.py", {"tool_name": "Agent", "agent_type": "ic-orchestrator",
                                                      "tool_input": {"subagent_type": "dv-engineer", "prompt": "write a tb"}}), 2)


if __name__ == "__main__":
    unittest.main()
