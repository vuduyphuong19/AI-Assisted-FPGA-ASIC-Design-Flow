"""mae.vivado - Vivado non-project flow for the tool runner (kinds vivado_synth, vivado_impl).

The runner, not an agent, writes the Tcl script: agents only supply the RTL and the XDC
(impl/<top>.xdc, owned by impl-engineer). Report parsing is deterministic; a value that
cannot be found is None and listed under "missing", never guessed.
Flow ideas distilled from rtl-skills' vsynth (Apache-2.0), with fixes: constraints are read
before synth_design, and no `read_verilog -v` (not a Vivado option, UG835).
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

NUM = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)"


def read(p: Path) -> str | None:
    return p.read_text(errors="replace") if p.is_file() else None


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and not set(s) <= set("|+-= "):
            rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows


# ---------------------------------------------------------------- utilization
UTIL_PATTERNS = {
    "LUT": r"^(CLB|Slice) LUTs\*?$",
    "FF": r"^(CLB|Slice) Registers\*?$",
    "BRAM": r"^Block RAM Tile\*?$",
    "URAM": r"^URAM\*?$",
    "DSP": r"^DSPs\*?$",
}


def parse_utilization(text: str) -> dict:
    out: dict = {}
    header: list[str] | None = None
    for row in table_rows(text):
        low = [c.lower() for c in row]
        if "site type" in low and "used" in low:
            header = low
            continue
        if not header or len(row) != len(header):
            continue
        name = row[0]
        for key, pat in UTIL_PATTERNS.items():
            if key in out or not re.match(pat, name):
                continue
            def col(h: str) -> float | None:
                if h not in header:
                    return None
                v = row[header.index(h)].replace(",", "")
                return float(v) if re.fullmatch(NUM, v) else None
            out[key] = {"used": col("used"), "available": col("available"), "util_pct": col("util%")}
    return out


# ---------------------------------------------------------------- timing
def split_cols(line: str) -> list[str]:
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def parse_timing(text: str) -> dict:
    out: dict = {"wns_ns": None, "tns_ns": None, "whs_ns": None, "ths_ns": None,
                 "failing_endpoints": None, "constraints_met": None, "clocks": []}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "WNS(ns)" in line and "TNS(ns)" in line:
            hdr = split_cols(line)
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or set(lines[j].strip()) <= set("- ")):
                j += 1
            if j < len(lines):
                vals = split_cols(lines[j])
                if len(vals) == len(hdr):
                    m = dict(zip(hdr, vals))

                    def f(k: str) -> float | None:
                        v = m.get(k, "")
                        return float(v) if re.fullmatch(NUM, v) else None
                    out.update(wns_ns=f("WNS(ns)"), tns_ns=f("TNS(ns)"), whs_ns=f("WHS(ns)"),
                               ths_ns=f("THS(ns)"))
                    fe = m.get("TNS Failing Endpoints", "")
                    out["failing_endpoints"] = int(fe) if fe.isdigit() else None
            break
    if re.search(r"All user specified timing constraints are met", text):
        out["constraints_met"] = True
    elif re.search(r"Timing constraints are not met", text):
        out["constraints_met"] = False
    # Clock Summary table: "clk  {0.000 1.400}  2.800  357.143"
    start = text.find("Clock Summary")
    if start >= 0:
        stop = min([i for i in (text.find("Timing Summary", start + 13), text.find("Intra Clock", start))
                    if i > 0] or [len(text)])
        for line in text[start:stop].splitlines():
            mm = re.match(rf"\s*(\S+)\s+\{{[^}}]*\}}\s+({NUM})\s+({NUM})", line)
            if mm:
                out["clocks"].append({"name": mm.group(1), "period_ns": float(mm.group(2)),
                                      "freq_mhz": float(mm.group(3))})
    return out


# ---------------------------------------------------------------- power
def parse_power(text: str) -> dict:
    out = {"total_w": None, "dynamic_w": None, "static_w": None, "confidence": None}
    keys = {"Total On-Chip Power (W)": "total_w", "Dynamic (W)": "dynamic_w",
            "Device Static (W)": "static_w", "Confidence Level": "confidence"}
    for row in table_rows(text):
        if len(row) >= 2 and row[0] in keys and out[keys[row[0]]] is None:
            v = row[1].rstrip("*").strip()
            out[keys[row[0]]] = float(v) if re.fullmatch(NUM, v) else (v or None)
    return out


# ---------------------------------------------------------------- logic levels
def level_of(h: str) -> int | None:
    h = h.strip()
    if re.fullmatch(r"\d+", h):
        return int(h)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", h)
    if m:
        return int(m.group(2))
    m = re.fullmatch(r"(\d+)\s*\+", h)
    if m:
        return int(m.group(1))
    return None


def parse_logic_levels(text: str, budget: int) -> dict:
    out: dict = {"budget": budget, "max_level": None, "paths_over_budget": None, "per_clock": []}
    header: list[str] | None = None
    for row in table_rows(text):
        if row and row[0].lower().startswith("end point clock"):
            header = row
            continue
        if header and len(row) == len(header):
            dist = {}
            for h, v in zip(header[2:], row[2:]):
                lv = level_of(h)
                if lv is not None and v.strip().isdigit():
                    dist[lv] = dist.get(lv, 0) + int(v)
            if dist:
                used = [lv for lv, c in dist.items() if c > 0]
                out["per_clock"].append({"clock": row[0], "requirement": row[1],
                                         "distribution": {str(k): v for k, v in sorted(dist.items())},
                                         "max_level": max(used) if used else 0,
                                         "paths_over_budget": sum(c for lv, c in dist.items() if lv > budget)})
    if out["per_clock"]:
        out["max_level"] = max(c["max_level"] for c in out["per_clock"])
        out["paths_over_budget"] = sum(c["paths_over_budget"] for c in out["per_clock"])
    return out


# ---------------------------------------------------------------- run log / rule reports
NOTABLE = {
    "latch": re.compile(r"latch", re.I),
    "multi_driven": re.compile(r"multi-?driven|multiple driver", re.I),
    "unconnected_or_undriven": re.compile(r"unconnected|undriven|no load", re.I),
    "removed_logic": re.compile(r"(unused|removed).*(sequential|register|flop)|(sequential|register).*(unused|removed)", re.I),
}


def parse_log(text: str) -> dict:
    lines = text.splitlines()
    errs = [l for l in lines if l.startswith("ERROR:")]
    crit = [l for l in lines if l.startswith("CRITICAL WARNING:")]
    warns = [l for l in lines if l.startswith("WARNING:")]
    codes = Counter(m.group(0) for l in warns + crit for m in [re.search(r"\[[A-Za-z]+ [\d-]+\]", l)] if m)
    notable = {k: [l[:240] for l in warns + crit if rx.search(l)][:10] for k, rx in NOTABLE.items()}
    # a CRITICAL WARNING located in an XDC file, or a failed Tcl command (Common 17-69), means a constraint
    # was not applied: the reports then describe a design that differs from the one the XDC asked for
    xdc_crit = [l[:240] for l in crit if re.search(r"\.xdc:\d+\]|\[Common 17-69\]", l)]
    return {"errors": errs[:20], "error_count": len(errs), "critical_warning_count": len(crit),
            "critical_warnings": crit[:20], "xdc_critical_warnings": xdc_crit[:10],
            "xdc_critical_count": len(xdc_crit), "warning_count": len(warns),
            "warnings_by_code": dict(codes.most_common(25)), "notable": notable,
            "done_marker": any("VIVADO_FLOW_DONE" in l for l in lines)}


def parse_rule_report(text: str) -> list[dict]:
    out = []
    for row in table_rows(text):
        if len(row) >= 4 and re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", row[0]) and row[-1].isdigit():
            out.append({"rule": row[0], "severity": row[1], "description": row[2], "count": int(row[-1])})
    return out




def parse_check_timing(text: str) -> dict:
    """check_timing section of report_timing_summary: '1. checking no_clock (0)'."""
    out: dict = {}
    for m in re.finditer(r"^\s*\d+\.\s+checking\s+(\w+)\s+\((\d+)\)", text, re.M):
        out.setdefault(m.group(1), int(m.group(2)))
    return out


CDC_CRITICAL_OK = set()          # no Critical CDC rule is acceptable without a human waiver


def parse_cdc(text: str) -> list[dict]:
    """report_cdc summary rows: | Severity | ID | Count | Description |."""
    rows = []
    for row in table_rows(text):
        ids = [c for c in row if re.fullmatch(r"CDC-\d+", c)]
        sev = [c for c in row if c in ("Critical", "Warning", "Info")]
        cnt = [c for c in row if c.isdigit()]
        if ids and sev and cnt:
            rows.append({"rule": ids[0], "severity": sev[0], "count": int(cnt[0]),
                         "description": row[-1] if not row[-1].isdigit() else ""})
    return rows


EXCEPTION_RE = re.compile(r"^\s*(set_false_path|set_multicycle_path|set_max_delay|set_min_delay|set_clock_groups)\b")


def audit_xdc(text: str, period_ns: float | None) -> dict:
    """Deterministic XDC policy: every timing exception cites a requirement (REQ-x) in a comment on
    its line or the line above; every create_clock period equals the spec target."""
    lines = text.splitlines()
    undocumented, clocks, mismatched = [], [], []
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0]
        if EXCEPTION_RE.match(code):
            here = line[len(code):]
            above = lines[i - 1].strip() if i > 0 else ""
            if not re.search(r"REQ-[\w.-]+", here) and not (above.startswith("#") and re.search(r"REQ-[\w.-]+", above)):
                undocumented.append(f"line {i + 1}: {line.strip()[:120]}")
        m = re.search(r"\bcreate_clock\b.*?-period\s+([0-9.]+)", code)
        if m:
            per = float(m.group(1))
            clocks.append(per)
            if period_ns is not None and abs(per - period_ns) > 1e-6:
                mismatched.append(f"line {i + 1}: period {per} ns != target {period_ns} ns")
    return {"exceptions_undocumented": undocumented, "clock_periods": clocks, "period_mismatch": mismatched}

# ---------------------------------------------------------------- Tcl
def tcl_script(top: str, part: str, rtl: str, xdc: str, impl: bool, directive: str = "Default") -> str:
    lines = [
        "# generated by mae.vivado - do not edit",
        f"read_verilog {{{rtl}}}",
        f"read_xdc {{{xdc}}}",
        f"synth_design -top {top} -part {part} -mode out_of_context -flatten_hierarchy rebuilt",
        "report_utilization -file utilization.rpt",
        "report_timing_summary -max_paths 10 -report_unconstrained -file timing_summary.rpt",
        "report_design_analysis -logic_level_distribution -file logic_levels.rpt",
        "report_methodology -file methodology.rpt",
        "report_power -file power.rpt",
        "if {[llength [get_clocks -quiet]] > 1} { report_cdc -details -file cdc.rpt }",
    ]
    if impl:
        lines += [
            "opt_design", f"place_design -directive {directive}", "phys_opt_design",
            f"route_design -directive {directive}",
            "report_utilization -file impl_utilization.rpt",
            "report_timing_summary -max_paths 10 -report_unconstrained -file impl_timing_summary.rpt",
            "report_design_analysis -logic_level_distribution -file impl_logic_levels.rpt",
            "report_power -file impl_power.rpt",
            "report_route_status -file impl_route_status.rpt",
        ]
    lines.append(f'puts "VIVADO_FLOW_DONE top={top} impl={int(impl)}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- summary
def _read(d: Path, name: str) -> str | None:
    p = d / name
    return p.read_text(errors="replace") if p.is_file() else None


def summarize(run_dir: Path, stage: str, period_ns: float | None, budget: int) -> dict:
    """stage: 'synth' or 'impl'. Returns the evidence summary (numbers + missing list)."""
    pre = "impl_" if stage == "impl" else ""
    missing: list[str] = []
    log = (_read(run_dir, "cmd0.stdout") or "") + (_read(run_dir, "cmd0.stderr") or "")
    out: dict = {"stage": stage, "run": parse_log(log)}
    for key, fname, fn in (("utilization", f"{pre}utilization.rpt", parse_utilization),
                           ("timing", f"{pre}timing_summary.rpt", parse_timing),
                           ("power", f"{pre}power.rpt", parse_power)):
        t = _read(run_dir, fname)
        out[key] = fn(t) if t is not None else None
        if t is None:
            missing.append(fname)
    t = _read(run_dir, f"{pre}logic_levels.rpt")
    out["logic_levels"] = parse_logic_levels(t, budget) if t is not None else None
    if t is None:
        missing.append(f"{pre}logic_levels.rpt")
    t = _read(run_dir, "methodology.rpt")
    out["methodology"] = parse_rule_report(t) if t is not None else None
    tt = _read(run_dir, f"{pre}timing_summary.rpt")
    out["check_timing"] = parse_check_timing(tt) if tt is not None else None
    t = _read(run_dir, "cdc.rpt")
    out["cdc"] = parse_cdc(t) if t is not None else None     # None = single clock, report not run
    xdc = sorted(run_dir.glob("*.xdc"))
    out["xdc_audit"] = audit_xdc(xdc[0].read_text(errors="replace"), period_ns) if xdc else None
    if stage == "impl":
        rs = _read(run_dir, "impl_route_status.rpt") or ""
        m = re.search(r"# of nets with routing errors\.*\s*:\s*(\d+)", rs)
        out["routing_errors"] = int(m.group(1)) if m else None
        if m is None:
            missing.append("impl_route_status.rpt routing errors")
    wns = (out.get("timing") or {}).get("wns_ns")
    if period_ns and wns is not None and period_ns - wns > 0:
        out["fmax_mhz"] = round(1000.0 / (period_ns - wns), 2)
    else:
        out["fmax_mhz"] = None
    out["fmax_basis"] = "post-route" if stage == "impl" else "post-synthesis estimate (no placement/routing)"
    out["missing"] = missing
    return out
