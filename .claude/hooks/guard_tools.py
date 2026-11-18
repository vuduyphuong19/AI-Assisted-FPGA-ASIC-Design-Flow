#!/usr/bin/env python3
"""PreToolUse hook: per-role write scopes, read barriers and Bash allow-lists.

Claude Code passes JSON on stdin with tool_name, tool_input, cwd and, inside a subagent (or
with --agent), agent_type. Exit 2 blocks the call and shows stderr to the agent.
This is a guardrail inside one machine, not a security boundary: production runs each
agent and the tool runner in separate containers with separate filesystem mounts.
"""
import fnmatch
import json
import os
import re
import sys

PATH_TOOLS_WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_TOOLS_READ = {"Read", "Grep", "Glob"}


def deny(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def match(rel: str, globs) -> bool:
    return any(fnmatch.fnmatch(rel, g) or rel == g.rstrip("/*") for g in globs)


def main() -> None:
    data = json.load(sys.stdin)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    policy = json.load(open(os.path.join(root, ".claude", "mae-policy.json")))
    # --agent main threads and subagents carry agent_type; MAE_ROLE (set by the human when launching
    # `claude`) is the fallback for a main thread whose payload lacks it.
    role = data.get("agent_type") or os.environ.get("MAE_ROLE") or policy["default_role"]
    rp = policy["roles"].get(role, {"write": [], "read_deny": ["**"], "bash_allow": []})
    tool, inp = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    g = policy["global"]
    if rp.get("full_access"):
        sys.exit(0)  # unrestricted role (human-supervised maintainer): no path, scope or Bash checks

    if tool in PATH_TOOLS_WRITE | PATH_TOOLS_READ:
        raw = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
        if not raw:
            if tool == "Grep" and rp.get("read_deny"):
                deny(f"[{role}] Grep without an explicit path would search files you may not read; pass `path`.")
            sys.exit(0)
        absp = os.path.realpath(os.path.join(data.get("cwd") or root, raw))
        rel = os.path.relpath(absp, os.path.realpath(root))
        if rel.startswith(".."):
            deny(f"[{role}] {raw} is outside the project.")
        if match(rel, g["no_access"]):
            deny(f"[{role}] {rel} is orchestrator state; use `python3 tools/mae.py ...` instead.")
        if tool in PATH_TOOLS_WRITE:
            globally_locked = match(rel, g["no_write"]) and not match(rel, rp.get("global_write_exempt", []))
            if globally_locked or not match(rel, rp.get("write", [])):
                deny(f"[{role}] may not write {rel}. Your write scope: {rp.get('write', [])}. "
                     "To change another role's artifact, report a finding.")
        elif match(rel, rp.get("read_deny", [])):
            deny(f"[{role}] may not read {rel}: independence barrier (derive your work from the spec and the evidence).")
        sys.exit(0)

    if tool == "Bash":
        cmd = " ".join((inp.get("command") or "").split())
        for pat in g["bash_deny"]:
            if re.search(pat, cmd):
                deny(f"[{role}] command blocked by policy ({pat}).")
        allow = rp.get("bash_allow") or policy["worker_bash_allow"]
        if not any(re.fullmatch(p, cmd) for p in allow):
            deny(f"[{role}] Bash is limited to: {allow}. Use the Read/Write/Edit tools for files.")
    sys.exit(0)


if __name__ == "__main__":
    main()
