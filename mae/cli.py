"""`python3 tools/mae.py <command>`: the interface between Claude Code agents and project state.

Agent-callable:   status | next | brief | run | submit | gate | evidence | revert | report | audit | validate-report
Human-only:       approve   (denied to every agent by permissions.deny and the guard_tools hook)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .briefs import make_brief
from .flow import Project, approve


def _root() -> Path:
    p = Path.cwd()
    for c in [p, *p.parents]:
        if (c / ".claude").is_dir() and (c / "tools" / "mae.py").exists():
            return c
    return p


def cmd_init(args) -> dict:
    from .workspace import Workspace
    ws = Workspace(_root())
    spec = json.loads((ws.root / args.spec).read_text())
    top = spec["block"]
    fmax = (spec.get("ppa_targets") or {}).get("fmax_mhz")
    cfg = {"top": top, "props_top": f"{top}_props", "spec": args.spec, "seed": args.seed,
           "files": {"rtl": f"rtl/{top}.v", "tb": f"dv/tb_{top}.v", "props": f"formal/{top}_props.v",
                     "xdc": f"impl/{top}.xdc", "sta_script": "impl/sta.tcl"},
           "impl": {"tool": args.impl_tool, "part": args.part,
                    "period_ns": round(1000.0 / fmax, 3) if fmax else args.period_ns,
                    "clock_port": "clk", "logic_level_budget": args.logic_level_budget,
                    "directive": args.directive},
           "requirements": [r["id"] for r in spec["requirements"]], "coverage_bins": spec.get("coverage_bins", []),
           "limits": {"max_fix_attempts": args.max_fix_attempts, "max_no_progress": 2}}
    ws.save("project.json", cfg)
    ws.runner_key()
    return cfg


def cmd_report(p: Project) -> str:
    lines = [f"# Signoff report: {p.cfg['top']}", ""]
    lines += ["## Signoff criteria", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for n, ok, d in p.signoff_checks():
        lines.append(f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |")
    from .gates import latest_fresh
    import re
    sim = latest_fresh(p.ledger, p.ws, "sim", p.files["rtl"])
    frm = latest_fresh(p.ledger, p.ws, "formal", p.files["rtl"])
    spec = json.loads(p.ws.read(p.cfg["spec"]))
    props = p.ws.read(p.files["props"]) if p.ws.exists(p.files["props"]) else ""
    asserted = {m for line in props.splitlines() if re.search(r"\bassert\s*\(", line) for m in re.findall(r"REQ-\d+", line)}
    lines += ["", "## Traceability (requirement -> latest evidence)", "",
              "| Requirement | Simulation | Formal | Evidence |", "| --- | --- | --- | --- |"]
    for r in spec["requirements"]:
        s = sim.summary.get("requirements", {}).get(r["id"], {}) if sim else {}
        f_cov = "asserted" if frm and r["id"] in asserted else "-"
        lines.append(f"| {r['id']} | {s.get('result', 'NO EVIDENCE')} ({s.get('checks', 0)} checks) | "
                     f"{(frm.status.value + ' ' + f_cov) if frm and f_cov != '-' else '-'} | {sim.evidence_id if sim else '-'}"
                     f"{', ' + frm.evidence_id if frm and f_cov != '-' else ''} |")
    lines += ["", "## Debug iterations", "", "| # | Outcome | Failing before | Regressions |", "| --- | --- | --- | --- |"]
    for i in p.state["debug"]:
        lines.append(f"| {i['k']} | {i['status']} | {', '.join(i['failing'])} | {', '.join(i.get('regressions', [])) or '-'} |")
    lines += ["", "## Findings", "", "| ID | Sev | Artifact | Source | Text |", "| --- | --- | --- | --- | --- |"]
    for f in p.state["findings"]:
        lines.append(f"| {f['id']} | {f['severity']} | {f['artifact']} | {f['source']} | {f['text']} |")
    lines += ["", "## Evidence ledger", "", "| ID | Task | Kind | Status | Tool | Inputs |", "| --- | --- | --- | --- | --- | --- |"]
    for r in p.ledger.records():
        lines.append(f"| {r.evidence_id} | {r.task_id} | {r.kind} | {r.status.value} | {r.tool_version[:40]} | "
                     f"{', '.join(f'{k}@{v[:8]}' for k, v in r.input_hashes.items())} |")
    lines += ["", "## Human decisions", ""] + [f"- {d['decision']} by {d['signer']}: {d['reason']}" for d in p.decisions()]
    out = "\n".join(lines) + "\n"
    (p.ws.root / "reports" / "signoff_report.md").write_text(out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mae")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--spec", default="spec/requirements.json"); s.add_argument("--seed", type=int, default=7)
    s.add_argument("--max-fix-attempts", type=int, default=3)
    s.add_argument("--impl-tool", choices=("vivado", "yosys"), default="vivado",
                   help="back end: vivado (synth + place/route timing) or yosys (generic synth + OpenSTA)")
    s.add_argument("--part", default="xc7a35tcpg236-1", help="Vivado part")
    s.add_argument("--period-ns", type=float, default=10.0, help="clock period if the spec has no fmax target")
    s.add_argument("--logic-level-budget", type=int, default=12)
    s.add_argument("--directive", default="Default",
                   help="place_design/route_design -directive (human choice, e.g. Explore)")
    sub.add_parser("status"); sub.add_parser("next"); sub.add_parser("audit"); sub.add_parser("report")
    for c in ("brief", "run", "submit", "validate-report"):
        sub.add_parser(c).add_argument("task")
    s = sub.add_parser("gate"); s.add_argument("name", choices=["rtl_freeze", "synth_clean", "timing"])
    s = sub.add_parser("revert"); s.add_argument("iteration", type=int)
    s = sub.add_parser("evidence"); s.add_argument("id", nargs="?"); s.add_argument("--log", action="store_true")
    s = sub.add_parser("approve"); s.add_argument("decision"); s.add_argument("--signer", required=True)
    s.add_argument("--reason", required=True); s.add_argument("--amount", type=int, default=0)
    a = ap.parse_args(argv)

    def emit(obj, code=0):
        print(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))
        return code

    if a.cmd == "init":
        return emit(cmd_init(a))
    if a.cmd == "approve":
        return emit(approve(_root(), a.decision, a.signer, a.reason, a.amount))
    p = Project(_root())
    if a.cmd == "next":
        return emit(p.next())
    if a.cmd == "status":
        nx = p.next()
        return emit({"gates": nx["gates"], "ready": [r["task"] for r in nx["ready"]],
                     "in_progress": [r["task"] for r in nx["in_progress"]],
                     "waiting_for_human": [r["task"] for r in nx["waiting_for_human"]],
                     "escalations": nx["escalations"], "debug": [{k: i[k] for k in ("k", "status")} for i in p.state["debug"]],
                     "complete": nx["complete"]})
    if a.cmd == "brief":
        return emit(make_brief(p, a.task)[1])
    if a.cmd == "run":
        return emit(p.run_tool(a.task))
    if a.cmd == "submit":
        r = p.submit(a.task)
        return emit(r, 0 if r["accepted"] else 1)
    if a.cmd == "validate-report":
        errors, downgrades, _ = p.validate_report(a.task)
        return emit({"errors": errors, "downgrades": downgrades}, 1 if errors else 0)
    if a.cmd == "gate":
        r = p.gate(a.name)
        return emit({"gate": r.gate, "passed": r.passed, "waived_by": r.waived_by,
                     "checks": [vars(c) for c in r.checks]}, 0 if r.passed else 1)
    if a.cmd == "revert":
        return emit(p.revert(a.iteration))
    if a.cmd == "evidence":
        if not a.id:
            return emit([{"id": r.evidence_id, "task": r.task_id, "kind": r.kind, "status": r.status.value}
                         for r in p.ledger.records()])
        r = p.ledger.get(a.id)
        out = {"id": r.evidence_id, "kind": r.kind, "status": r.status.value, "tool": r.tool_version,
               "command": r.command, "inputs": r.input_hashes, "summary": r.summary,
               "verification_status": p.ledger.status_of(r.evidence_id).value}
        if a.log:
            run = Path(r.raw_location)
            out["log_tail"] = {f.name: f.read_text(errors="replace").splitlines()[-60:]
                               for f in sorted(run.glob("cmd*.std*")) if f.stat().st_size}
        return emit(out)
    if a.cmd == "audit":
        probs = p.ledger.audit()
        return emit({"clean": not probs, "problems": probs}, 0 if not probs else 1)
    if a.cmd == "report":
        return emit(cmd_report(p))
    return 2


if __name__ == "__main__":
    sys.exit(main())
