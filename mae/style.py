#!/usr/bin/env python3
"""mae.style - hard-rule checker for synthesizable Verilog-2005 RTL (runner kind `style`).

Deterministic part of the `rtl-coding` skill: the rules a reviewer would otherwise check
by eye. Rule ideas distilled from rtl-skills' CodingStyle (Apache-2.0) and VeriFlow-CC bug
lessons; code written for this project. Reset polarity is fixed by the spec, so only
*synchronous* reset is enforced here. It is not a full Verilog parser: it strips comments
and strings, then pattern-matches. Formatting/alignment is out of scope
(use verible-verilog-format).

Usage:
    python3 -m mae.style rtl/<top>.v
    python3 mae/style.py --list-rules

Waive one rule on one line (always say why):
    some_code;   // mae-style: allow R07 memory array, not a flop

Exit status: 0 = no findings, 1 = findings, 2 = usage / file error.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

RULES = {
    "R01": "`default_nettype none` must be the first directive and `default_nettype wire` the last",
    "R02": "SystemVerilog construct in Verilog-2005 RTL",
    "R03": "`casex` is banned (use case / casez with ? in patterns only)",
    "R04": "#delay in synthesizable RTL",
    "R05": "declaration initializer on a reg (reset is the only initialization)",
    "R06": "leftover //@ design-direction marker",
    "R07": "register written in a clocked block must carry the _ff suffix",
    "R08": "_ff suffix on a signal that is not a clocked register",
    "R09": "asynchronous reset/set: edge on a non-clock signal in a sensitivity list (use synchronous reset)",
    "R11": "positional port connection (named connections only)",
    "R12": "instance name must start with u_",
    "R13": "variable divide/modulo outside parameter/localparam (use shift/slice/redesign)",
    "R14": "`output reg` (use output wire + internal _ff + assign)",
    "R15": "blocking assignment (=) in a clocked block, or <= in a combinational block",
    "R16": "synchronizer flop (*_meta_ff / *_sync_ff) without (* ASYNC_REG = \"TRUE\" *)",
}

KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "integer",
    "real", "time", "parameter", "localparam", "assign", "always", "initial",
    "begin", "end", "if", "else", "case", "casez", "casex", "endcase", "for",
    "while", "repeat", "forever", "generate", "endgenerate", "genvar", "function",
    "endfunction", "task", "endtask", "default", "posedge", "negedge", "or",
    "and", "not", "signed", "unsigned", "defparam", "specify", "endspecify",
    "supply0", "supply1", "tri", "wand", "wor", "event", "fork", "join",
    "disable", "wait", "force", "release", "deassign", "automatic",
}

SV_PATTERNS = [
    (r"\blogic\b", "logic"),
    (r"\bbit\b", "bit"),
    (r"\balways_ff\b", "always_ff"),
    (r"\balways_comb\b", "always_comb"),
    (r"\balways_latch\b", "always_latch"),
    (r"\btypedef\b", "typedef"),
    (r"\bstruct\b", "struct"),
    (r"\benum\b", "enum"),
    (r"\binterface\b", "interface"),
    (r"\bpackage\b", "package"),
    (r"\bunique\b", "unique"),
    (r"\bpriority\b", "priority"),
    (r"\bassert\b", "assert"),
    (r"\.\*", ".* port connection"),
    (r"(?<![\w'])'[01xzXZ](?![\w])", "unsized fill literal '0/'1"),
]

WAIVE_RE = re.compile(r"mae-style:\s*allow\s+((?:R\d\d[ ,]*)+)")


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    msg: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.msg}"


def strip_comments_strings(text: str) -> str:
    """Blank out comments and string literals, keeping offsets and newlines."""
    out = list(text)
    i, n = 0, len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            blank(i, min(j + 1, n))
            i = j + 1
        else:
            i += 1
    return "".join(out)


def match_paren(s: str, i: int) -> int:
    """s[i] == '('; return index just after the matching ')'."""
    depth = 0
    for k in range(i, len(s)):
        if s[k] == "(":
            depth += 1
        elif s[k] == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(s)


TOKEN_RE = re.compile(r"\s*([A-Za-z_][\w$]*|\S)")


def next_token(s: str, i: int) -> tuple[str, int, int]:
    m = TOKEN_RE.match(s, i)
    if not m:
        return "", len(s), len(s)
    return m.group(1), m.start(1), m.end(1)


def consume_statement(s: str, i: int) -> int:
    """Return the index just after one Verilog statement starting at/after i."""
    tok, a, b = next_token(s, i)
    if tok == "begin":
        depth, k = 1, b
        while depth and k < len(s):
            t, ta, tb = next_token(s, k)
            if not t:
                return len(s)
            if t in ("begin", "case", "casez", "casex"):
                depth += 1
            elif t in ("end", "endcase"):
                depth -= 1
            k = tb
        # optional ": label"
        return k
    if tok == "if":
        t, ta, tb = next_token(s, b)
        k = match_paren(s, ta) if t == "(" else tb
        k = consume_statement(s, k)
        t, ta, tb = next_token(s, k)
        if t == "else":
            k = consume_statement(s, tb)
        return k
    if tok in ("case", "casez", "casex"):
        depth, k = 1, b
        while depth and k < len(s):
            t, ta, tb = next_token(s, k)
            if not t:
                return len(s)
            if t in ("case", "casez", "casex"):
                depth += 1
            elif t == "endcase":
                depth -= 1
            k = tb
        return k
    if tok == "for":
        t, ta, tb = next_token(s, b)
        k = match_paren(s, ta) if t == "(" else tb
        return consume_statement(s, k)
    j = s.find(";", i)
    return len(s) if j < 0 else j + 1


FOR_HDR_RE = re.compile(r"\bfor\s*\(")
STMT_LHS = r"(?:^|(?<=[;:)\n])|(?<=\bbegin)|(?<=\belse))\s*([A-Za-z_]\w*)\s*((?:\[[^\]]*\]\s*)*)"
NBA_RE = re.compile(STMT_LHS + r"<=")
BA_RE = re.compile(STMT_LHS + r"=(?!=)")


def blank_for_headers(body: str) -> str:
    out = list(body)
    for m in FOR_HDR_RE.finditer(body):
        start = m.end() - 1
        end = match_paren(body, start)
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


class Checker:
    def __init__(self, path: Path):
        self.path = path
        self.raw = path.read_text(errors="replace")
        self.code = strip_comments_strings(self.raw)
        self.lines = self.raw.splitlines()
        self.findings: list[Finding] = []

    def line_of(self, offset: int) -> int:
        return self.raw.count("\n", 0, offset) + 1

    def add(self, offset_or_line: int, rule: str, msg: str, is_line: bool = False) -> None:
        line = offset_or_line if is_line else self.line_of(offset_or_line)
        text = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        w = WAIVE_RE.search(text)
        if w and rule in re.findall(r"R\d\d", w.group(1)):
            return
        self.findings.append(Finding(str(self.path), line, rule, msg))

    # ---------------------------------------------------------------- rules
    def run(self) -> list[Finding]:
        self.r01_nettype()
        self.r02_sv()
        self.simple_rules()
        self.r06_markers()
        self.r16_async_reg()
        self.module_rules()
        return sorted(self.findings, key=lambda f: (f.line, f.rule))

    def r01_nettype(self) -> None:
        directives = [(m.start(), m.group(0)) for m in re.finditer(r"`\w+[^\n]*", self.code)]
        directives = [(o, d.strip()) for o, d in directives
                      if not d.startswith(("`timescale", "`resetall", "`include", "`define",
                                           "`ifdef", "`ifndef", "`else", "`endif", "`undef"))]
        first_code = re.search(r"\S", self.code)
        if not directives or not re.match(r"`default_nettype\s+none\b", directives[0][1]):
            self.add(first_code.start() if first_code else 0, "R01",
                     "file does not start with `default_nettype none")
        elif first_code and first_code.start() < directives[0][0]:
            pre = self.code[first_code.start():directives[0][0]]
            if re.search(r"\bmodule\b", pre):
                self.add(first_code.start(), "R01", "`default_nettype none appears after module code")
        last_nt = [d for d in directives if d[1].startswith("`default_nettype")]
        tail = self.code.rstrip()
        if not last_nt or not re.match(r"`default_nettype\s+wire\b", last_nt[-1][1]) \
                or re.search(r"\bendmodule\b", self.code[last_nt[-1][0]:]):
            self.add(len(tail), "R01", "file does not end with `default_nettype wire after endmodule")

    def r02_sv(self) -> None:
        for pat, name in SV_PATTERNS:
            for m in re.finditer(pat, self.code):
                self.add(m.start(), "R02", f"SystemVerilog construct: {name}")

    def simple_rules(self) -> None:
        for m in re.finditer(r"\bcasex\b", self.code):
            self.add(m.start(), "R03", "casex")
        for m in re.finditer(r"#\s*\d", self.code):
            self.add(m.start(), "R04", "#delay")
        for m in re.finditer(r"\breg\b[^;=]*?\b([A-Za-z_]\w*)\s*(\[[^\]]*\]\s*)*=(?!=)[^;]*;", self.code):
            self.add(m.start(), "R05", f"initializer on reg {m.group(1)}")
        for m in re.finditer(r"\boutput\s+reg\b", self.code):
            self.add(m.start(), "R14", "output reg")
        for m in re.finditer(r"[^/*]([/%])[^/*=]", self.code):
            ls = self.code.rfind("\n", 0, m.start()) + 1
            stmt_start = max(self.code.rfind(";", 0, m.start()), self.code.rfind("\n\n", 0, m.start()))
            ctx = self.code[max(ls, stmt_start + 1):m.start()]
            full_ctx = self.code[stmt_start + 1:m.start()]
            if re.search(r"\b(localparam|parameter)\b", full_ctx) or "`" in ctx:
                continue
            self.add(m.start(1), "R13", f"'{m.group(1)}' operator in logic")

    def r16_async_reg(self) -> None:
        pat = re.compile(r"(\(\*(?:(?!\*\)).)*\*\)\s*)?\breg\b[^;]*?\b(\w*_(?:meta|sync)\d*_ff)\b", re.S)
        for m in pat.finditer(self.code):
            attr = m.group(1) or ""
            if not re.search(r"\bASYNC_REG\b", attr, re.I):
                self.add(m.start(2), "R16", f"synchronizer '{m.group(2)}' lacks (* ASYNC_REG = \"TRUE\" *)")

    def r06_markers(self) -> None:
        for i, line in enumerate(self.lines, 1):
            if re.match(r"\s*//@", line):
                self.add(i, "R06", "//@ marker left in RTL", is_line=True)

    # -------------------------------------------------------------- modules
    def module_rules(self) -> None:
        for mm in re.finditer(r"\bmodule\s+([A-Za-z_]\w*)", self.code):
            end = self.code.find("endmodule", mm.end())
            end = len(self.code) if end < 0 else end
            self.check_module(mm.group(1), mm.start(), mm.end(), end)

    def check_module(self, name: str, mstart: int, hdr: int, mend: int) -> None:
        code = self.code
        # header: optional #( params ), then ( ports );
        params: list[tuple[str, int]] = []
        k = hdr
        tok, a, b = next_token(code, k)
        if tok == "#":
            t2, a2, b2 = next_token(code, b)
            pend = match_paren(code, a2)
            for pm in re.finditer(r"\bparameter\b[^,;)]*?\b([A-Za-z_]\w*)\s*=", code[a2:pend]):
                params.append((pm.group(1), a2 + pm.start(1)))
            k = pend
        t3, a3, b3 = next_token(code, k)
        port_end = match_paren(code, a3) if t3 == "(" else k
        ports = code[a3:port_end]
        body_start = code.find(";", port_end) + 1
        body = code[body_start:mend]

        clocked_blocks, comb_blocks = [], []
        for am in re.finditer(r"\balways\b", body):
            t, ta, tb = next_token(body, am.end())
            if t != "@":
                continue
            t, sa, sb = next_token(body, tb)
            if t == "(":
                sens_end = match_paren(body, sa)
                sens = body[sa:sens_end]
            else:
                sens_end, sens = sb, t
            stmt_end = consume_statement(body, sens_end)
            blk = (body_start + sens_end, body[sens_end:stmt_end])
            edges = re.findall(r"\b(?:posedge|negedge)\s+([A-Za-z_]\w*)", sens)
            if len(edges) > 1:
                self.add(body_start + am.start(), "R09", f"sensitivity on {', '.join(edges[1:])} besides the clock")
            if edges:
                clocked_blocks.append(blk)
            else:
                comb_blocks.append(blk)

        # memory arrays (unpacked) are exempt from _ff naming
        arrays = set(re.findall(r"\breg\b[^;]*?\b([A-Za-z_]\w*)\s*\[[^\]]*\]\s*;", body))
        arrays |= set(re.findall(r"\breg\b\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*\[", body))

        clocked_regs: set[str] = set()
        reported: set[str] = set()
        for off, blk in clocked_blocks:
            clean = blank_for_headers(blk)
            for m in NBA_RE.finditer(clean):
                sig = m.group(1)
                if sig in KEYWORDS:
                    continue
                clocked_regs.add(sig)
                if not sig.endswith("_ff") and sig not in arrays and sig not in reported:
                    reported.add(sig)
                    self.add(off + m.start(1), "R07", f"'{sig}' is a flop but lacks _ff")
            for m in BA_RE.finditer(clean):
                sig = m.group(1)
                if sig not in KEYWORDS and sig not in arrays:
                    self.add(off + m.start(1), "R15", f"blocking '=' to '{sig}' in clocked block")
        for off, blk in comb_blocks:
            clean = blank_for_headers(blk)
            for m in NBA_RE.finditer(clean):
                if m.group(1) not in KEYWORDS:
                    self.add(off + m.start(1), "R15", f"non-blocking '<=' to '{m.group(1)}' in comb block")

        # _ff on things that are not clocked regs (wires, ports, comb regs)
        decl_re = re.compile(r"\b(wire|input|output|inout)\b[^;,)=]*?\b([A-Za-z_]\w*_ff)\b")
        for m in decl_re.finditer(code, a3, mend):
            self.add(m.start(2), "R08", f"'{m.group(2)}' declared as {m.group(1)} but named _ff")
        for off, blk in comb_blocks:
            for m in BA_RE.finditer(blank_for_headers(blk)):
                if m.group(1).endswith("_ff") and m.group(1) not in clocked_regs:
                    self.add(off + m.start(1), "R08", f"comb signal '{m.group(1)}' named _ff")

        self.check_instances(body, body_start)

    def check_instances(self, body: str, base: int) -> None:
        pat = re.compile(r"(?:^|(?<=[;\n]))\s*([A-Za-z_]\w*)\s*(#\s*\()?", re.M)
        for m in pat.finditer(body):
            mod = m.group(1)
            if mod in KEYWORDS:
                continue
            k = m.end()
            if m.group(2):
                k = match_paren(body, m.end() - 1)
            t, ta, tb = next_token(body, k)
            if not re.match(r"[A-Za-z_]\w*$", t) or t in KEYWORDS:
                continue
            inst, inst_off = t, ta
            t2, pa, pb = next_token(body, tb)
            if t2 == "[":  # instance array
                close = body.find("]", pa)
                t2, pa, pb = next_token(body, close + 1)
            if t2 != "(":
                continue
            first, fa, fb = next_token(body, pb)
            if first not in (".", ")"):
                self.add(base + fa, "R11", f"positional connection in instance '{inst}' of '{mod}'")
            if not inst.startswith("u_"):
                self.add(base + inst_off, "R12", f"instance '{inst}' of '{mod}' lacks u_ prefix")


def main(argv: list[str]) -> int:
    if "--list-rules" in argv:
        for k, v in RULES.items():
            print(f"{k}  {v}")
        return 0
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        print(__doc__)
        return 2
    findings: list[Finding] = []
    for f in files:
        p = Path(f)
        if not p.is_file():
            print(f"error: {f}: no such file", file=sys.stderr)
            return 2
        if p.suffix != ".v":
            continue  # benches (.sv) follow a different rule set
        findings += Checker(p).run()
    for fd in findings:
        print(fd)
    print(f"mae.style: {len(files)} file(s), {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
