"""Deterministic task briefs: the ONLY prompt the orchestrator may pass to a subagent.

The orchestrator agent runs `mae brief <task>` and passes the output verbatim as the Agent tool
prompt. A PreToolUse hook on the Agent tool checks the BRIEF-ID, so the orchestrator cannot leak
the RTL designer's reasoning to verification agents, or add its own interpretation of results.
"""
from __future__ import annotations

import json

from .flow import OWNER, Project, sha8
from .gates import latest_fresh

REPORT_SCHEMA = """{
  "task": "<task id>", "agent": "<your agent name>",
  "outputs": [<exactly the files listed under Deliverables>],
  "claims": [{"text": "...", "type": "hypothesis|assumption|estimate|unverified|tool_result",
              "evidence_refs": ["EV-..."]}],          // evidence_refs required for tool_result
  "findings": [{"severity": 1|2|3, "artifact": "<path>", "text": "...", "evidence_refs": []}],   // optional, required for review
  "suspect_artifact": "rtl|tb|props|spec"              // triage only
}"""

TB_CONTRACT = """The runner parses ONLY these stdout lines, so the testbench must print them:
  REQ_RESULT <REQ-ID> <PASS|FAIL> checks=<n> errors=<n>     (one per requirement; checks=0 is treated as vacuous = FAIL)
  COVER <bin_name> <hits>                                   (one per coverage bin in the spec)
  CHECK_FAIL <free text>                                    (first few failures, for debugging)
  SUMMARY <PASS|FAIL>                                       (last line; missing = incomplete run = ERROR)
It must read the seed with $value$plusargs("seed=%d", seed) and call $finish."""

FORMAL_CONTRACT = """Module name must be `{props_top}`; its ports are the DUT inputs; instantiate the DUT inside it.
Use immediate `assert`/`assume` in always blocks (Yosys read_verilog -formal, k-induction via `sat -tempinduct`).
Every `assume` must be listed as an "assumption" claim in your report, with the requirement that justifies it."""


XDC_CONTRACT = """Write {xdc}: out-of-context constraints read BEFORE synth_design by the runner's own Tcl
(you do not write Tcl; `python3 tools/mae.py run synth|impl` builds and runs it).
  - order: clocks -> I/O -> exceptions
  - one create_clock per clock port, period exactly {period} ns ({part}); name the clock after the port
    (the runner fails any other period)
  - optional OOC placement hint: set_property HD.CLK_SRC <site> on each clock port, where <site> is a
    global-buffer site that exists on {part} (7-series: BUFGCTRL_X0Y0; UltraScale/UltraScale+ have no
    X0Y0 site, e.g. xczu7ev starts at BUFGCE_X0Y96). A site that does not exist is a failed XDC command
    and the runner fails the run; if unsure, omit the line and record an "assumption" claim (ideal clock)
  - no set_false_path / set_multicycle_path / set_max_delay / set_min_delay / set_clock_groups unless
    the spec documents the behavior; each carries "# REQ-<id>" on its line or the line above (the runner
    fails it otherwise) and is an "assumption" claim citing the requirement
  - no I/O delays unless the spec gives external timing (TIMING-18 in OOC runs is expected)"""


def _evidence_digest(p: Project) -> str:
    rows = []
    for kind in ("style", "lint", "sim", "formal", "synth", "sta", "vivado_synth", "vivado_impl"):
        rec = latest_fresh(p.ledger, p.ws, kind, p.files["rtl"])
        if rec:
            s = dict(rec.summary)
            rows.append(f"- {rec.evidence_id} [{kind}] status={rec.status.value} summary={json.dumps(s)[:900]}")
    return "\n".join(rows) or "- (no evidence yet)"


def make_brief(p: Project, tid: str) -> tuple[str, str]:
    t = p.task(tid)
    spec = p.ws.read(p.cfg["spec"])
    deliver = p.expected_outputs(t)
    ctx: list[str] = []
    rules = [
        "You cannot state that anything passes, is proven, meets timing or has a given size unless you cite an evidence ID "
        "printed by `python3 tools/mae.py run <task>`; such claims must be type tool_result with evidence_refs.",
        "You may write only the files under Deliverables plus your report. Hooks will block anything else.",
        "If the spec is ambiguous, do not resolve it silently: record an 'assumption' claim.",
    ]
    if t.agent == "rtl-designer":
        ctx.append("Coding rules: follow the rtl-coding skill (Verilog-2005, synchronous reset, _ff registers). "
                   "The rtl_freeze gate requires `style` (mae/style.py hard rules) and `lint` (verilator -Wall) to PASS.")
        rules.append("You may self-check with `python3 tools/mae.py run style` and `python3 tools/mae.py run lint`. You cannot "
                     "read the testbench or properties; debug from the failure evidence below.")
    if t.agent == "dv-engineer":
        ctx.append(TB_CONTRACT)
        ctx.append("Testbench method: follow the dv-testbench skill (reference model from the spec, per-cycle compare, "
                   "CHECK_FAIL with cycle/expected/actual, all six stimulus categories, every coverage bin).")
        rules.append("Derive every check from the requirements text. You cannot read rtl/: that is deliberate. "
                     "Build an independent reference model.")
    if t.agent == "formal-engineer":
        ctx.append(FORMAL_CONTRACT.format(props_top=p.cfg["props_top"]))
        rules.append("Derive properties from the requirements text. You cannot read rtl/.")
    if tid.startswith(("fix-", "triage-")):
        k = int(tid.split("-")[1])
        it = p.state["debug"][k - 1]
        ctx.append("Failing gate checks (rtl_freeze):\n" + "\n".join(f"- {f}" for f in it["failing"]))
        ctx.append("Current evidence (inspect raw logs with `python3 tools/mae.py evidence <EV-ID> --log`):\n" + _evidence_digest(p))
        tri = p.state["submissions"].get(f"triage-{k}")
        if tri:
            ctx.append("Triage hypothesis (a hypothesis, not a fact):\n" + json.dumps(tri["report"].get("claims"), indent=1))
        prior = [i for i in p.state["debug"] if i["k"] < k]
        if prior:
            ctx.append("Previous fix attempts (do not repeat them):\n" + "\n".join(
                f"- iteration {i['k']}: outcome={i['status']} regressions={i.get('regressions', [])} failing_before={i['failing']}"
                for i in prior))
    if t.agent == "impl-engineer":
        imp = p.cfg.get("impl", {})
        ctx.append(XDC_CONTRACT.format(xdc=p.files.get("xdc", "impl/<top>.xdc"), period=imp.get("period_ns"),
                                       part=imp.get("part")))
        ctx.append("Method: follow the vivado-flow skill.")
    if t.agent == "debug-triage":
        ctx.append("Method: follow the failure-triage skill (symptom class from values, first divergence, "
                   "sim-vs-formal cross-check, bug-pattern table).")
        ctx.append("Artifacts and owners:\n" + "\n".join(f"- {k}: {p.files[k]} (owner {OWNER[k]})" for k in ("rtl", "tb", "props"))
                   + "\n- spec: " + p.cfg["spec"] + " (owner human)")
        rules.append("Decide which artifact is most likely wrong: the RTL, the testbench, the properties, or the spec. "
                     "Check whether simulation and formal fail on the same requirement before blaming the RTL. "
                     "Report suspect_artifact and a hypothesis claim citing evidence IDs.")
    if t.agent == "design-reviewer":
        subs = {k: {"agent": v["agent"], "claims": v["report"].get("claims"), "downgrades": v["downgrades"]}
                for k, v in p.state["submissions"].items()}
        ctx.append("Evidence:\n" + _evidence_digest(p))
        ctx.append("Agent reports (claims are NOT evidence):\n" + json.dumps(subs, indent=1)[:6000])
        ctx.append("Debug history:\n" + json.dumps([{k: i[k] for k in ("k", "status", "failing")} for i in p.state["debug"]], indent=1))
        rules.append("Assume the work is wrong until evidence shows otherwise. Look for: requirements without a real check, "
                     "vacuous or over-constrained properties, claims without evidence, spec gaps, missing implementation evidence. "
                     "Severity 1 blocks signoff. Every finding must name an artifact and, where possible, an evidence ID or REQ ID.")
    body = "\n".join([
        f"ROLE: {t.agent}    TASK: {tid}",
        "## Goal", t.goal or f"Complete {tid}",
        "## Deliverables",
        *([f"- write {d}" for d in deliver] or ["- (no artifact; report only)"]),
        f"- then write reports/{tid}.json with this schema:", REPORT_SCHEMA,
        "## Specification (" + p.cfg["spec"] + ")", "```json", spec.strip(), "```",
        "## Context", *ctx,
        "## Rules", *[f"{i + 1}. {r}" for i, r in enumerate(rules)],
    ])
    bid = f"{tid}-{sha8(body)}"
    text = f"BRIEF-ID: {bid}\n{body}\n"
    p.ws.path(f".mae/briefs/{bid}.md").write_text(text)
    p.ws.save(f"active/{t.agent}.json", {"task": tid, "brief_id": bid, "blocks": 0})
    p.state.setdefault("dispatched", {})[tid] = {"agent": t.agent, "brief_id": bid}
    p.save()
    return bid, text
