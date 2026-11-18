# RTL Patterns and Bug Catalog

Condensed from rtl-skills `DesignPatterns.md` (P1–P16, Apache-2.0) and the
VeriFlow-CC bug catalog (ideas, rewritten). P17–P19 and bugs 18–22 are drawn
from the VLSIT skill library (Apache-2.0): its CDC mechanism catalogue and the
mutants its FIFO testbenches had to catch. Each entry: when it applies and the
one decision people get wrong. Name the pattern in your report when you use it.

## Reflex triggers — apply the moment the design commits to…

| …this structure | apply |
|-----------------|-------|
| a RAM/array read before it is guaranteed written | P4 preclear |
| a RAM read-modify-written by key (counter, table, accumulator) | P8 forwarding |
| on-chip storage whose size should pick the primitive | P9 → P10 |
| a pipelined datapath carrying side-band (`valid`, id, last) | P10 |
| a CPU/register bus | P6 (+ P5 for W1C bits) |
| a slow strobe or divided "clock" input | P7 (edge-detect enable, never a second clock) |
| an array crossing a Verilog-2005 port | P12 |
| any signal sampled in a clock domain other than the one that launched it | P17 (pick the mechanism per crossing) |
| a FIFO, ring buffer or counter whose depth/limit is not a power of two | P18 |
| a flush, soft reset or "clear" input besides `rst_n` | P19 |

## Patterns

| # | Pattern | Use when | The decision that matters |
|---|---------|----------|---------------------------|
| P1 | Stream buffer | a valid/ready stream needs decoupling | Use a FIFO for anything that must hold beats under back-pressure; a single register slice (`s_ready = !m_valid_ff \|\| m_ready`) only to cut a long path |
| P2 | Arbiter (priority / round-robin) | N requesters, one resource | Qualify the grant with downstream `ready`; `?` only in `casez` patterns; for many requesters, round-robin over fixed-size groups |
| P3 | Counter + strobe | period, divide-by-N, address walk | Limit from a parameter; a counter that must never read 0 resets to 1; reload-on-event is explicit |
| P4 | RAM preclear | RAM must start in a known state | Hold off traffic and `ready` until `init_done`; the clear value is the spec's reset value, not always 0 |
| P5 | Sticky / W1C bit | a one-shot event must not be missed | **Set beats clear** in the same cycle; for polled must-not-miss events expose the level, not a clear-on-read flag |
| P6 | CSR decode | registers on a CPU bus | Unmapped reads return 0; read data 0 when not selected; derive a one-cycle write strobe; reset to spec defaults |
| P7 | Pulse ↔ level | act once on an edge | Reset the history flop so no phantom edge leaves reset |
| P8 | RAM read-during-write forwarding | same key re-read within the RAM latency | One bypass per in-flight stage; compare address **and** write/read enables |
| P9 | Primitive by size | depth-parameterized storage | Derive read latency from the chosen primitive (regs/LUT-RAM 0, BRAM 1) and propagate it |
| P10 | Latency-matching delay line | side-band must align with datapath | Tie the depth to the same parameter as the datapath latency, never a hand count |
| P11 | Up/down occupancy | fill and drain from two events | Simultaneous inc+dec nets to zero — handle it explicitly |
| P12 | Port arrays as flat buses | array through a port | Same `W*i +: W` stride on both sides; unflatten immediately inside |
| P13 | Persistency / debounce | declare after N samples | N-consecutive (reset on disagreement) or hysteresis; never drive the status from the raw condition |
| P14 | LFSR / PRBS / CRC | polynomial sequence | Non-zero seed; polynomial is a parameter |
| P15 | Wide reduction | parity, popcount, error count | Purely combinational fold, register only the result; size the sum `$clog2(W+1)` |
| P16 | Windowed counter | count per window | Latch and clear in the same cycle so no event is lost between read and clear |
| P17 | CDC mechanism | a signal crosses clock domains | One mechanism per crossing, chosen from the table below and named in your report; the source is a flop in its own domain with no logic before the first sync stage |
| P18 | Non-power-of-two wrap | depth/limit `N` not `2^k` | Wrap explicitly (`ptr == N-1 ? 0 : ptr+1`) and derive full/empty from a count or wrap bit, never from address bits alone; test `N-1 → 0` |
| P19 | Secondary clear (flush / soft reset) | a clear other than `rst_n` | Clear **every** register `rst_n` clears that the spec says the clear affects, list them; define its priority over a same-cycle read/write; no output pulse (overflow, valid) for an operation the clear blocked |

A small Moore FSM is not a pattern: build it per SKILL.md §4.

### P17 — which crossing mechanism

| Crossing | Mechanism | Fails when assumed wrongly |
|----------|-----------|----------------------------|
| Single-bit **level**, held ≥ 2 destination cycles | 2-flop synchronizer `x_meta_ff → x_sync_ff` | a pulse shorter than a destination cycle is lost |
| Single-bit **pulse** / event | toggle flop in the source, 2-flop sync, edge detect (XOR of last two) in the destination | a pulse sent straight through 2 flops is lost or doubled |
| Multi-bit **data**, occasional | req/ack handshake; data held stable from req until ack returns | data that changes before ack is captured torn |
| Multi-bit **counter / pointer** | gray code, registered in the source, one synchronizer per bit | a binary value can be sampled as a value that never existed |
| **Stream** of data | async FIFO (gray pointers both ways) | — the full/empty seen in each domain is conservative (late), never optimistic |
| **Quasi-static** config | no synchronizer, only if the spec says it is stable while the destination is active | software rewrites it at run time |

A signal named `*_sync*` without the flop chain visible is not a synchronizer. In
simulation a crossing has a range of latencies (it depends on clock phase), so the
spec — and any check — states a latency bound, not one value.

## Bug catalog (use when self-checking and when triaging)

Symptom classes: **A** computation (plausible wrong value), **B** timing (right
value, wrong cycle), **C** protocol (handshake/FSM sequencing), **D**
initialization (x / 0 / wrong from the first cycle), **E** sequence (right for N
rounds, then wrong).

| # | Bug | Class | Symptom | Prevention |
|---|-----|-------|---------|------------|
| 1 | Latch-then-consume race | D | first transaction after reset wrong | same-cycle data on the input path, not a parent register |
| 2 | Sliding window drains | E | right for N rounds, then zeros | replenish every active cycle |
| 3 | Incomplete initial state | D | off by a constant on the first operation | load every register the result depends on |
| 4 | Off-by-one pipeline delay | B | value appears one cycle early/late | count `_ff` stages against the stated latency |
| 5 | Output not reset | D | x/stale output after reset | reset every control register driving an output |
| 6 | FSM stuck / missing arc | C | handshake never completes, watchdog | transition table covers every stimulus; `default` recovery |
| 7 | Handshake violation | C | lost/duplicated beats under back-pressure | `valid` and data hold until accepted |
| 8 | Counter range off-by-one | A/B | one beat too many/few | `<` vs `<=` and `LIMIT` vs `LIMIT-1` consistent; test 0, max-1, max |
| 9 | Premature timing hypothesis | — | repeated latency "fixes" that do not converge | if no nearby expected value matches, it is class A, not B |
| 10 | Finalize reads next-round wires | A | result = one extra round | DONE reads `_ff` only |
| 11 | Chaining state not re-initialized | D | 2nd stream wrong after a 1st stream | first-block path reloads chaining registers too |
| 12 | Capture on the wrong transition | B/D | flag a cycle late or lost | capture in the same clocked block, keyed on the transition condition |
| 13 | Concatenation width truncation | A | upper bits zero or shifted | size every `{}` member; no silent `WIDTH` waivers |
| 14 | Multi-block valid not gated | C | spurious output valid between blocks | gate output `valid` with "last block" |
| 15 | Bench sampling skew | B | bench fails, waveform right one delta later | sample `reg` outputs after the edge, `comb` outputs in-cycle |
| 16 | Lost one-shot event | C | interrupt/done occasionally missed | P5 set-over-clear |
| 17 | Overflow/underflow not blocked | A | count wraps, data overwritten | gate writes with `!full`, reads with `!empty` (the FIFO example's bug) |
| 18 | Non-power-of-two wrap off by one | E | correct until the first wrap, then data/flags wrong | P18; count stages `N-1 → 0` by hand |
| 19 | Flag from the wrong pointer | B | `empty`/`full`/fall-through data one cycle late or early | decide whether a flag/read address uses the current or the next pointer and write it in the plan |
| 20 | Secondary clear misses a register | D/E | after flush/soft reset, one stale pointer, flag or busy bit | P19 list; compare it with the `rst_n` branch |
| 21 | Pulse or bus crossed as a level | C/A | event lost or value torn, depending on clock ratio/seed | P17 table |
| 22 | Threshold compare `>` vs `>=` | A/B | flag one word late at the boundary | write the boundary case (`count == THR`) in the plan and test it |
