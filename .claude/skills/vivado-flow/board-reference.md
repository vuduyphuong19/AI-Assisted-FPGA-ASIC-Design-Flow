# Vivado Reference: constraints, CDC, IP, timing closure

Read the section you need. Topics distilled from QingquanYao/xilinx-skill
(`xdc_constraints.md`, `vu9p_guide.md`, `vivado_guide.md`, `tcl_commands.md`,
`official-docs/index.md`), rewritten, corrected where it was wrong, and tied to
this project's checks. Sections 1–4 apply now (`impl/<top>.xdc`, OOC). Sections
5–7 apply when a block becomes a board top level. **The mae runner does not run
those flows yet**: a task that needs them is escalated to the human, not
improvised.

Rule for every Tcl/XDC/IP detail: check the exact syntax and property names for
the project's Vivado version (UG835 / UG912, or `report_property` on the object)
rather than recalling them. APIs and IP parameter names change between versions.

## 1. Constraint file layout

Order inside the file: **clocks → I/O → timing exceptions**. Exceptions refer to
clocks and ports, so those must exist first. Keep one XDC per block.

Every exception (`set_false_path`, `set_multicycle_path`, `set_max_delay`,
`set_min_delay`, `set_clock_groups`) carries a `REQ-<id>` reference in a comment
on its own line or the comment line directly above. The runner fails the run
otherwise (`xdc_audit.exceptions_undocumented`). Every `create_clock` period must
equal `project.json → impl.period_ns` (`xdc_audit.period_mismatch`).

## 2. Clocks

| Situation | Constraint |
|-----------|------------|
| Single-ended clock port | `create_clock -name clk -period <ns> [get_ports clk]` |
| Differential clock (`clk_p`/`clk_n`) | constrain **only the P port**; the tool infers N |
| MMCM/PLL output | do **not** create it. Vivado derives generated clocks from the primitive's settings. Use `create_generated_clock -name <x> [get_pins …]` only to give a derived clock a stable name |
| Clock divided by logic (counter, toggle flop) | don't build one: use a clock enable (`rtl-coding` §5). If it already exists, it needs `create_generated_clock -source … -divide_by N` and is a review finding |
| I/O timed against an external device | a virtual clock (`create_clock -name vclk -period <ns>`, no object) as the reference for I/O delays |

OOC only: `set_property HD.CLK_SRC <bufg_site> [get_ports clk]` tells the
placer where the clock buffer would be. The site is part-specific: 7-series
`BUFGCTRL_X0Y0`; UltraScale+ has no `X0Y0` site (xczu7ev: `BUFGCE_X0Y96`).
A non-existent site is a failed XDC command (Common 17-69); the runner fails it.

## 3. Clock-domain crossings (CDC)

Vivado's `report_cdc` classifies every crossing. Unsynchronized single-bit and
multi-bit crossings are **Critical** (CDC-1, CDC-4). A 2+ stage synchronizer
with a missing `ASYNC_REG` property is a **Warning** (CDC-2, CDC-5). The runner
fails synthesis on any Critical CDC rule and on CDC-2/CDC-5.

| Crossing | RTL | Constraint |
|----------|-----|------------|
| Single control bit | 2-flop synchronizer `x_meta_ff → x_sync_ff`, both declared `(* ASYNC_REG = "TRUE" *)` (style rule R16) | clocks unrelated: `set_clock_groups -asynchronous -group … -group …` (REQ-cited) |
| Multi-bit counter/pointer | gray code, then a synchronizer per bit | `set_max_delay -datapath_only <min(src period, dst period)> -from … -to …` — **not** a false path, so bus skew stays bounded |
| Multi-bit data bus | req/ack handshake or async FIFO; data held stable while the synchronized req travels | `set_max_delay -datapath_only` on the data bus |
| Quasi-static config (written once, then stable) | the spec must say it is stable during operation | `set_false_path -to <dst regs>` with that REQ cited |
| Asynchronous reset input | synchronize the deassertion (2-flop, ASYNC_REG) | false path on the raw input only, REQ-cited |

A blanket `set_false_path` between two clocks hides every future crossing
between them, including unsafe ones. Use it only in the documented cases above.
Check with `report_cdc` (Critical = 0), `report_clock_interaction`, and
`check_timing` (`no_clock`, `unconstrained_internal_endpoints` = 0).

**Constraint precedence — the rows above do not mix on one clock pair.** In
Vivado, `set_clock_groups` and `set_false_path` take precedence over
`set_max_delay -datapath_only` (UG903, constraint priority). If two clocks carry
both single-bit synchronizers and a gray/handshake bus, a `set_clock_groups`
between them silently cancels the bus's `set_max_delay`, and the bus skew is no
longer bounded. For such a pair, put **no** clock group or clock-to-clock false
path: constrain every crossing with `set_max_delay -datapath_only` from the
source flops to the first sync stage (single bits included), each REQ-cited.
Use `set_clock_groups -asynchronous` only for a clock pair crossed exclusively
by single-bit synchronizers. With `-datapath_only` Vivado does not time hold on
that path, which is intended for a crossing. Confirm in `report_exceptions`
(or `report_timing` on one crossing) that the max delay is not overridden.

| Anti-pattern | What it hides |
|--------------|---------------|
| `set_clock_groups` + `set_max_delay` on the same clock pair | the max delay (bus skew unbounded) |
| false path from a register software can rewrite at run time | a real crossing; quasi-static needs the spec's "stable while active" |
| `set_multicycle_path -setup N` without the matching `-hold N-1` | a hold violation on the same path |
| multicycle on a path whose capture flop is enabled every cycle | a real single-cycle setup path |
| asynchronous group between two outputs of one MMCM/PLL | real synchronous setup/hold paths |
| exception with a wildcard `-through` | unrelated paths through the same net |

## 4. Timing closure order

When `impl` fails setup or hold, work through this list in order. Each step is a
**human decision** offered by the `mae next` escalation; agents only gather
evidence.

1. **Read the worst path** in `impl_timing_summary.rpt`: start and end points,
   logic levels, and whether the delay is logic-dominated or route-dominated.
   Compare with `logic_levels.paths_over_budget`.
2. **Logic-dominated or over the level budget → RTL.** The rtl-designer adds a
   pipeline stage or restructures the logic (latency changes, so the spec owner
   agrees). Duplicate high-fanout registers. A `MAX_FANOUT` attribute is a fallback.
3. **Route-dominated, small negative slack → implementation directive.** Set
   `impl.directive` (e.g. `Explore`) with `mae init --directive`; it applies to
   `place_design` and `route_design`. `phys_opt_design` already runs.
4. **Path really is multicycle or false by specification → constraint.** Only
   with the REQ that says so (enforced by `xdc_audit`).
5. **Lower the clock frequency → spec change.** Never done by editing the XDC
   (enforced by `period_mismatch`); the human changes `ppa_targets` and re-baselines.

Hold violations after routing are rare in a single-clock design. They usually
point at a clock-tree problem or a wrong constraint (`set_min_delay`, I/O delay),
so review the XDC first.

## 5. Board-level I/O (top level only)

- `PACKAGE_PIN` and `IOSTANDARD` on every top-level port. Unconstrained pins are
  a DRC error at bitstream generation.
- All pins in one bank share that bank's VCCO, so the `IOSTANDARD`s in a bank
  must be compatible with one voltage. 7-series HP banks and UltraScale+ HP banks
  go up to 1.8 V. HR (7-series) and HD (UltraScale+) banks support up to 3.3 V.
  Check the board schematic, not memory.
- `DRIVE` and `SLEW` apply only to single-ended outputs. Differential standards
  use `DIFF_TERM` / `DIFF_TERM_ADV` for on-die termination. The LVDS standard
  name depends on bank type (e.g. `LVDS_25` in 7-series HR banks, `LVDS` in HP
  banks).
- System-synchronous I/O delays, relative to the (virtual) clock:
  - `set_input_delay -max` = external Tco(max) + board delay(max)
  - `set_input_delay -min` = external Tco(min) + board delay(min)
  - `set_output_delay -max` = external Tsu + board delay(max)
  - `set_output_delay -min` = board delay(min) − external Th
- Source-synchronous and DDR interfaces need `-clock_fall -add_delay` pairs and
  are designed from the interface's data sheet (UG903 has the templates).

## 6. IP cores (top level only)

Prefer RTL inference for RAM, FIFO, shift registers and arithmetic
(`rtl-coding` §5): it is portable, simulates in this project's flow, and is
checked by the gates. Use vendor IP only for what RTL cannot express well: clock
generation (Clocking Wizard, PG065), transceivers, PCIe (DMA/Bridge Subsystem,
PG195), memory controllers.

Non-project IP flow (sketch; verify syntax in UG835 / UG896 for the version):

```tcl
create_ip -name clk_wiz -vendor xilinx.com -library ip -module_name clk_wiz_0 -dir ip
report_property [get_ips clk_wiz_0]          ;# discover the CONFIG.* names; never guess them
set_property -dict [list CONFIG.<name> {<value>} ...] [get_ips clk_wiz_0]
generate_target all [get_ips clk_wiz_0]
synth_ip [get_ips clk_wiz_0]
# later runs: read_ip ip/clk_wiz_0/clk_wiz_0.xci
```

Pin the IP version to the Vivado version. An IP upgrade is a design change and
gets reviewed. IP models are not simulated by this project's Icarus flow, so a
block that instantiates IP needs a simulation plan before it enters the flow.

## 7. Bitstream and hand-off (top level only)

After a routed design meets timing: `write_bitstream`. For Vitis/PetaLinux, use
`write_hw_platform -fixed -include_bit <name>.xsa`. Vivado, Vitis and PetaLinux
versions must match. Check `report_drc` (no errors) and `report_methodology`
(review every Critical) before hand-off.

## 8. Official documents (docs.amd.com/r/en-US/<id>)

| ID | Title | Use it for |
|----|-------|-----------|
| UG835 | Tcl Command Reference | exact command syntax and options |
| UG901 | Synthesis | synthesis attributes (`ASYNC_REG`, `ram_style`, `MAX_FANOUT`), RTL inference templates |
| UG903 | Using Constraints | XDC syntax, clocks, I/O delays, CDC constraints |
| UG904 | Implementation | opt/place/phys_opt/route, directives |
| UG906 | Design Analysis and Closure Techniques | timing reports, `report_cdc` rules, closure |
| UG912 | Properties Reference | every object property (note: *not* IP Integrator) |
| UG949 | UltraFast Design Methodology | reset, clocking, CDC and closure best practice |
| UG896 / UG994 | Designing with IP / IP Integrator | IP flow / Block Design |
| UG908 | Programming and Debugging | bitstream download, ILA/VIO |
| UG471 / UG571 | SelectIO (7-series / UltraScale) | I/O standards, banks, termination |
| UG472 / UG572 | Clocking Resources (7-series / UltraScale) | MMCM/PLL, global buffers |
| UG473 / UG573 | Memory Resources (7-series / UltraScale) | BRAM/URAM primitives |
| PG065 / PG058 / PG195 | Clocking Wizard / Block Memory Generator / DMA-Bridge for PCIe | IP parameters |
| DS890 | UltraScale Architecture and Product Data Sheet: Overview | device selection, speed grades |
| DS925 | Zynq UltraScale+ MPSoC DC and AC Switching Characteristics | electrical and switching limits |

Two entries in xilinx-skill's index were wrong: it lists UG912 as IP Integrator
(it is the Properties Reference) and DS890 as the MPSoC DC/AC data sheet (that
is DS925).
