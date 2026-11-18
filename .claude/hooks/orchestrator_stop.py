#!/usr/bin/env python3
"""Stop hook for the main (orchestrator) session.

1. If `mae next` still has ready actions, the orchestrator may not stop: it is told to continue.
2. If its final message claims success (signoff, all tests pass, timing met) while `mae next`
   does not report the flow complete, the stop is blocked and the claim must be corrected.
Stopping is allowed when only human decisions, escalations or running background subagents remain.
A counter caps consecutive blocks so a broken state cannot trap the session.
"""
import json
import os
import re
import subprocess
import sys

MAX_CONSECUTIVE_BLOCKS = 25
SUCCESS_CLAIM = re.compile(r"\b(signed[ -]?off|signoff (is )?complete|all (tests|checks|gates) pass(ed)?|timing (is )?met|tape[- ]?out ready)\b", re.I)


def main() -> None:
    data = json.load(sys.stdin)
    if (data.get("agent_type") or "ic-orchestrator") != "ic-orchestrator":
        sys.exit(0)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if not os.path.isfile(os.path.join(root, ".mae", "project.json")):
        sys.exit(0)
    counter = os.path.join(root, ".mae", "stop_blocks")
    r = subprocess.run([sys.executable, os.path.join(root, "tools", "mae.py"), "next"], cwd=root, capture_output=True, text=True)
    nxt = json.loads(r.stdout)
    msg = data.get("last_assistant_message") or ""
    reason = None
    if SUCCESS_CLAIM.search(msg) and not nxt["complete"]:
        reason = ("Your message claims success, but `mae next` says the flow is not complete "
                  f"(escalations={len(nxt['escalations'])}, waiting_for_human={[w['task'] for w in nxt['waiting_for_human']]}). "
                  "Report status only from `mae next` / `mae gate`.")
    elif nxt["ready"]:
        reason = "Ready actions remain: " + ", ".join(x["task"] for x in nxt["ready"]) + ". Continue the loop."
    n = int(open(counter).read()) if os.path.isfile(counter) else 0
    if reason and n < MAX_CONSECUTIVE_BLOCKS:
        open(counter, "w").write(str(n + 1))
        print(json.dumps({"decision": "block", "reason": reason}))
    elif os.path.isfile(counter):
        os.remove(counter)
    sys.exit(0)


if __name__ == "__main__":
    main()
