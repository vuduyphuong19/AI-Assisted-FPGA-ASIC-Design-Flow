# FPGA design flow with ic-agents: from spec to a running board

This document records the flow as it was actually run end to end on the reference block
**`uart_apb`** (APB3 UART, 23 requirements) targeting the **ZCU104** board
(Zynq UltraScale+ `xczu7ev-ffvc1156-2-e`). It serves as the playbook for the next FPGA block and
as the baseline to compare against when the ASIC flow is tried (see §9).

The flow has two halves:

| Half | Driven by | Produces |
|------|-----------|----------|
| **Block flow** (§2–§6): spec → RTL/TB/properties/XDC → lint/sim/formal → Vivado out-of-context synth + impl → review → signoff | `ic-orchestrator` + subagents; state, tools and gates owned by `python3 tools/mae.py` | signed evidence (`.mae/ledger.jsonl`), gates, `reports/signoff_report.md` |
| **Board integration** (§7): Block Design, BD testbench, bare-metal software, JTAG run | the human, with the `project-maintainer` session (scripts in `impl/board/zcu104/`) | bitstream, XSA, ELF, board test result (no mae evidence) |

---

## 1. Principles that shape every step

1. **Truth comes from tools.** Pass/fail, coverage, proofs, area and timing exist only as evidence
   records written by the runner (`mae run <style|lint|sim|formal|synth|impl>`), HMAC-signed and
   hash-chained. Agents cite them by ID (`EV-...`). Missing evidence, `NOT_RUN` and `ERROR` count
   as failures. Agreement between agents is not evidence.
2. **Typed claims.** Every statement in a report is a `hypothesis`, `assumption`, `estimate`,
   `unverified` or `tool_result` (with `evidence_refs`). Only a passed gate creates a verified fact;
   untyped or unsupported claims are downgraded and recorded as findings.
3. **Independence barriers.** The RTL designer, DV engineer and formal engineer all work from the
   approved spec only and cannot read each other's files (enforced by `.claude/hooks/guard_tools.py`
   and `.claude/mae-policy.json`). A bug therefore has to slip past three independent readings of
   the spec.
4. **Humans decide.** Spec baseline, waivers, budget extensions and signoff are
   `python3 tools/mae.py approve ...`, run by a person in their own terminal. Agents are blocked
   from running it.

---

## 2. Stage 0–1: project setup and specification

```bash
# sources (Markdown, PDF, draw.io) go in spec/sources/; diagrams need a text extract:
python3 tools/drawio_text.py spec/sources/<diagram>.drawio > spec/sources/<diagram>.drawio.md
claude --agent spec-analyst          # drafts spec-drafts/requirements.json + <block>_notes.md
# review, copy the JSON to spec/requirements.json, then:
python3 tools/mae.py init --impl-tool vivado --part xczu7ev-ffvc1156-2-e
python3 tools/mae.py approve spec_baseline --signer <you> --reason "baseline reviewed"
```

- `spec/requirements.json` is **the contract**: `block`, `parameters`, `ports`, atomic
  `requirements` (`REQ-n`), `coverage_bins`, `ppa_targets` (`fmax_mhz`, `part`).
- `init` derives the file names (`rtl/<block>.v`, `dv/tb_<block>.v`, `formal/<block>_props.v`,
  `impl/<block>.xdc`) and the clock period (`1000 / fmax_mhz`), and creates the runner key.
- The companion `spec/<block>_notes.md` is for the human reviewer only (gaps, open points written
  as options). Agents receive only the JSON.
- Any later edit to the spec changes its hash: `approve spec_baseline` is due again.

**uart_apb:** 4 parameters (`FIFO_DEPTH=4`, `ADDR_W=5`, `DIV_RESET=868`, `DIV_MIN=4`),
13 ports, 23 requirements, 25 coverage bins, 100 MHz target (10.000 ns).

---

## 3. Stage 2: parallel authoring (behind barriers)

After `spec_baseline`, `mae next` makes four agent tasks ready at once. The orchestrator gets a
signed brief for each (`mae brief <task>`) and spawns the subagent with it.

| Task | Agent (model) | Skill | Output | Method highlights |
|------|---------------|-------|--------|-------------------|
| `rtl` | `rtl-designer` (sonnet) | `rtl-coding` + `patterns.md` | `rtl/<block>.v` | plan before code; Verilog-2005, synchronous active-low reset, `_ff` naming; ASYNC_REG 2-flop synchronizer for async inputs (rule R16); patterns P1–P19 and bug catalog |
| `tb` | `dv-engineer` (opus) | `dv-testbench` | `dv/tb_<block>.v` | reference model, per-cycle compare, REQ-tagged checks, stimulus phases (directed → random → error/recovery), coverage bins |
| `props` | `formal-engineer` (opus) | your `verification-code-style` | `formal/<block>_props.v` | immediate asserts/assumes for Yosys k-induction; every `assume` is an `assumption` claim citing a REQ |
| `constraints` | `impl-engineer` (sonnet) | `vivado-flow` + `board-reference.md` | `impl/<block>.xdc` | out-of-context XDC: one `create_clock` at the spec period; no exceptions without a `# REQ-n` reference; `HD.CLK_SRC` site **must exist on the part** |

Each agent ends by writing `reports/<task>.json`; the `SubagentStop` hook refuses to let it stop
until the report validates, then the orchestrator runs `mae submit <task>`.

**uart_apb:** RTL 352 lines, testbench 1305 lines, properties 319 lines, XDC 13 lines.

---

## 4. Stage 3–4: verification gates and the debug loop

```text
style ─┐
lint  ─┤
sim   ─┼──▶ rtl_freeze gate ──▶ synth ──▶ impl ──▶ review ──▶ signoff (human)
formal─┘        │ fail
                ▼
          triage-N (debug-triage) ──▶ fix-N (owner of the suspect artifact) ──▶ re-run
```

| Run | Tool | What passes |
|-----|------|-------------|
| `style` | `mae/style.py` | no rule violations (R01–R16: reset style, naming, no latches, ASYNC_REG, ...) |
| `lint` | Verilator `-Wall` | no warnings |
| `sim` | Icarus Verilog | testbench result line PASS; every REQ has checks; every coverage bin hit |
| `formal` | Yosys `sat -tempinduct` | all asserts proven (k-induction, `-maxsteps 20`) |

`rtl_freeze` requires all four on the **same RTL hash**. If a run fails, `debug-triage` decides
which artifact is wrong (RTL, testbench, properties or spec) from the evidence, and the owner of
that artifact fixes it. `max_fix_attempts` (default 3) and a no-progress counter bound the loop;
`approve extend_budget` lets a human grant more.

**uart_apb:** style, lint, sim and formal passed with no debug iteration needed
(sim EV-2756c0315d: per-REQ checks from 273 to 283,859; formal EV-5b28863441: proven).
The formal engineer used four deliberately failing vacuity probes (EV-a591c318ec, ...) to show the
properties can fail.

---

## 5. Stage 5: Vivado back end (out of context)

The runner writes its own Tcl (agents never supply Tcl): `read_verilog` → `read_xdc` →
`synth_design -mode out_of_context` → reports → `opt_design` / `place_design` /
`phys_opt_design` / `route_design` → reports. The parser fails the run on:

- `ERROR` lines, missing end marker (incomplete run);
- **a CRITICAL WARNING located in the XDC or `[Common 17-69]` (a constraint that was not applied)**,
  added after this project, see §6;
- timing exceptions without a REQ reference, a clock period different from the spec;
- unsafe CDC (any Critical `report_cdc` rule) or a synchronizer without ASYNC_REG (CDC-2/CDC-5);
- latches, multi-driven nets; (impl) routing errors, no clocks, WNS < 0, WHS < 0, bad `check_timing`.

Gates: `synth_clean` (synthesis passed) and `timing` (post-route timing met; waivable by a human).
Post-synthesis timing is recorded as an **estimate**, never as a timing result.

**uart_apb:** synth EV-f23b4b1349 and impl EV-c0842be1c2 passed, with WNS +8.169 ns and
WHS +0.025 ns, but with an ideal clock: the XDC's `HD.CLK_SRC BUFGCTRL_X0Y0` names a site that
does not exist on UltraScale+ (finding F010). The XDC now uses `BUFGCE_X0Y96`. A scratch run (not
evidence) gives WNS +7.765 ns and WHS +0.012 ns with real clock skew; re-run the flow to record it.

---

## 6. Stage 6–7: review, findings and signoff

`design-reviewer` (opus, skills `rtl-coding`, `dv-testbench`, `vivado-flow`) reviews every artifact,
claim and evidence record adversarially and records findings (severity 1 blocks signoff).
`mae report` writes `reports/signoff_report.md`: signoff criteria, requirement → evidence
traceability, debug iterations, findings, evidence ledger, human decisions.

Signoff criteria: `rtl_freeze`, `synth_clean`, `timing`, no open severity-1 finding, all evidence
from real tools (not the FAKE Vivado stub), clean ledger audit.

**uart_apb:** all criteria PASS; 23 findings (10 severity 2, 13 severity 3, none blocking).
The ones worth remembering for the next block:

| Finding | Lesson |
|---------|--------|
| F003/F013 | Spec conflict: REQ-1 (outputs 0 after reset) vs REQ-2/3/4 when an APB access starts in the first cycle after reset. Write the reset-exit behaviour into the spec. |
| F004/F017 | k-induction with a 16-cycle window after reset only proves stateless decode unboundedly; long behaviours (full frames, RX) rest on simulation. Plan formal scope explicitly (bounded BMC mode, larger depth, or accept sim-only). |
| F010/F021 | `HD.CLK_SRC BUFGCTRL_X0Y0` is a 7-series site; it failed silently on UltraScale+. The runner now fails such runs and the skill is part-aware. Always read sites from the device (`get_sites`). |
| F011/F012/F014 | Same-cycle races (TX push vs pop, config write vs RX start, RX commit point) were not pinned by the spec, so tests accepted either outcome. Specify the edge or a tolerance window. |
| F015 | One deterministic seed for all RX behaviour and no RX at the real operating point (D = 868). Run multiple seeds and the real baud divisor. |
| F022 | Combinational `prdata` is unconstrained out of context; only the board build times it. |

Then the human runs `python3 tools/mae.py approve signoff --signer <you> --reason "..."`.

---

## 7. Stage 8: board integration on ZCU104 (manual, outside mae)

Scripts and details: [`impl/board/zcu104/README.md`](../impl/board/zcu104/README.md). The signed-off RTL is used
unchanged; this stage produces no mae evidence.

```text
 Zynq US+ PS ── M_AXI_HPM0_FPD ──▶ smartconnect ──▶ axi_apb_bridge ──APB3──▶ uart_0 (rtl/uart_apb.v)
   pl_clk0 100 MHz, pl_resetn0 ─▶ proc_sys_reset          irq ─▶ pl_ps_irq0     txd C19 / rxd A20
```

| Step | Command | uart_apb result |
|------|---------|-----------------|
| 0. Testbench dry run (Icarus + stub BD) | `iverilog ... && vvp ...` | `TB_RESULT PASS checks=272 errors=0` |
| 1. Project, BD, synth, impl, bitstream, XSA | `vivado -mode batch -source impl/board/zcu104/build_bd.tcl` | WNS **+5.504 ns**, WHS **+0.029 ns**, 0 critical warnings; 599 LUTs (0.26 %), 704 FFs (0.15 %), 0 BRAM/DSP for the whole design |
| 2. BD testbench in XSim (PS VIP drives AXI) | `vivado -mode batch -source impl/board/zcu104/sim_bd.tcl` | `TB_RESULT PASS checks=115 errors=0` |
| 3. Bare-metal software | `xsct impl/board/zcu104/sw/build_sw.tcl` | `uart_apb_demo.elf` built, no warnings; `xparameters.h`: base `0xA0000000`, IRQ 121 |
| 4. Program and run over JTAG | `xsct impl/board/zcu104/sw/run_jtag.tcl` | PS console: `STATUS after reset 0x0A`, `BAUD 868 -> 115207 baud`; uart_apb line: banner, typed text echoed exactly, no RX errors |

Board-level lessons:

- **Resources:** parallel out-of-context IP synthesis (`launch_runs -jobs 4`) crashed a 16 GB
  desktop. `build_bd.tcl` now uses `jobs 1`; run long Vivado jobs detached
  (`setsid nohup nice -n 19 ...`) so a closed session does not kill them.
- **Do not rebuild while the project is open in the Vivado GUI**: `create_project -force` deletes it.
- **Software rule:** the AXI-APB bridge turns PSLVERR into AXI SLVERR, which is a data abort on the
  A53. The driver checks `TX_FULL` before writing, `RX_EMPTY` before reading, never writes
  BAUD < 4, never touches unmapped offsets.
- **Serial ports:** the ZCU104's FT4232H (`0403:6011`, "Xilinx JTAG+3Serial") gives JTAG plus three
  serial ports; once `hw_server` claims JTAG, the PS console is `if01` (`/dev/ttyUSB1`) and the PL
  UART (uart_apb) is `if03` (`/dev/ttyUSB3`). Use `/dev/serial/by-id/*-if03-port0`.
- **IOB packing** of `txd` was not honoured in the BD build (`Place 30-722`) and gives nothing on a
  false-path pin; the constraint was removed.

---

## 8. Command cheat sheet

```bash
python3 tools/mae.py status | next | audit | report
python3 tools/mae.py evidence [EV-...] [--log]
python3 tools/mae.py gate rtl_freeze | synth_clean | timing
python3 tools/mae.py approve spec_baseline | waive:timing | extend_budget --amount N | signoff \
        --signer <you> --reason "..."                       # human only
claude                                                        # orchestrator: "Run the flow."
MAE_ROLE=project-maintainer claude --agent project-maintainer # supervised maintenance
python3 -m unittest discover -s tests                         # control-plane tests
```

---

## 9. Toward the ASIC flow (plan, not yet run)

What carries over unchanged: the orchestrator, the spec stage, the barriers, typed claims, the
evidence ledger, `style`/`lint`/`sim`/`formal` and `rtl_freeze`, review and signoff.

What changes (hypotheses to confirm when the ASIC flow is tried):

| Area | FPGA (this run) | ASIC (expected) |
|------|-----------------|-----------------|
| Back end | `--impl-tool vivado` (synth + place/route) | `--impl-tool yosys` exists today: generic synthesis + optional OpenSTA (`impl/sta.tcl`). Needs a liberty file for real area/timing; place and route (e.g. OpenROAD) would be new `Project._template()` tasks |
| Constraints | XDC by `impl-engineer` with the `vivado-flow` skill | SDC (clocks, I/O delays, uncertainty) - a new `asic-flow`/SDC skill, or extend `vivado-flow` |
| Reset style | synchronous active-low (Vivado-friendly) | often asynchronous assert / synchronous de-assert; the `rtl-coding` skill and style rules R01–R16 would need an ASIC profile (see NOTICE: VLSIT ASIC rules were deliberately not adopted) |
| Memories | distributed RAM inferred by Vivado | SRAM macros or flop arrays; macro wrappers |
| Extra signoff | timing, CDC, DRC from Vivado | DFT/scan, equivalence (RTL vs netlist), power, gate-level simulation |

Start the ASIC trial by copying the framework to a new project (README, "Start a new project")
and initialising with `--impl-tool yosys`.
