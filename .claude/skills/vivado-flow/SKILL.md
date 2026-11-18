---
name: vivado-flow
description: How this project uses Vivado — writing the out-of-context XDC (clocks, REQ-cited exceptions, CDC constraints), what `mae run synth` and `mae run impl` execute and enforce, how to read vivado_synth / vivado_impl evidence (utilization, WNS/WHS, Fmax basis, logic levels, check_timing, report_cdc, XDC audit), and the timing-closure order. board-reference.md covers board I/O, IP cores and official documents. Use for constraints tasks and when reviewing implementation evidence.
---

# Vivado Flow

Distilled from rtl-skills `vsynth` (Apache-2.0) with two fixes: constraints are
read **before** `synth_design` (timing-driven synthesis), and no
`read_verilog -v` (not a Vivado option, UG835). Place & route was added so the
timing gate rests on post-route numbers. Constraint, CDC and closure guidance
distilled from QingquanYao/xilinx-skill, corrected and made checkable — details
in `board-reference.md` (same folder).

## 1. Who does what

| Step | Who | How |
|------|-----|-----|
| Write `impl/<top>.xdc` | impl-engineer (`constraints` task) | §2 |
| Synthesis | runner | `python3 tools/mae.py run synth` → kind `vivado_synth` |
| Place, route, timing | runner | `python3 tools/mae.py run impl` → kind `vivado_impl` |
| Numbers | `mae/vivado.py` parser | evidence summary; missing values are `null`, never guessed |
| Decisions on failures | human (escalation) | §5 |

The runner writes its own Tcl (`.mae/runs/<EV>/run.tcl`): `read_verilog` →
`read_xdc` → `synth_design -mode out_of_context` → reports → (impl:
`opt_design`, `place_design`, `phys_opt_design`, `route_design`) → reports.
Agents never supply Tcl or command lines. Part and period come from
`project.json` → `impl` (period = 1000 / spec `ppa_targets.fmax_mhz`;
`directive` for place/route, default `Default`). `report_cdc` runs when the
design has more than one clock.

## 2. Writing the XDC

```tcl
# <top>.xdc - out-of-context constraints (source: spec ppa_targets)
create_clock -name clk -period <period_ns> [get_ports clk]
set_property HD.CLK_SRC <bufg_site> [get_ports clk]   ;# optional, part-specific: see below
```

- `HD.CLK_SRC` names a real global-buffer site **on the target part**.
  7-series: `BUFGCTRL_X0Y0`. UltraScale/UltraScale+: there is no `X0Y0`
  site (xczu7ev: `BUFGCE_X0Y96`, `BUFGCTRL_X0Y32` onward). A site that does
  not exist raises `CRITICAL WARNING [Common 17-69]`, and the runner fails the run
  (`constraint not applied`). If you cannot confirm a site for the part, omit
  the line and add an `assumption` claim: clock skew is then ideal in OOC timing.

- Order: clocks → I/O → exceptions.
- One `create_clock` per clock port, named after the port; the period **must**
  equal `impl.period_ns` (the runner fails a mismatch). Differential clock:
  constrain only the P port. Never re-create MMCM/PLL outputs: Vivado derives them.
- **No exceptions by default.** `set_false_path`, `set_multicycle_path`,
  `set_max_delay`, `set_min_delay`, `set_clock_groups` only where the spec
  documents the behavior, with `# REQ-<id>` on the line or the comment line above
  (the runner fails an exception without one), and an `assumption` claim in
  your report.
- Multiple clocks: follow the CDC table in `board-reference.md` §3. Use
  `set_clock_groups -asynchronous` for unrelated clocks crossed only by
  single-bit synchronizers, and `set_max_delay -datapath_only` for gray/handshake
  buses. **Never both on one clock pair**: the clock group overrides the max
  delay (§3, constraint precedence). Avoid a blanket false path between clocks.
- Every crossing in the XDC matches a mechanism the rtl-designer named
  (`rtl-coding/patterns.md` P17). A crossing without a named mechanism is a
  finding against `rtl/`, not something to constrain away.
- No I/O delays unless the spec gives external timing; `TIMING-18` (missing
  I/O delay) and `no_input_delay` / `no_output_delay` in `check_timing` are
  expected out of context.

## 3. Reading the evidence

```bash
python3 tools/mae.py evidence <EV-ID>        # summary
python3 tools/mae.py evidence <EV-ID> --log  # Vivado transcript
```

| Field | Meaning |
|-------|---------|
| `utilization.LUT/FF/BRAM/DSP` | used / available / % |
| `timing.wns_ns`, `whs_ns`, `failing_endpoints` | setup and hold slack |
| `fmax_mhz` + `fmax_basis` | `post-synthesis estimate` (vivado_synth, labelled `timing_claim_type: estimate`) or `post-route` (vivado_impl) |
| `logic_levels.max_level`, `paths_over_budget` | against `impl.logic_level_budget` |
| `run.notable.latch / multi_driven / unconnected_or_undriven` | inference warnings quoted from the log |
| `methodology` | rule, severity, count |
| `routing_errors` | impl only; must be 0 |
| `check_timing` | counts per check (`no_clock`, `unconstrained_internal_endpoints`, `loops`, …) |
| `cdc` | `report_cdc` rule rows (rule, severity, count); `null` = single clock |
| `xdc_audit` | `exceptions_undocumented`, `clock_periods`, `period_mismatch` |
| `missing` | anything the parser could not find |

Status rules (deterministic, in `mae/runner.py`). **Both stages** FAIL on any
`ERROR`, a latch, a multi-driven net, an XDC exception without a REQ reference,
a clock period different from the target, a Critical `report_cdc` rule, or
CDC-2/CDC-5 (synchronizer without `ASYNC_REG`). **impl** also FAILs on routing
errors, no clocks, setup WNS < 0, hold WHS < 0, or `check_timing` reporting
`no_clock`, `unconstrained_internal_endpoints`, `loops` or `latch_loops` > 0. Only `vivado_impl` PASS satisfies the `timing` gate;
post-synthesis slack never does. Evidence produced by a stub tool (version string
contains `FAKE`) blocks signoff.

## 4. What to report as findings

- Latch or multi-driven net → severity 1, artifact `rtl/<top>.v`, cite the
  evidence; it is an RTL bug.
- `paths_over_budget > 0` → severity 2, the endpoint clock and level count; the
  RTL needs a pipeline stage (and its latency changes).
- Setup or hold violation → severity 1 with WNS/WHS and failing endpoints.
- Unconstrained clock, or a timing exception without a spec reference → severity 1.
- Unsafe CDC (CDC-1/4/7/10–14) or a synchronizer without `ASYNC_REG` → severity 1,
  artifact `rtl/<top>.v` (the RTL fix is a 2-flop synchronizer with
  `(* ASYNC_REG = "TRUE" *)`, which style rule R16 also checks).

## 5. When timing fails

`mae next` escalates a failing `synth`/`impl` to the human. Present the evidence
along the closure order in `board-reference.md` §4:
1. Worst path: logic- or route-dominated, and its logic levels.
2. RTL pipelining or restructuring (rtl-designer).
3. An implementation directive (`init --directive Explore`).
4. A REQ-justified constraint.
5. A spec change of the frequency.

The human picks; you do not. Never offer "loosen the period in the XDC". The
runner rejects it, and it would hide the miss.

## 6. Running with real Vivado

`vivado` must be on the runner's PATH (`source <Vivado>/settings64.sh`). The
default part `xc7a35tcpg236-1` is supported by the free WebPACK edition; set
another with `python3 tools/mae.py init --part <part>`. Board-level I/O pins, IP
cores (Clocking Wizard, PCIe), bitstream and XSA hand-off are documented in
`board-reference.md` §5–7 but are **not run by the mae runner yet**: escalate
tasks that need them. Without Vivado the tasks
record `NOT_RUN` and the gates fail — they never pass on missing evidence.
