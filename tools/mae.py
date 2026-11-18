#!/usr/bin/env python3
"""Entry point: python3 tools/mae.py <command>. See mae/cli.py."""
import sys
from pathlib import Path

sys.path[0] = str(Path(__file__).resolve().parent.parent)   # replace tools/ so this file does not shadow the package
sys.dont_write_bytecode = True
from mae.cli import main  # noqa: E402

sys.exit(main())
