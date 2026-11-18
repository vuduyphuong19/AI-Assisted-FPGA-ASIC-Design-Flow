#!/usr/bin/env python3
"""SubagentStop hook: a subagent may not finish until its structured report is valid.

Runs `mae validate-report <task>` for the task this agent type was dispatched on. On errors it
returns {"decision": "block", "reason": ...} so the subagent keeps working and fixes the report.
After 3 blocks it lets the subagent stop; `mae submit` then rejects the work and the
orchestrator re-dispatches it, so a confused agent cannot loop forever.
"""
import json
import os
import subprocess
import sys

MAX_BLOCKS = 3


def main() -> None:
    data = json.load(sys.stdin)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    agent = data.get("agent_type")
    active_path = os.path.join(root, ".mae", "active", f"{agent}.json")
    if not agent or not os.path.isfile(active_path):
        sys.exit(0)
    active = json.load(open(active_path))
    r = subprocess.run([sys.executable, os.path.join(root, "tools", "mae.py"), "validate-report", active["task"]],
                       cwd=root, capture_output=True, text=True)
    try:
        result = json.loads(r.stdout)
    except json.JSONDecodeError:
        result = {"errors": [r.stderr.strip()[-500:] or "validate-report failed"]}
    if not result.get("errors"):
        sys.exit(0)
    if active.get("blocks", 0) >= MAX_BLOCKS:
        sys.exit(0)
    active["blocks"] = active.get("blocks", 0) + 1
    json.dump(active, open(active_path, "w"))
    print(json.dumps({"decision": "block",
                      "reason": f"Report for task {active['task']} is not acceptable yet: " + "; ".join(result["errors"])
                                + ". Fix it, then finish."}))
    sys.exit(0)


if __name__ == "__main__":
    main()
