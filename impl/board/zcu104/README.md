# uart_apb on ZCU104: Vivado Block Design

Board integration of the signed-off block `rtl/uart_apb.v`. This is a **manual step outside the mae
flow**: it produces no evidence records. The block's verification and out-of-context timing evidence
is in `reports/signoff_report.md`. The RTL is used unchanged.

## Block Design

```
 Zynq US+ PS ── M_AXI_HPM0_FPD ──▶ smartconnect ──▶ axi_apb_bridge ──APB3──▶ uart_0 (uart_apb_bd)
   pl_clk0 100 MHz ─────────────────────┴───────────────┴─────────────────────┘     │ txd ──▶ pin
   pl_resetn0 ─▶ proc_sys_reset ─▶ peripheral_aresetn (synchronous, active-low)      │ rxd ◀── pin
   pl_ps_irq0 ◀─────────────────────────────────────────────────────────── irq ─────┘
```

| Item | Value |
|------|-------|
| Part / board | `xczu7ev-ffvc1156-2-e`, board part `xilinx.com:zcu104:part0:1.1` (latest installed) |
| Clock | PS `pl_clk0` = 100 MHz (spec `fmax_mhz`), BAUD reset 868 → 115200 baud |
| Address | `0xA000_0000`, 4 KB (registers at +0x00..+0x10; +0x20..+0xFFF alias) |
| Interrupt | `irq` → `pl_ps_irq0[0]`, level high (GIC ID 121 per UG1085; confirm in the generated `xparameters.h`) |

## Files

| File | Purpose |
|------|---------|
| `uart_apb_bd.v` | Wrapper with Vivado interface attributes (APB slave, clock, reset, interrupt); no logic. Lint (Verilator `-Wall`) and `mae/style.py` clean. |
| `build_bd.tcl` | Creates the project (RTL + wrapper + XDC + BD testbench), the BD, then synthesis. With pins filled: implementation, bitstream, timing check (fails if WNS/WHS < 0) and XSA with bitstream. `bd_only`: XSA without bitstream. |
| `sim/tb_design_1.sv` | **BD testbench** (XSim). The PS VIP does real AXI transfers through smartconnect → AXI-APB bridge → uart_apb. Checks: reset values, PSLVERR → AXI SLVERR, address map, 4-byte txd→rxd loopback (with an independent txd decoder), irq, external framing error + W1C. Ends with `TB_RESULT PASS/FAIL`. |
| `sim_bd.tcl` | Runs the BD testbench in XSim and fails unless `TB_RESULT PASS`. |
| `sim/dryrun/bd_stub.sv` | Stand-in for the BD (not the Xilinx IP), used to check the testbench with Icarus without Vivado. |
| `zcu104_uart.xdc` | Implementation constraints: rxd/txd false paths (asynchronous, REQ-13/REQ-9), rxd PULLUP, txd SLEW/DRIVE (no IOB packing), bitstream compression. |
| `zcu104_uart_pins.xdc` | **txd/rxd package pins + IOSTANDARD: fill in before implementation.** |
| `list_board_pins.tcl` | Prints the UART/PMOD pins from the installed ZCU104 board file. |
| `sw/uart_apb_test.c` | Bare-metal A53 demo: banner, then echo; clears error flags. |
| `sw/build_sw.tcl` | XSCT: platform + standalone app from the XSA → `uart_apb_demo.elf`. |
| `sw/run_jtag.tcl` | XSCT: reset, program the bitstream, psu_init, download and run the ELF on A53 #0. |

## Run (in order)

```bash
source /tools/Xilinx/Vivado/2022.2/settings64.sh
source /tools/Xilinx/Vitis/2022.2/settings64.sh

# 0. optional, no Vivado: check the testbench logic against the real RTL (Icarus + stub BD)
iverilog -g2012 -o impl/board/zcu104/sim/dryrun/bd.vvp -s tb_design_1 rtl/uart_apb.v \
  impl/board/zcu104/uart_apb_bd.v impl/board/zcu104/sim/tb_design_1.sv impl/board/zcu104/sim/dryrun/bd_stub.sv
vvp -n impl/board/zcu104/sim/dryrun/bd.vvp | grep TB_

# 1. project + BD + synthesis + XSA (no pins needed)
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/build_bd.tcl -tclargs bd_only
# 2. BD testbench in XSim (PS VIP + Xilinx IP + RTL)
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/sim_bd.tcl
# 3. software
xsct impl/board/zcu104/sw/build_sw.tcl
# 4. pins, then implementation + bitstream + XSA
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/list_board_pins.tcl
#    edit impl/board/zcu104/zcu104_uart_pins.xdc
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/build_bd.tcl
xsct impl/board/zcu104/sw/build_sw.tcl            # rebuild against the XSA with bitstream
# 5. board (JTAG boot mode, USB JTAG connected)
xsct impl/board/zcu104/sw/run_jtag.tcl
```
To view the BD: `vivado impl/board/zcu104/build/uart_apb_zcu104/uart_apb_zcu104.xpr`.
Output goes to `impl/board/zcu104/build/` (git-ignored): project, `reports/`, XSA, `vitis_ws/`.

## On the board

1. Wire the UART. Choose one:
   - a 3.3 V USB-TTL adapter on the chosen PMOD pins: adapter TX → `rxd`, adapter RX ← `txd`, GND;
   - the board's PL-connected USB-UART channel, if you picked those pins.
2. Set the boot mode switch to JTAG (UG1267 boot-mode table), then power on and run step 5.
3. Open two terminals at 115200 8N1: the PS console (status messages) and the uart_apb line
   (banner, then echo of what you type).

## Software rule

The bridge turns PSLVERR into an AXI SLVERR, which is a **data abort** on the A53. The driver must
avoid every error response defined in the spec:
- check `STATUS.TX_FULL` before writing TXDATA;
- check `STATUS.RX_EMPTY` before reading RXDATA;
- never write BAUD < 4;
- never access offsets 0x14–0x1C.
`sw/uart_apb_test.c` follows these rules.

## Open items carried from the signoff report

- F010: the OOC XDC `impl/uart_apb.xdc` names `BUFGCTRL_X0Y0`, which does not exist on this part.
  It does not affect this board build, which does not use that file. It is still open for the flow
  (impl-engineer / human decision).
- F022: zero-wait-state `prdata` is combinational. In this design that path is timed
  bridge-to-bridge at 100 MHz. Check `reports/timing_summary.rpt` for the worst path through
  `uart_0`.
