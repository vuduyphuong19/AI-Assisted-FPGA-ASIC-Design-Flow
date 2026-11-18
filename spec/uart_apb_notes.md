# uart_apb: specification notes (SPEC-UART-APB-1.0)

Companion to `spec/requirements.json`, written for the human reviewer. Agents receive **only the
JSON** in their brief, so everything binding is in the JSON. This file explains it and lists the
decisions to confirm before `approve spec_baseline`.

## 1. Overview

```
            APB3 slave (clk domain)                      UART
 psel ──┐  ┌──────────────┐  push  ┌─────────┐  pop  ┌──────────┐
 penable┼─▶│ register     │───────▶│ TX FIFO │──────▶│ TX shift │──▶ txd (flop)
 pwrite │  │ decode       │        │ 4 x 8b  │       │ + baud   │
 paddr  │  │ CTRL, BAUD,  │        └─────────┘       └──────────┘
 pwdata ┘  │ STATUS (W1C) │  pop   ┌─────────┐ push  ┌──────────┐   ┌────────┐
 prdata ◀──│              │◀───────│ RX FIFO │◀──────│ RX shift │◀──│ 2FF    │◀── rxd (async)
 pready ◀──│              │        │ 4 x 8b  │       │ mid-bit  │   │ sync   │
 pslverr◀──└──────┬───────┘        └─────────┘       └──────────┘   └────────┘
                  └──▶ irq
```

- One clock (`clk` = PCLK), synchronous active-low reset (project style; APB PRESETn sampled on
  the clock edge).
- Frame: 8 data bits, LSB first, optional even/odd parity, 1 stop bit. 115200 baud at 100 MHz
  after reset (BAUD = 868).
- `rxd` is the only asynchronous input. Synchronizer `*_meta_ff`/`*_sync_ff` with ASYNC_REG
  (pattern P17 level / style R16). There is no clock crossing, so the XDC needs no CDC
  constraint. Only `rxd` has a false path at the board level (see §6).

## 2. Register map

| Offset | Name | Access | Bits | Reset |
|--------|------|--------|------|-------|
| 0x00 | TXDATA | W | [7:0] byte to send; write when TX_FULL → dropped, PSLVERR | — (reads 0) |
| 0x04 | RXDATA | R (pop) | [7:0] oldest byte; read when RX_EMPTY → 0, PSLVERR | 0 |
| 0x08 | STATUS | R, W1C [7:5] | [0] TX_FULL [1] TX_EMPTY [2] RX_FULL [3] RX_EMPTY [4] TX_BUSY [5] RX_OVERRUN [6] FRAME_ERR [7] PARITY_ERR | 0x0A |
| 0x0C | CTRL | RW | [0] TX_EN [1] RX_EN [2] PARITY_EN [3] PARITY_ODD [4] IE_RXNE [5] IE_TXE [6] IE_ERR | 0x00 |
| 0x10 | BAUD | RW | [15:0] D = clk cycles per bit, D ≥ 4 (write < 4 → ignored, PSLVERR) | 868 |
| 0x14–0x1C | — | — | PSLVERR, reads 0 | — |

Baud examples at 100 MHz: 9600 → 10417, 115200 → 868, 921600 → 109, 1 Mbaud → 100.

## 3. Cycle tables

APB write (no wait state; REQ-2):

| Cycle | psel | penable | pwrite | pready | Effect |
|-------|------|---------|--------|--------|--------|
| 0 setup | 1 | 0 | 1 | 1 | — |
| 1 access | 1 | 1 | 1 | 1 | pslverr valid; register/FIFO updated at the end of this cycle |
| 2 | 0 | 0 | x | 1 | new value visible |

APB read: same phases. prdata and pslverr are valid in cycle 1, prdata = 0 otherwise. For RXDATA
the pop happens at the end of cycle 1.

TX frame, D cycles per bit, PARITY_EN = 1 (REQ-8, 9, 10). The start bit begins at most 3 cycles
after the push edge:

| Bit | start | d0 | d1 | … | d7 | parity | stop |
|-----|-------|----|----|---|----|--------|------|
| txd | 0 | data[0] | data[1] | … | data[7] | even/odd | 1 |
| cycles | D | D | D | | D | D | D |

RX (REQ-13–15): detect 1→0 on the synchronized rxd, check the start bit at D/2 (`D >> 1`, R13
forbids a variable divide), then sample every D cycles. Two to three cycles of synchronizer
delay fall inside the ±D/4 sampling margin. The byte is stored at most 6 cycles after the
nominal end of the stop bit.

## 4. Decisions in the JSON — confirm before approving

Each decision has alternatives. To change one, edit the REQ and re-hash (approve again).

| Q | Decision taken (REQ) | Alternatives |
|---|----------------------|--------------|
| Q1 | Write to TXDATA when full: drop + PSLVERR (REQ-6) | B. drop silently + sticky TX_OVF bit · C. wait states (PREADY=0) until space |
| Q2 | RX frame with framing/parity error: byte **dropped**, sticky flag (REQ-18, 19) | B. store the byte, with an error bit per FIFO entry (16550 style) |
| Q3 | RX FIFO full: new byte dropped, RX_OVERRUN (REQ-17) | B. overwrite the oldest byte |
| Q4 | Read RXDATA when empty: PSLVERR (REQ-16) | B. return 0 without an error |
| Q5 | No wait states, pready = 1 (REQ-2) | B. one wait state on every read (registered prdata, better timing) |
| Q6 | FIFO depth 4 (parameter) | 8/16: more area; 4 keeps formal and coverage short |
| Q7 | 1 stop bit, 8 data bits only | B. CTRL bits for 2 stop bits / 7 data bits (more REQs) |
| Q8 | Receiver tolerance ±2 % for D ≥ 16 (REQ-13) | B. 16× oversampling with majority vote (more robust against noise, more logic) |
| Q9 | No loopback mode | B. CTRL[7] LOOPBACK: txd→rx internally, useful for board self-test |

## 5. Gap checklist (spec-writing §3)

| Topic | Status |
|-------|--------|
| Reset | stated: REQ-1 (outputs, registers, frame in progress) |
| Full/empty | stated: REQ-6, 7, 16, 17 |
| Latency | stated: APB REQ-2, TX start ≤ 3 (REQ-10), RX store ≤ 6 (REQ-15), irq ≤ 1 (REQ-23) |
| Limits | stated: DIV_MIN (REQ-22), FIFO_DEPTH power of two (REQ-6) |
| Concurrency | stated: push-on-full vs pop (REQ-6), overrun vs read (REQ-17), error vs clear (REQ-20) |
| Illegal input | stated: unmapped address (REQ-4), BAUD < 4 (REQ-22); APB protocol violations explicitly undefined (REQ-2) |
| Timeout | not required: UART has no response handshake; an idle line is legal |
| Crossings | stated: rxd asynchronous, 2FF synchronizer (port note); no clock crossing |
| Secondary clear | stated: TX_EN/RX_EN disable (REQ-11, 21); no FIFO flush (not required, reset clears) |

## 6. Implementation and FPGA

- Target board: **ZCU104**, part `xczu7ev-ffvc1156-2-e`. The flow (`mae run synth/impl`) runs
  **out-of-context** on that part at 100 MHz. These numbers are what the timing gate decides on.
- Running the block on the board needs a top wrapper, which the mae runner does not build yet
  (`vivado-flow` §6), so it comes after signoff. The wrapper needs:
  - an MMCM from the 125 MHz differential PL user clock to 100 MHz;
  - an APB master: the Zynq PS through an AXI-to-APB bridge, or a small PL test sequencer;
  - `txd`/`rxd` routed to the USB-UART bridge channel that reaches the PL, or to a PMOD with a
    USB-UART adapter. Take the pins from the ZCU104 board files / UG1267, not from memory;
  - a board XDC with pins, IOSTANDARD and
    `set_false_path -from [get_ports rxd]  ;# REQ-13 rxd asynchronous, 2FF synchronizer`. The mae runner
  does not build bitstreams yet (`vivado-flow` §6). That is a separate step after signoff.
