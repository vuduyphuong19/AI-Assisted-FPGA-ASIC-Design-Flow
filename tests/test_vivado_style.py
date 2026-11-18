"""Tests for the distilled-skill controls: style gate and the Vivado back end.
Run: python3 -m unittest discover -s tests -v   (uses examples/fake_vivado; no real Vivado needed)"""
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

from mae.evidence import EvidenceLedger, RunStatus  # noqa: E402
from mae.runner import ToolRunner  # noqa: E402
from mae.workspace import Workspace  # noqa: E402
from mae import vivado  # noqa: E402

FAKE = ROOT / "examples" / "fake_vivado"
FIXED = ROOT / "examples" / "fifo-scripted" / "rtl_v2_fixed.v"

BAD = """module bad #(parameter W = 8) (input wire clk, input wire rst_n, input wire [7:0] a, output reg [7:0] q);
  reg [7:0] cnt = 8'd0;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) q <= 8'd0; else q <= a / cnt;
  sub inst0 (clk, rst_n);
endmodule
"""


class Env(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.dir)
        for d in ("rtl", "impl"):
            (self.dir / d).mkdir()
        shutil.copy(FIXED, self.dir / "rtl/sync_fifo.v")
        shutil.copy(ROOT / "examples/fifo-scripted/sync_fifo.xdc", self.dir / "impl/sync_fifo.xdc")
        key = self.ws.runner_key()
        self.ledger = EvidenceLedger(self.ws.state_dir / "ledger.jsonl", {"runner-local": key})
        self.runner = ToolRunner("runner-local", key, self.ledger, self.ws)
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ["PATH"] = self.path
        for k in ("FAKE_VIVADO_WNS", "FAKE_VIVADO_LATCH", "FAKE_VIVADO_CDC", "FAKE_VIVADO_NOCLOCK", "FAKE_VIVADO_XDC_CRIT"):
            os.environ.pop(k, None)
        shutil.rmtree(self.dir)

    def fake_on_path(self):
        os.environ["PATH"] = str(FAKE) + os.pathsep + self.path

    def viv(self, kind):
        return self.runner.run(kind, kind, "rtl/sync_fifo.v", {"rtl": "rtl/sync_fifo.v", "xdc": "impl/sync_fifo.xdc"},
                               {"top": "sync_fifo", "part": "xc7a35tcpg236-1", "period_ns": 5.0, "logic_level_budget": 12})


class TestStyle(Env):
    def test_example_rtl_passes(self):
        rec = self.runner.run("style", "style", "rtl/sync_fifo.v", {"rtl": "rtl/sync_fifo.v"}, {})
        self.assertEqual(rec.status, RunStatus.PASS, rec.summary)

    def test_violations_fail_with_rule_ids(self):
        (self.dir / "rtl/sync_fifo.v").write_text(BAD)
        rec = self.runner.run("style", "style", "rtl/sync_fifo.v", {"rtl": "rtl/sync_fifo.v"}, {})
        self.assertEqual(rec.status, RunStatus.FAIL)
        for rule in ("R01", "R05", "R07", "R09", "R11", "R12", "R13", "R14"):
            self.assertIn(rule, rec.summary["by_rule"], rec.summary)


class TestVivado(Env):
    def test_not_installed_is_not_run(self):
        os.environ["PATH"] = "/nonexistent"
        self.assertEqual(self.viv("vivado_synth").status, RunStatus.NOT_RUN)

    def test_runner_writes_tcl_constraints_before_synth(self):
        self.fake_on_path()
        rec = self.viv("vivado_synth")
        tcl = (Path(rec.raw_location) / "run.tcl").read_text().splitlines()
        self.assertLess(next(i for i, l in enumerate(tcl) if l.startswith("read_xdc")),
                        next(i for i, l in enumerate(tcl) if l.startswith("synth_design")))
        self.assertNotIn("read_verilog -v", "\n".join(tcl))
        self.assertIn("FAKE", rec.tool_version)

    def test_synth_and_impl_pass(self):
        self.fake_on_path()
        s = self.viv("vivado_synth")
        self.assertEqual(s.status, RunStatus.PASS, s.summary)
        self.assertEqual(s.summary["utilization"]["LUT"]["used"], 21)
        self.assertEqual(s.summary["timing_claim_type"], "estimate")
        self.assertIn("post-synthesis", s.summary["fmax_basis"])
        i = self.viv("vivado_impl")
        self.assertEqual(i.status, RunStatus.PASS, i.summary)
        self.assertEqual(i.summary["routing_errors"], 0)
        self.assertAlmostEqual(i.summary["timing"]["wns_ns"], 0.412)
        self.assertEqual(i.summary["fmax_basis"], "post-route")
        self.assertEqual(i.summary["missing"], [])

    def test_negative_slack_fails_impl(self):
        self.fake_on_path()
        os.environ["FAKE_VIVADO_WNS"] = "-0.300"
        i = self.viv("vivado_impl")
        self.assertEqual(i.status, RunStatus.FAIL)
        self.assertIn("setup WNS", i.summary["reason"])

    def test_latch_fails_synth(self):
        self.fake_on_path()
        os.environ["FAKE_VIVADO_LATCH"] = "1"
        s = self.viv("vivado_synth")
        self.assertEqual(s.status, RunStatus.FAIL)
        self.assertIn("latch", s.summary["reason"])

    def test_failed_xdc_command_fails_synth_and_impl(self):
        self.fake_on_path()
        os.environ["FAKE_VIVADO_XDC_CRIT"] = "1"
        for kind in ("vivado_synth", "vivado_impl"):
            r = self.viv(kind)
            self.assertEqual(r.status, RunStatus.FAIL, kind)
            self.assertIn("constraint not applied", r.summary["reason"])
        # a CRITICAL WARNING unrelated to the XDC is recorded but does not fail the run
        log = vivado.parse_log("CRITICAL WARNING: [Synth 8-5000] something else\n")
        self.assertEqual((log["critical_warning_count"], log["xdc_critical_count"]), (1, 0))

    def test_parser_never_guesses(self):
        s = vivado.summarize(self.dir, "impl", 5.0, 12)
        self.assertIsNone(s["fmax_mhz"])
        self.assertIn("impl_timing_summary.rpt", s["missing"])


SYNC_OK = """`default_nettype none
module s2 (input wire clk, input wire rst_n, input wire d_async, output wire q);
    (* ASYNC_REG = "TRUE" *) reg d_meta_ff;
    (* ASYNC_REG = "TRUE" *)
    reg d_sync_ff;
    always @(posedge clk) begin
        if (!rst_n) begin d_meta_ff <= 1'b0; d_sync_ff <= 1'b0; end
        else begin d_meta_ff <= d_async; d_sync_ff <= d_meta_ff; end
    end
    assign q = d_sync_ff;
endmodule
`default_nettype wire
"""


class TestConstraintAndCdcChecks(Env):
    def write_xdc(self, text):
        (self.dir / "impl/sync_fifo.xdc").write_text(text)

    def test_async_reg_rule(self):
        (self.dir / "rtl/sync_fifo.v").write_text(SYNC_OK)
        rec = self.runner.run("style", "style", "rtl/sync_fifo.v", {"rtl": "rtl/sync_fifo.v"}, {})
        self.assertEqual(rec.status, RunStatus.PASS, rec.summary)
        (self.dir / "rtl/sync_fifo.v").write_text(SYNC_OK.replace('(* ASYNC_REG = "TRUE" *)\n    reg d_sync_ff;', "reg d_sync_ff;"))
        rec = self.runner.run("style", "style", "rtl/sync_fifo.v", {"rtl": "rtl/sync_fifo.v"}, {})
        self.assertEqual(rec.status, RunStatus.FAIL)
        self.assertEqual(rec.summary["by_rule"], {"R16": 1})

    def test_undocumented_exception_fails(self):
        self.fake_on_path()
        base = (ROOT / "examples/fifo-scripted/sync_fifo.xdc").read_text()
        self.write_xdc(base + "set_false_path -from [get_ports rst_n]\n")
        s = self.viv("vivado_synth")
        self.assertEqual(s.status, RunStatus.FAIL)
        self.assertIn("without a REQ reference", s.summary["reason"])
        self.write_xdc(base + "# REQ-1: reset input is synchronized by the integrator\nset_false_path -from [get_ports rst_n]\n")
        self.assertEqual(self.viv("vivado_synth").status, RunStatus.PASS)
        self.write_xdc(base + "set_false_path -from [get_ports rst_n]  ;# REQ-1 synchronized upstream\n")
        self.assertEqual(self.viv("vivado_synth").status, RunStatus.PASS)

    def test_loosened_period_fails(self):
        self.fake_on_path()
        self.write_xdc("create_clock -name clk -period 6.000 [get_ports clk]\n")
        s = self.viv("vivado_impl")
        self.assertEqual(s.status, RunStatus.FAIL)
        self.assertIn("differs from the spec target", s.summary["reason"])

    def test_unsafe_cdc_and_missing_async_reg_fail(self):
        self.fake_on_path()
        os.environ["FAKE_VIVADO_CDC"] = "critical"
        s = self.viv("vivado_synth")
        self.assertEqual(s.status, RunStatus.FAIL)
        self.assertIn("CDC-1", s.summary["reason"])
        os.environ["FAKE_VIVADO_CDC"] = "noasync"
        s = self.viv("vivado_synth")
        self.assertEqual(s.status, RunStatus.FAIL)
        self.assertIn("ASYNC_REG", s.summary["reason"])
        os.environ.pop("FAKE_VIVADO_CDC")

    def test_check_timing_no_clock_fails_impl(self):
        self.fake_on_path()
        os.environ["FAKE_VIVADO_NOCLOCK"] = "4"
        i = self.viv("vivado_impl")
        self.assertEqual(i.status, RunStatus.FAIL)
        self.assertIn("no_clock", i.summary["reason"])
        self.assertEqual(i.summary["check_timing"]["no_input_delay"], 12)   # expected OOC, recorded only
        os.environ.pop("FAKE_VIVADO_NOCLOCK")

    def test_directive_reaches_tcl(self):
        self.fake_on_path()
        rec = self.runner.run("vivado_impl", "impl", "rtl/sync_fifo.v",
                              {"rtl": "rtl/sync_fifo.v", "xdc": "impl/sync_fifo.xdc"},
                              {"top": "sync_fifo", "part": "p", "period_ns": 5.0, "directive": "Explore"})
        tcl = (Path(rec.raw_location) / "run.tcl").read_text()
        self.assertIn("place_design -directive Explore", tcl)
        self.assertIn("route_design -directive Explore", tcl)

    def test_audit_helpers(self):
        a = vivado.audit_xdc("create_clock -period 5.000 [get_ports clk]\nset_clock_groups -asynchronous -group a -group b\n", 5.0)
        self.assertEqual(len(a["exceptions_undocumented"]), 1)
        self.assertEqual(a["period_mismatch"], [])
        ct = vivado.parse_check_timing("1. checking no_clock (3)\n4. checking unconstrained_internal_endpoints (0)\n")
        self.assertEqual(ct, {"no_clock": 3, "unconstrained_internal_endpoints": 0})


class TestFlowTemplate(unittest.TestCase):
    def init(self, tool):
        d = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".mae", "__pycache__"))
        # fixed FIFO fixture, independent of whatever block spec/requirements.json currently holds
        subprocess.run([sys.executable, "tools/mae.py", "init", "--impl-tool", tool,
                        "--spec", "examples/fifo-scripted/requirements.json"], cwd=d, check=True, capture_output=True)
        return d

    def test_vivado_template(self):
        d = self.init("vivado")
        try:
            from mae.flow import Project
            p = Project(d)
            ids = {t.id: t for t in p.tasks}
            self.assertEqual(ids["constraints"].agent, "impl-engineer")
            self.assertEqual(ids["synth"].kind, "vivado_synth")
            self.assertEqual(ids["impl"].kind, "vivado_impl")
            self.assertIn("style", ids["rtl_freeze"].needs)
            self.assertEqual(p.cfg["impl"]["period_ns"], 5.0)          # 1000 / fmax 200 MHz
            self.assertEqual(p.cfg["files"]["xdc"], "impl/sync_fifo.xdc")
        finally:
            shutil.rmtree(d)

    def test_yosys_template_unchanged(self):
        d = self.init("yosys")
        try:
            from mae.flow import Project
            ids = {t.id: t for t in Project(d).tasks}
            self.assertNotIn("constraints", ids)
            self.assertEqual(ids["sta"].kind, "sta")
        finally:
            shutil.rmtree(d)


class TestImplPolicy(unittest.TestCase):
    def t(self, agent, path):
        payload = {"agent_type": agent, "tool_name": "Write", "tool_input": {"file_path": str(ROOT / path)}, "cwd": str(ROOT)}
        p = subprocess.run([sys.executable, str(ROOT / ".claude/hooks/guard_tools.py")], input=json.dumps(payload),
                           capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)})
        return p.returncode

    def test_impl_engineer_scope(self):
        self.assertEqual(self.t("impl-engineer", "impl/sync_fifo.xdc"), 0)
        self.assertEqual(self.t("impl-engineer", "reports/constraints.json"), 0)
        self.assertEqual(self.t("impl-engineer", "rtl/sync_fifo.v"), 2)

    def test_skills_exist_for_agents(self):
        import re
        for a in (ROOT / ".claude/agents").glob("*.md"):
            fm = a.read_text().split("---")[1]
            for s in re.findall(r"^  - ([\w-]+)$", fm, re.M):
                if s == "verification-code-style":      # the user's own account skill
                    continue
                self.assertTrue((ROOT / ".claude/skills" / s / "SKILL.md").exists(), f"{a.name}: {s}")



class TestOrchestratorPolicy(unittest.TestCase):
    def test_orchestrator_can_revert_and_run_new_tasks(self):
        def bash(cmd):
            payload = {"agent_type": "ic-orchestrator", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ROOT)}
            return subprocess.run([sys.executable, str(ROOT / ".claude/hooks/guard_tools.py")], input=json.dumps(payload),
                                  capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}).returncode
        for cmd in ("python3 tools/mae.py revert 1", "python3 tools/mae.py run impl", "python3 tools/mae.py run style",
                    "python3 tools/mae.py brief constraints", "python3 tools/mae.py gate timing"):
            self.assertEqual(bash(cmd), 0, cmd)
        self.assertEqual(bash("python3 tools/mae.py approve signoff --signer x --reason y"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMaintainerRole(unittest.TestCase):
    def call(self, tool, inp, agent=None, env_role=None):
        payload = {"tool_name": tool, "tool_input": inp, "cwd": str(ROOT)}
        if agent:
            payload["agent_type"] = agent
        env = {k: v for k, v in os.environ.items() if k != "MAE_ROLE"}
        env["CLAUDE_PROJECT_DIR"] = str(ROOT)
        if env_role:
            env["MAE_ROLE"] = env_role
        return subprocess.run([sys.executable, str(ROOT / ".claude/hooks/guard_tools.py")], input=json.dumps(payload),
                              capture_output=True, text=True, env=env).returncode

    def w(self, path, **kw):
        return self.call("Write", {"file_path": str(ROOT / path)}, **kw)

    def test_maintainer_can_edit_project_files(self):
        for path in ("spec/requirements.json", "mae/flow.py", "tools/mae.py", "tests/test_x.py",
                     ".claude/skills/rtl-coding/SKILL.md", ".claude/agents/rtl-designer.md", "README.md", "rtl/sync_fifo.v"):
            self.assertEqual(self.w(path, agent="project-maintainer"), 0, path)

    def test_maintainer_has_full_access(self):
        for path in (".claude/hooks/guard_tools.py", ".claude/settings.json", ".claude/mae-policy.json",
                     ".mae/ledger.jsonl", "/tmp/outside_project.txt"):
            self.assertEqual(self.w(path, agent="project-maintainer"), 0, path)
        self.assertEqual(self.call("Read", {"file_path": str(ROOT / ".mae/runner.key")}, agent="project-maintainer"), 0)
        bash = lambda c: self.call("Bash", {"command": c}, agent="project-maintainer")
        for cmd in ("python3 tools/mae.py approve signoff --signer x --reason y", "cat .mae/runner.key",
                    "python3 -m unittest discover -s tests", "git push origin main", "rm -rf build"):
            self.assertEqual(bash(cmd), 0, cmd)
        # other roles stay locked
        self.assertEqual(self.w(".claude/mae-policy.json", agent="rtl-designer"), 2)
        self.assertEqual(self.call("Bash", {"command": "cat .mae/runner.key"}, agent="ic-orchestrator"), 2)

    def test_env_fallback_and_default_unchanged(self):
        self.assertEqual(self.w("spec/requirements.json", env_role="project-maintainer"), 0)
        self.assertEqual(self.w("spec/requirements.json"), 2)                       # default = orchestrator
        self.assertEqual(self.w("mae/flow.py", agent="rtl-designer"), 2)             # workers still locked
        self.assertEqual(self.w(".claude/skills/rtl-coding/SKILL.md", agent="rtl-designer"), 2)
        # a subagent's own agent_type wins over the session's MAE_ROLE
        self.assertEqual(self.w("spec/requirements.json", agent="dv-engineer", env_role="project-maintainer"), 2)
