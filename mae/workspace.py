"""Project workspace: files in the repo are the artifacts; .mae/ holds state agents cannot touch.

Artifact ownership and read barriers are enforced by Claude Code hooks (.claude/hooks/guard_tools.py)
using .claude/mae-policy.json. This module only hashes, snapshots and restores files.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .evidence import sha256_bytes


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.state_dir = self.root / ".mae"
        for d in ("versions", "briefs", "active", "runs"):
            (self.state_dir / d).mkdir(parents=True, exist_ok=True)

    # -- artifacts --------------------------------------------------------------
    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def hash(self, rel: str) -> str | None:
        p = self.path(rel)
        return sha256_bytes(p.read_bytes()) if p.is_file() else None

    def read(self, rel: str) -> str:
        return self.path(rel).read_text()

    def snapshot(self, rel: str) -> str:
        data = self.path(rel).read_bytes()
        h = sha256_bytes(data)
        dst = self.state_dir / "versions" / h
        if not dst.exists():
            dst.write_bytes(data)
        return h

    def restore(self, rel: str, sha: str) -> None:
        shutil.copyfile(self.state_dir / "versions" / sha, self.path(rel))

    def copy_into(self, rels: list[str], dest: Path) -> dict[str, str]:
        dest.mkdir(parents=True, exist_ok=True)
        used = {}
        for rel in rels:
            if not self.exists(rel):
                raise FileNotFoundError(f"input artifact '{rel}' does not exist")
            shutil.copyfile(self.path(rel), dest / Path(rel).name)
            used[rel] = self.hash(rel)
        return used

    # -- state ------------------------------------------------------------------
    def load(self, name: str, default):
        p = self.state_dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def save(self, name: str, obj) -> None:
        p = self.state_dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
        os.replace(tmp, p)

    def append(self, name: str, obj) -> None:
        with (self.state_dir / name).open("a") as f:
            f.write(json.dumps(obj, sort_keys=True) + "\n")

    def read_lines(self, name: str) -> list[dict]:
        p = self.state_dir / name
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def runner_key(self) -> bytes:
        """Generated once, chmod 600. Hooks deny agents any access to .mae/.
        Production: the runner is a separate service/container and the key never touches this host."""
        p = self.state_dir / "runner.key"
        if not p.exists():
            p.write_bytes(os.urandom(32))
            p.chmod(0o600)
        return p.read_bytes()
