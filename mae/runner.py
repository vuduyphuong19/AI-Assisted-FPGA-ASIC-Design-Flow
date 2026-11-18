"""Tool runner: executes allow-listed EDA commands and writes signed evidence.

Agents submit ToolRunRequests (kind + params). They never supply a command line, never see
the runner key, and never write the summary: parsing is deterministic code in this file.
In production each runner is a separate container with pinned tool images; here it is a
subprocess wrapper with the same contract.
"""
from __future__ import annotations

import hashlib
import hmac
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .workspace import Workspace
from .evidence import (ClaimType, EvidenceLedger, EvidenceRecord, RunStatus, new_evidence_id,
                       sha256_bytes, sha256_file)


@dataclass
class ToolSpec:
    kind: str
    tool: str                                   # executable that must exist
    version_cmd: list[str]
    build: Callable[[Path, dict, dict], list[list[str]]]   # (run_dir, files, params) -> commands
    parse: Callable[[Path, list[int], dict], tuple[RunStatus, dict]]
    timeout_s: int = 300


def _first_line(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as e:  # tool missing or broken
        return f"unavailable ({type(e).__name__})"


# ----------------------------------------------------------------------------- parsers
def _read(run_dir: Path, name: str) -> str:
    p = run_dir / name
    return p.read_text(errors="replace") if p.exists() else ""


def parse_lint(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    log = _read(run_dir, "cmd0.stdout") + _read(run_dir, "cmd0.stderr")
    warnings = len(re.findall(r"^%Warning", log, re.M))
    errors = len(re.findall(r"^%Error(?!: Exiting)", log, re.M))
    status = RunStatus.PASS if codes == [0] and warnings == 0 and errors == 0 else RunStatus.FAIL
    return status, {"warnings": warnings, "errors": errors}


def parse_sim(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    if codes and codes[0] != 0:
        return RunStatus.ERROR, {"reason": "compile failed", "compile_exit": codes[0]}
    log = _read(run_dir, "cmd1.stdout")
    reqs = {m[0]: {"result": m[1], "checks": int(m[2]), "errors": int(m[3])}
            for m in re.findall(r"^REQ_RESULT (\S+) (PASS|FAIL) checks=(\d+) errors=(\d+)", log, re.M)}
    cover = {m[0]: int(m[1]) for m in re.findall(r"^COVER (\S+) (\d+)", log, re.M)}
    first_fails = re.findall(r"^CHECK_FAIL .*$", log, re.M)[:5]
    summary_line = re.search(r"^SUMMARY (PASS|FAIL)", log, re.M)
    summary = {"requirements": reqs, "coverage": cover, "first_failures": first_fails, "seed": params.get("seed")}
    if summary_line is None or not reqs:
        # the testbench did not finish: this is incomplete execution, never a pass
        summary["reason"] = "no SUMMARY/REQ_RESULT lines: simulation incomplete"
        return RunStatus.ERROR, summary
    zero_check = [r for r, v in reqs.items() if v["checks"] == 0]
    if zero_check:
        summary["vacuous_requirements"] = zero_check
    ok = summary_line.group(1) == "PASS" and all(v["result"] == "PASS" for v in reqs.values()) and not zero_check
    return (RunStatus.PASS if ok else RunStatus.FAIL), summary


def parse_formal(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    log = _read(run_dir, "cmd0.stdout")
    if "Induction step proven: SUCCESS!" in log:
        depth = re.findall(r"Trying induction with length (\d+)", log)
        return RunStatus.PASS, {"result": "proven (unbounded, k-induction)", "induction_length": int(depth[-1]) if depth else None}
    if "model found for base case: FAIL!" in log:
        step = re.findall(r"Trying induction with length (\d+)", log)
        return RunStatus.FAIL, {"result": "counterexample in base case", "cex_depth": int(step[-1]) if step else None}
    if "Reached maximum number of time steps" in log:
        return RunStatus.FAIL, {"result": "inconclusive: max induction length reached (treated as FAIL)"}
    return RunStatus.ERROR, {"result": "no recognisable proof outcome", "exit": codes}


def parse_synth(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    if codes != [0]:
        log = _read(run_dir, "cmd0.stdout") + _read(run_dir, "cmd0.stderr")
        latch = "select -assert-none" in log or "Assertion failed" in log
        return RunStatus.FAIL, {"reason": "latch inferred" if latch else "synthesis error", "exit": codes}
    stat = _read(run_dir, "stat.txt")
    ltp = _read(run_dir, "ltp.txt")
    cells = re.search(r"^\s*(\d+)\s+cells\s*$", stat, re.M) or re.search(r"Number of cells:\s+(\d+)", stat)
    flops = sum(int(n) for n in re.findall(r"^\s*(\d+)\s+\$_S?DFFE?_\w+", stat, re.M))
    depth = re.search(r"Longest topological path in \S+ \(length=(\d+)\)", ltp)
    return RunStatus.PASS, {
        "generic_cells": int(cells.group(1)) if cells else None,
        "flip_flops": flops,
        "latches": 0,
        # logic depth is a measured proxy, not a timing result: labelled so it cannot satisfy a timing gate
        "logic_depth_levels": {"value": int(depth.group(1)) if depth else None, "claim_type": ClaimType.ESTIMATE.value,
                               "note": "unmapped generic-gate depth; not a substitute for STA"},
        "library": "none (generic gates) - area in real units requires a liberty file",
    }


def parse_sta(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    log = _read(run_dir, "cmd0.stdout")
    wns = re.search(r"wns\s+(-?[\d.]+)", log)
    unconstrained = "unconstrained" in log.lower()
    if wns is None:
        return RunStatus.ERROR, {"reason": "no WNS in report"}
    return (RunStatus.PASS if float(wns.group(1)) >= 0 and not unconstrained else RunStatus.FAIL), {"wns_ns": float(wns.group(1))}


def parse_style(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
    log = _read(run_dir, "cmd0.stdout")
    findings = re.findall(r"^\S+:(\d+): (R\d\d): (.*)$", log, re.M)
    if not re.search(r"^mae.style: \d+ file\(s\), \d+ finding\(s\)", log, re.M):
        return RunStatus.ERROR, {"reason": "style checker did not finish", "exit": codes}
    by_rule: dict[str, int] = {}
    for _, rule, _ in findings:
        by_rule[rule] = by_rule.get(rule, 0) + 1
    summary = {"findings": len(findings), "by_rule": by_rule,
               "first": [f"line {ln} {rule}: {msg}" for ln, rule, msg in findings[:10]]}
    return (RunStatus.PASS if not findings else RunStatus.FAIL), summary


def _vivado_parse(stage: str):
    def parse(run_dir: Path, codes: list[int], params: dict) -> tuple[RunStatus, dict]:
        from .vivado import summarize
        s = summarize(run_dir, stage, params.get("period_ns"), int(params.get("logic_level_budget", 12)))
        run = s["run"]
        if run["error_count"]:
            s["reason"] = "Vivado reported ERROR lines"
            return RunStatus.FAIL, s
        if not run["done_marker"]:
            s["reason"] = "flow did not reach its end marker: incomplete run"
            return RunStatus.ERROR, s
        problems = []
        if run.get("xdc_critical_count"):
            problems.append(f"constraint not applied (CRITICAL WARNING x{run['xdc_critical_count']}): "
                            f"{run['xdc_critical_warnings'][:2]}")
        xa = s.get("xdc_audit") or {}
        if xa.get("exceptions_undocumented"):
            problems.append(f"timing exception without a REQ reference: {xa['exceptions_undocumented'][:3]}")
        if xa.get("period_mismatch"):
            problems.append(f"clock period differs from the spec target: {xa['period_mismatch'][:3]}")
        crit = [c for c in (s.get("cdc") or []) if c["severity"] == "Critical" and c["count"] > 0]
        no_async = [c for c in (s.get("cdc") or []) if c["rule"] in ("CDC-2", "CDC-5") and c["count"] > 0]
        if crit:
            problems.append("unsafe clock-domain crossing: " + ", ".join(f"{c['rule']} x{c['count']}" for c in crit))
        if no_async:
            problems.append("synchronizer without ASYNC_REG: " + ", ".join(f"{c['rule']} x{c['count']}" for c in no_async))
        if run["notable"]["latch"]:
            problems.append("latch inferred")
        if run["notable"]["multi_driven"]:
            problems.append("multi-driven net")
        tm = s.get("timing") or {}
        if stage == "synth":
            if s.get("utilization") in (None, {}):
                problems.append("utilization not found")
            # post-synthesis timing is an estimate: recorded, labelled, never a timing result
            s["timing_claim_type"] = ClaimType.ESTIMATE.value
        else:
            if s.get("routing_errors") != 0:
                problems.append(f"routing errors: {s.get('routing_errors')}")
            if not tm.get("clocks"):
                problems.append("no clocks defined: design unconstrained")
            if tm.get("wns_ns") is None or tm["wns_ns"] < 0:
                problems.append(f"setup WNS {tm.get('wns_ns')}")
            if tm.get("whs_ns") is not None and tm["whs_ns"] < 0:
                problems.append(f"hold WHS {tm['whs_ns']}")
            ct = s.get("check_timing")
            if ct is None:
                problems.append("check_timing section not found")
            else:
                bad = {k: v for k, v in ct.items()
                       if k in ("no_clock", "unconstrained_internal_endpoints", "loops", "latch_loops") and v > 0}
                if bad:
                    problems.append(f"check_timing: {bad}")
        ll = s.get("logic_levels") or {}
        if ll.get("paths_over_budget"):
            s["design_findings"] = [f"{ll['paths_over_budget']} path(s) over the {ll['budget']}-level logic budget "
                                    f"(max {ll['max_level']})"]
        if problems:
            s["reason"] = "; ".join(problems)
            return RunStatus.FAIL, s
        return RunStatus.PASS, s
    return parse


def _vivado_build(impl: bool):
    def build(d: Path, f: dict, p: dict) -> list[list[str]]:
        from .vivado import tcl_script
        (d / "run.tcl").write_text(tcl_script(p["top"], p["part"], f["rtl"], f["xdc"], impl,
                                              p.get("directive", "Default")))
        return [["vivado", "-mode", "batch", "-nolog", "-nojournal", "-notrace", "-source", "run.tcl"]]
    return build


# ----------------------------------------------------------------------------- tool registry
YOSYS = shutil.which("yosys") and "yosys" or "yowasp-yosys"

TOOLS: dict[str, ToolSpec] = {
    "lint": ToolSpec("lint", "verilator", ["verilator", "--version"],
                     lambda d, f, p: [["verilator", "--lint-only", "-Wall", "--top-module", p["top"], f["rtl"]]],
                     parse_lint),
    "sim": ToolSpec("sim", "iverilog", ["iverilog", "-V"],
                    lambda d, f, p: [["iverilog", "-g2012", "-o", "sim.vvp", f["rtl"], f["tb"]],
                                     ["vvp", "-n", "sim.vvp", f"+seed={p['seed']}"]],
                    parse_sim),
    "formal": ToolSpec("formal", YOSYS, [YOSYS, "-V"],
                       lambda d, f, p: [[YOSYS, "-p", f"read_verilog -formal {f['rtl']} {f['props']}; prep -top {p['top']}; "
                                         "flatten; async2sync; chformal -lower; memory_map; opt_clean; "
                                         f"sat -tempinduct -prove-asserts -set-assumes -seq 1 -maxsteps {p.get('maxsteps', 20)} -set-init-zero"]],
                       parse_formal),
    "synth": ToolSpec("synth", YOSYS, [YOSYS, "-V"],
                      lambda d, f, p: [[YOSYS, "-q", "-p", f"read_verilog {f['rtl']}; synth -top {p['top']}; "
                                        "tee -o stat.txt stat; tee -o ltp.txt ltp -noff; select -assert-none t:$_DLATCH*"]],
                      parse_synth),
    "style": ToolSpec("style", "python3", [sys.executable, "--version"],
                      lambda d, f, p: [[sys.executable, str(Path(__file__).with_name("style.py")), f["rtl"]]],
                      parse_style),
    "vivado_synth": ToolSpec("vivado_synth", "vivado", ["vivado", "-version"], _vivado_build(False),
                             _vivado_parse("synth"), timeout_s=3600),
    "vivado_impl": ToolSpec("vivado_impl", "vivado", ["vivado", "-version"], _vivado_build(True),
                            _vivado_parse("impl"), timeout_s=7200),
    "sta": ToolSpec("sta", "sta", ["sta", "-version"],
                    lambda d, f, p: [["sta", "-exit", p["sta_script"]]], parse_sta),   # wire to your liberty + SDC
}


class ToolRunner:
    def __init__(self, runner_id: str, key: bytes, ledger: EvidenceLedger, ws: Workspace):
        self.runner_id = runner_id
        self.__key = key            # never exposed to agents
        self.ledger = ledger
        self.ws = ws
        self.work = ws.state_dir / "runs"

    def run(self, kind: str, task_id: str, artifact_id: str, inputs: dict[str, str], params: dict) -> EvidenceRecord:
        """inputs: role-in-command -> repo-relative path, e.g. {"rtl": "rtl/sync_fifo.v"}."""
        spec = TOOLS[kind]
        eid = new_evidence_id()
        run_dir = self.work / eid
        used = self.ws.copy_into(list(inputs.values()), run_dir)
        files = {role: Path(name).name for role, name in inputs.items()}
        started = time.time()
        codes: list[int] = []
        commands: list[list[str]] = []
        version = _first_line(spec.version_cmd)
        if shutil.which(spec.tool) is None:
            status, summary = RunStatus.NOT_RUN, {"reason": f"tool '{spec.tool}' not installed on runner {self.runner_id}"}
            (run_dir / "not_run.txt").write_text(summary["reason"] + "\n")
        else:
            commands = spec.build(run_dir, files, params)
            for i, cmd in enumerate(commands):
                try:
                    p = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True, timeout=spec.timeout_s)
                    (run_dir / f"cmd{i}.stdout").write_text(p.stdout)
                    (run_dir / f"cmd{i}.stderr").write_text(p.stderr)
                    codes.append(p.returncode)
                    if p.returncode != 0:
                        break
                except subprocess.TimeoutExpired:
                    (run_dir / f"cmd{i}.stderr").write_text("TIMEOUT\n")
                    codes.append(-9)
                    break
            try:
                status, summary = spec.parse(run_dir, codes, params)
            except Exception as e:   # a parser bug is an ERROR, not a pass
                status, summary = RunStatus.ERROR, {"reason": f"parser exception {e!r}"}
        outputs = {p.name: sha256_file(p) for p in sorted(run_dir.iterdir()) if p.is_file()}
        rec = EvidenceRecord(
            evidence_id=eid, task_id=task_id, artifact_id=artifact_id, kind=kind,
            provenance={"runner_id": self.runner_id, "host": platform.node(), "python": platform.python_version()},
            tool=spec.tool, tool_version=version, command=commands, config=dict(params),
            input_hashes=used, output_hashes=outputs, exit_codes=codes, status=status, summary=summary,
            raw_location=str(run_dir),
            claim_type=ClaimType.TOOL_RESULT if status in (RunStatus.PASS, RunStatus.FAIL) else ClaimType.FAILED_EXECUTION,
            started_at=started, finished_at=time.time())
        with self.ledger.locked():
            rec.prev_hash = self.ledger.head
            rec.record_hash = sha256_bytes(rec.canonical())
            rec.signature = hmac.new(self.__key, rec.record_hash.encode(), hashlib.sha256).hexdigest()
            return self.ledger.append(rec)
