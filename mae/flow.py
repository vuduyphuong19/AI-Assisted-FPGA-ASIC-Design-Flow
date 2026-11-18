"""Workflow engine: the deterministic part of the orchestrator.

The Claude Code orchestrator agent never decides what is "done". It calls `mae next`, which
derives task status from files on disk, the evidence ledger, submitted reports and human
decisions, and returns the ready actions. The debug loop and its guards also live here.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import reports
from .evidence import EvidenceLedger, RunStatus
from .gates import (Gate, coverage_bins_hit, every_requirement_verified, is_fresh, latest_fresh, tool_passed)
from .runner import ToolRunner
from .workspace import Workspace

OWNER = {"rtl": "rtl-designer", "tb": "dv-engineer", "props": "formal-engineer", "spec": "human"}


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


@dataclass
class Task:
    id: str
    type: str                       # human | agent | tool | gate | signoff
    needs: list[str] = field(default_factory=list)
    agent: str = ""
    outputs: list[str] = field(default_factory=list)   # keys into config.files
    kind: str = ""
    inputs: dict = field(default_factory=dict)          # role -> key into config.files
    goal: str = ""


class Project:
    def __init__(self, root: Path):
        self.ws = Workspace(root)
        self.cfg = self.ws.load("project.json", None)
        if self.cfg is None:
            raise SystemExit("not initialised: run `python3 tools/mae.py init` first")
        self.ledger = EvidenceLedger(self.ws.state_dir / "ledger.jsonl", {"runner-local": self.ws.runner_key()})
        self.state = self.ws.load("state.json", {"submissions": {}, "debug": [], "findings": []})
        self.files = self.cfg["files"]
        self.tasks = self._template()

    # ----------------------------------------------------------------------- template
    def vivado(self) -> bool:
        return self.cfg.get("impl", {}).get("tool") == "vivado"

    def impl_task(self) -> str:
        return "impl" if self.vivado() else "sta"

    def _template(self) -> list[Task]:
        top = self.cfg["top"]
        if self.vivado():
            back_end = [
                Task("synth", "tool", ["rtl_freeze", "constraints"], kind="vivado_synth", inputs={"rtl": "rtl", "xdc": "xdc"}),
                Task("impl", "tool", ["synth"], kind="vivado_impl", inputs={"rtl": "rtl", "xdc": "xdc"}),
            ]
            front = [Task("constraints", "agent", ["spec_baseline"], agent="impl-engineer", outputs=["xdc"],
                          goal=f"Write out-of-context Vivado constraints for `{top}` from the spec's timing targets.")]
        else:
            back_end = [
                Task("synth", "tool", ["rtl_freeze"], kind="synth", inputs={"rtl": "rtl"}),
                Task("sta", "tool", ["synth"], kind="sta", inputs={"rtl": "rtl"}),
            ]
            front = []
        return [
            Task("spec_baseline", "human", goal="Human approves the requirements baseline"),
            Task("rtl", "agent", ["spec_baseline"], agent="rtl-designer", outputs=["rtl"],
                 goal=f"Implement synthesizable RTL for module `{top}` that satisfies every requirement."),
            Task("tb", "agent", ["spec_baseline"], agent="dv-engineer", outputs=["tb"],
                 goal=f"Write a self-checking testbench for `{top}` derived from the spec only."),
            Task("props", "agent", ["spec_baseline"], agent="formal-engineer", outputs=["props"],
                 goal=f"Write formal properties for `{top}` derived from the spec only."),
            *front,
            Task("style", "tool", ["rtl"], kind="style", inputs={"rtl": "rtl"}),
            Task("lint", "tool", ["rtl"], kind="lint", inputs={"rtl": "rtl"}),
            Task("sim", "tool", ["rtl", "tb"], kind="sim", inputs={"rtl": "rtl", "tb": "tb"}),
            Task("formal", "tool", ["rtl", "props"], kind="formal", inputs={"rtl": "rtl", "props": "props"}),
            Task("rtl_freeze", "gate", ["style", "lint", "sim", "formal"]),
            *back_end,
            Task("review", "agent", ["synth", self.impl_task()], agent="design-reviewer", outputs=[],
                 goal="Independently and adversarially review every artifact, claim and piece of evidence."),
            Task("signoff", "signoff", ["review"]),
        ]

    def task(self, tid: str) -> Task:
        for t in self.tasks:
            if t.id == tid:
                return t
        m = re.fullmatch(r"(triage|fix)-(\d+)", tid)
        if m:
            it = self._iteration(int(m.group(2)))
            if m.group(1) == "triage":
                return Task(tid, "agent", agent="debug-triage", outputs=[], goal="Identify the faulty artifact")
            key = it["suspect"]
            return Task(tid, "agent", agent=OWNER[key], outputs=[key], goal=f"Fix {self.files[key]}")
        raise KeyError(f"unknown task {tid}")

    def save(self) -> None:
        self.ws.save("state.json", self.state)

    # ----------------------------------------------------------------------- gates
    def gate(self, name: str):
        rtl = self.files["rtl"]
        if name == "rtl_freeze":
            g = Gate("rtl_freeze", [tool_passed("style", rtl), tool_passed("lint", rtl), every_requirement_verified(self.cfg["requirements"], rtl),
                                    coverage_bins_hit(self.cfg["coverage_bins"], rtl), tool_passed("formal", rtl)])
        elif name == "synth_clean":
            g = Gate("synth_clean", [tool_passed("vivado_synth" if self.vivado() else "synth", rtl)])
        elif name == "timing":
            g = Gate("timing", [tool_passed("vivado_impl" if self.vivado() else "sta", rtl)], waivable=True)
        else:
            raise KeyError(name)
        return g.evaluate(self.ledger, self.ws, self.waivers())

    # ----------------------------------------------------------------------- decisions
    def decisions(self) -> list[dict]:
        return self.ws.read_lines("decisions.jsonl")

    def decided(self, name: str) -> dict | None:
        spec_hash = self.ws.hash(self.cfg["spec"])
        for d in reversed(self.decisions()):
            if d["decision"] == name and d.get("spec_hash") in (None, spec_hash):
                return d
        return None

    def waivers(self) -> dict[str, str]:
        return {d["decision"][len("waive:"):]: d["signer"] for d in self.decisions() if d["decision"].startswith("waive:")}

    def budget(self) -> int:
        extra = sum(int(d.get("amount", 0)) for d in self.decisions() if d["decision"] == "extend_budget")
        return self.cfg["limits"]["max_fix_attempts"] + extra

    # ----------------------------------------------------------------------- status
    def submitted_hashes(self, key: str) -> set[str]:
        rel = self.files[key]
        return {s["outputs"][rel] for s in self.state["submissions"].values() if rel in s.get("outputs", {})}

    def agent_done(self, t: Task) -> bool:
        if not t.outputs:
            return t.id in self.state["submissions"]
        return all(self.ws.hash(self.files[k]) in self.submitted_hashes(k) for k in t.outputs)

    def tool_evidence(self, t: Task):
        rec = latest_fresh(self.ledger, self.ws, t.kind, self.files["rtl"])
        return rec if (rec and is_fresh(rec, self.ws)[0]) else None

    def _iteration(self, k: int) -> dict:
        return self.state["debug"][k - 1]

    def _sim_snapshot(self) -> dict:
        rec = latest_fresh(self.ledger, self.ws, "sim", self.files["rtl"])
        frm = latest_fresh(self.ledger, self.ws, "formal", self.files["rtl"])
        snap = {r: v["result"] for r, v in (rec.summary.get("requirements", {}) if rec else {}).items()}
        snap["formal"] = frm.status.value if frm else "NONE"
        return snap

    # ----------------------------------------------------------------------- next
    def next(self) -> dict:
        out = {"ready": [], "in_progress": [], "waiting_for_human": [], "escalations": [], "gates": {}, "complete": False}
        done: set[str] = set()
        for t in self.tasks:
            if not all(n in done for n in t.needs):
                continue
            if t.type == "human":
                if self.decided("spec_baseline"):
                    done.add(t.id)
                else:
                    out["waiting_for_human"].append({"task": t.id, "human_command": "python3 tools/mae.py approve spec_baseline --signer <name> --reason <why>"})
            elif t.type == "agent":
                if self.agent_done(t):
                    done.add(t.id)
                else:
                    self._agent_action(t, out)
            elif t.type == "tool":
                rec = self.tool_evidence(t)
                if rec:
                    done.add(t.id)
                    if t.id in ("synth", "impl") and rec.status is not RunStatus.PASS:
                        why = rec.summary.get("reason", rec.summary) if isinstance(rec.summary, dict) else rec.summary
                        out["escalations"].append({"task": t.id, "evidence": rec.evidence_id,
                                                   "why": f"{t.kind} {rec.status.value}: {str(why)[:300]}",
                                                   "human_options": ["route to rtl-designer (micro-architecture/pipelining)",
                                                                     "route to impl-engineer (constraints, with justification)",
                                                                     "waive timing at signoff"]})
                else:
                    out["ready"].append({"task": t.id, "action": "run", "command": f"python3 tools/mae.py run {t.id}"})
            elif t.type == "gate":
                res = self.gate(t.id)
                out["gates"][t.id] = {"passed": res.passed, "failing": [f"{c.name}: {c.detail}" for c in res.failures]}
                if res.passed:
                    done.add(t.id)
                    self._close_iteration_if_fixed()
                else:
                    self._debug_step(res, out)
            elif t.type == "signoff":
                self._signoff(out)
        self.save()
        return out

    def _agent_action(self, t: Task, out: dict) -> None:
        disp = self.state.setdefault("dispatched", {}).get(t.id)
        if disp:
            out["in_progress"].append({"task": t.id, "agent": t.agent,
                                       "when_finished": f"python3 tools/mae.py submit {t.id}"})
        else:
            out["ready"].append({"task": t.id, "action": "spawn", "agent": t.agent,
                                 "brief_command": f"python3 tools/mae.py brief {t.id}"})

    # ----------------------------------------------------------------------- debug loop
    def _debug_step(self, res, out: dict) -> None:
        lim = self.cfg["limits"]
        failing = sorted(c.name for c in res.failures)
        sig = sha8("|".join(failing))
        its = self.state["debug"]
        cur = its[-1] if its else None
        # 1. a fix was submitted and the tools have re-run: judge it
        if cur and cur["status"] == "verifying":
            now = self._sim_snapshot()
            regress = [r for r, v in cur["before"].items() if v == "PASS" and now.get(r) not in ("PASS", None)]
            if regress:
                cur.update(status="regressed", regressions=regress, after=now)
            elif sig == cur["sig"]:
                cur.update(status="no_progress", after=now)
            else:
                cur.update(status="progressed", after=now)
        if cur and cur["status"] == "regressed":
            out["ready"].append({"task": f"revert-{cur['k']}", "action": "revert",
                                 "command": f"python3 tools/mae.py revert {cur['k']}",
                                 "why": f"fix {cur['k']} broke previously passing checks {cur['regressions']}"})
            return
        # 2. iteration in flight
        if cur and cur["status"] in ("triage", "fix"):
            t = self.task(f"{cur['status']}-{cur['k']}")
            if cur["status"] == "triage" and t.id in self.state["submissions"]:
                suspect = self.state["submissions"][t.id]["report"].get("suspect_artifact")
                if suspect == "spec":
                    out["escalations"].append({"task": t.id, "why": "triage suspects the specification: human must clarify",
                                               "hypothesis": self.state["submissions"][t.id]["report"].get("claims")})
                    return
                cur.update(status="fix", suspect=suspect)
                t = self.task(f"fix-{cur['k']}")
            self._agent_action(t, out)
            return
        # 3. start a new iteration, if the guards allow
        attempts = sum(1 for i in its if i["status"] not in ("triage", "fix"))
        streak = 0
        for i in reversed(its):
            if i["status"] in ("no_progress", "duplicate"):
                streak += 1
            else:
                break
        if attempts >= self.budget():
            out["escalations"].append({"task": "rtl_freeze", "why": f"fix budget exhausted ({attempts}/{self.budget()})",
                                       "human_options": ["python3 tools/mae.py approve extend_budget --amount 2 ...",
                                                         "change the spec or architecture"], "failing": failing})
            return
        if streak >= lim["max_no_progress"]:
            out["escalations"].append({"task": "rtl_freeze", "why": f"{streak} consecutive fixes made no progress on the same failure",
                                       "failing": failing})
            return
        k = len(its) + 1
        its.append({"k": k, "status": "triage", "sig": sig, "failing": failing, "before": self._sim_snapshot(),
                    "start_hashes": {key: self.ws.hash(self.files[key]) for key in ("rtl", "tb", "props")},
                    "started": time.time()})
        self._agent_action(self.task(f"triage-{k}"), out)

    def _close_iteration_if_fixed(self) -> None:
        its = self.state["debug"]
        if its and its[-1]["status"] == "verifying":
            its[-1].update(status="fixed", after=self._sim_snapshot())

    def revert(self, k: int) -> str:
        it = self._iteration(k)
        if it["status"] != "regressed":
            raise SystemExit(f"iteration {k} is '{it['status']}', only regressed fixes are reverted")
        key = it["suspect"]
        self.ws.restore(self.files[key], it["start_hashes"][key])
        it["status"] = "reverted"
        self.save()
        return f"restored {self.files[key]} to {it['start_hashes'][key][:12]} (pre-fix version)"

    # ----------------------------------------------------------------------- signoff
    def signoff_checks(self) -> list[tuple[str, bool, str]]:
        checks = []
        for g in ("rtl_freeze", "synth_clean", "timing"):
            r = self.gate(g)
            detail = "waived by " + r.waived_by if r.waived_by else ("; ".join(f"{c.name}: {c.detail}" for c in r.failures) or "pass")
            checks.append((g, r.passed, detail))
        waived = self.waivers()
        open_sev1 = [f for f in self.state["findings"] if f["severity"] == 1 and f"finding:{f['id']}" not in waived]
        submitted = "review" in self.state["submissions"]
        checks.append(("review", submitted and not open_sev1,
                       "independent review not submitted" if not submitted else
                       ("no open severity-1 findings" if not open_sev1 else f"open sev-1: {[f['id'] for f in open_sev1]}")))
        stub = []
        for kind in ("style", "lint", "sim", "formal", "synth", "sta", "vivado_synth", "vivado_impl"):
            rec = latest_fresh(self.ledger, self.ws, kind, self.files["rtl"])
            if rec and "FAKE" in (rec.tool_version or ""):
                stub.append(f"{kind}:{rec.evidence_id}")
        checks.append(("real_tools", not stub, "all evidence from real tools" if not stub else
                       f"evidence from a stub tool cannot sign off: {stub}"))
        problems = self.ledger.audit()
        checks.append(("ledger_audit", not problems, "clean" if not problems else "; ".join(problems)))
        return checks

    def _signoff(self, out: dict) -> None:
        checks = self.signoff_checks()
        failing = [(n, d) for n, ok, d in checks if not ok]
        if failing:
            out["escalations"].append({"task": "signoff", "why": "signoff criteria not met",
                                       "failing": [f"{n}: {d}" for n, d in failing],
                                       "human_options": ["provide the missing evidence",
                                                         "python3 tools/mae.py approve waive:<gate-or-finding:ID> --signer <name> --reason <why>"]})
        elif not self.decided("signoff"):
            out["waiting_for_human"].append({"task": "signoff", "human_command": "python3 tools/mae.py approve signoff --signer <name> --reason <why>"})
        else:
            out["complete"] = True

    # ----------------------------------------------------------------------- run / submit / approve
    def run_tool(self, tid: str) -> dict:
        t = self.task(tid)
        if t.type != "tool":
            raise SystemExit(f"{tid} is not a tool task")
        runner = ToolRunner("runner-local", self.ws.runner_key(), self.ledger, self.ws)
        params = {"top": self.cfg["top"], "seed": self.cfg.get("seed", 1)}
        if t.kind == "formal":
            params["top"] = self.cfg["props_top"]
        if t.kind == "sta":
            params["sta_script"] = self.files.get("sta_script", "impl/sta.tcl")
        if t.kind.startswith("vivado_"):
            imp = self.cfg["impl"]
            params.update(part=imp["part"], period_ns=imp["period_ns"], logic_level_budget=imp.get("logic_level_budget", 12),
                          directive=imp.get("directive", "Default"))
        rec = runner.run(t.kind, tid, self.files["rtl"], {r: self.files[k] for r, k in t.inputs.items()}, params)
        return {"evidence_id": rec.evidence_id, "status": rec.status.value, "tool": rec.tool_version,
                "summary": rec.summary, "raw": str(Path(rec.raw_location).relative_to(self.ws.root))}

    def expected_outputs(self, t: Task) -> list[str]:
        return [self.files[k] for k in t.outputs]

    def validate_report(self, tid: str):
        t = self.task(tid)
        need = ("suspect_artifact",) if tid.startswith("triage") else (("findings",) if tid == "review" else ())
        return reports.validate(self.ws.root / "reports" / f"{tid}.json", tid, t.agent, self.expected_outputs(t),
                                self.ws.root, self.ledger, need)

    def submit(self, tid: str) -> dict:
        t = self.task(tid)
        errors, downgrades, rep = self.validate_report(tid)
        if errors:
            return {"accepted": False, "errors": errors}
        if tid.startswith("triage") and rep["suspect_artifact"] not in OWNER:
            return {"accepted": False, "errors": [f"suspect_artifact must be one of {sorted(OWNER)}"]}
        outputs = {}
        for key in t.outputs:
            rel = self.files[key]
            h = self.ws.hash(rel)
            if tid.startswith("fix-") and h in self.submitted_hashes(key):
                it = self._iteration(int(tid.split("-")[1]))
                it.update(status="duplicate", fix_hash=h)
                self.ws.restore(rel, it["start_hashes"][key])
                self.save()
                return {"accepted": False, "errors": [f"{rel} is identical to a version already tried ({h[:12]}): "
                                                      "rejected as a no-op fix; counts against the fix budget"]}
            outputs[rel] = self.ws.snapshot(rel)
        self.state["submissions"][tid] = {"agent": t.agent, "outputs": outputs, "report": rep, "downgrades": downgrades, "at": time.time()}
        self.state.get("dispatched", {}).pop(tid, None)
        for d in downgrades:
            self._finding(3, tid, f"{t.agent} claim downgraded ({d['why']}): \"{d['claim'][:120]}\"", source="mae-submit")
        for f in rep.get("findings", []) or []:
            self._finding(f["severity"], f["artifact"], f["text"], source=t.agent, refs=f.get("evidence_refs", []))
        if tid.startswith("fix-"):
            self._iteration(int(tid.split("-")[1])).update(status="verifying", fix_hash=list(outputs.values())[0])
        self.save()
        return {"accepted": True, "downgraded_claims": downgrades, "outputs": outputs}

    def _finding(self, sev: int, artifact: str, text: str, source: str, refs=()) -> None:
        fid = f"F{len(self.state['findings']) + 1:03d}"
        self.state["findings"].append({"id": fid, "severity": sev, "artifact": artifact, "text": text,
                                       "source": source, "evidence_refs": list(refs)})


def approve(root: Path, decision: str, signer: str, reason: str, amount: int = 0) -> dict:
    ws = Workspace(root)
    cfg = ws.load("project.json", {})
    rec = {"decision": decision, "signer": signer, "reason": reason, "at": time.time()}
    if decision in ("spec_baseline", "signoff"):
        rec["spec_hash"] = ws.hash(cfg["spec"])
    if decision == "extend_budget":
        rec["amount"] = amount
    ws.append("decisions.jsonl", rec)
    return rec
