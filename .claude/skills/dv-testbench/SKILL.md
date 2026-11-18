---
name: dv-testbench
description: How to write the self-checking testbench dv/tb_<top>.v for this project — independent reference model from the spec, per-cycle comparison, the REQ_RESULT/COVER/CHECK_FAIL/SUMMARY output contract the runner parses, stimulus categories and coverage bins. Use for tb and tb fix tasks, and as the verification checklist when reviewing.
---

# DV Testbench

Distilled from rtl-skills (`vtestgen`, `vtestrun`; Apache-2.0) and VeriFlow-CC
ideas (golden model, per-cycle compare, first divergence). Protocol-rule checks,
error/recovery and multi-clock phases, scoreboard drain and the kill list come
from the VLSIT verification and RTL-generator skills (Apache-2.0). Adapted to this
project's runner: one testbench file, Icarus `iverilog -g2012`, results parsed
from stdout.

## 1. Independence

Derive every expected value from the requirements text. You cannot read `rtl/`
(the hook blocks it) and must not try to infer the implementation. When the spec
is ambiguous, pick a reading, record it as an `assumption` claim, and check that
reading consistently. Never weaken a check to make a run pass; a check you
believe is wrong is fixed only by changing your reading of the spec, stated in
the report.

## 2. Output contract (exact — the runner parses only these lines)

```text
REQ_RESULT <REQ-ID> <PASS|FAIL> checks=<n> errors=<n>   one per requirement; checks=0 = vacuous = FAIL
COVER <bin> <hits>                                      one per coverage bin in the spec
CHECK_FAIL <text>                                       first failures, for debugging
SUMMARY <PASS|FAIL>                                     last line; missing = incomplete run = ERROR
```

Read the seed with `$value$plusargs("seed=%d", seed)` and print it. End with
`$finish`. A watchdog that expires prints a `CHECK_FAIL watchdog …` line, then
all `REQ_RESULT`/`COVER` lines and `SUMMARY FAIL` — a hang must never look like an
incomplete run that hides which checks had run.

Make `CHECK_FAIL` lines diagnostic — triage reads them, not the waveform:

```text
CHECK_FAIL cycle=<n> REQ-<k> <signal> exp=<hex> act=<hex> <one-line context>
```

## 3. Structure

```verilog
`timescale 1ns/1ps
module tb_<top>;
    // 1. clock, reset (polarity and sync/async exactly as the spec says), seed
    // 2. DUT instance, ports by name from the spec
    // 3. reference model: behavioral, written from the requirements, updated on the
    //    same clock edge as the DUT using the inputs the DUT sampled
    // 4. checker: every cycle after reset, compare every output with the model;
    //    count checks/errors per REQ; print CHECK_FAIL for the first few errors
    // 5. coverage counters: increment a bin when its condition holds at an edge
    // 6. stimulus phases (§4), driven on the falling edge so inputs are stable at posedge
    // 7. report: REQ_RESULT per REQ, COVER per bin, SUMMARY; $finish
endmodule
```

Rules that keep the bench honest:
- **Model, don't hardcode.** Expected values come from the model, so random
  stimulus is checked too. Directed expectations are for spec examples only.
- **Per-cycle comparison** of every output, not only end values: the first
  `CHECK_FAIL` then pinpoints the cycle where behavior diverged.
- **Sampling**: drive inputs at `negedge`; compare at the next `negedge` (after
  the `posedge` update). A `reg` output reflects the state computed at the
  previous edge; a `comb` output reflects current inputs and state. Model both
  the same way the spec states them.
- **Every requirement has checks**: map each REQ to the comparisons that prove
  it, and count them under that REQ. A REQ whose condition never occurred is
  vacuous — add stimulus until `checks > 0`.
- Testbench code may use SystemVerilog constructs Icarus supports, but keep to
  plain Verilog where possible; avoid classes and `program` blocks. Declare
  tasks `automatic` when they keep local state (a static local keeps its value
  between calls — a common bench bug).
- **Protocol rules as checks.** When a port follows a standard protocol named in
  the spec (APB, AXI, valid/ready), check its rules every cycle — request held
  until accepted, address/data stable while stalled, response only after a
  request, no X on a valid signal — and count each rule's activations. A rule
  whose condition never occurred proved nothing: add stimulus, and print its
  activations as an extra `COVER` line.
- **Stimulus through ports only.** Never `force` or hierarchically write a DUT
  signal; drive the state through legal inputs.
- **Drain at the end.** Every expected transaction the model still holds when
  the test ends is a failure (lost or stuck data), not a pass.

## 4. Stimulus phases (all mandatory unless the DUT makes one meaningless — say so)

| Phase | Covers |
|-------|--------|
| Reset | outputs at their reset values; reset mid-operation clears state |
| Basic | one directed scenario per requirement / mode |
| Edge | 0, all-ones, max-1, max; empty and full; single beat |
| Back-pressure / boundary | operations attempted when not allowed (write when full, read when empty, stalled handshake ≥ 2 cycles) |
| Concurrency | simultaneous events (read+write, set+clear) in every state |
| Error / recovery | every error or illegal-input case the spec defines gets its response checked, **and** the next legal operation is checked to start clean |
| Random stress | seeded random ops for ≥ 1000 cycles, biased to visit boundaries |
| Multi-clock (only with > 1 clock) | derive each clock's period and phase offset from the seed; cover fast→slow and slow→fast; a latency check accepts the bound the spec states, never one fixed value |

Run the phases in this order: one focused scenario per requirement first,
combined scenarios after, so a failure points at one function. For random
stimulus write the legal range of each random field next to its constraint;
drive the boundary values explicitly — random alone rarely reaches max.

## 5. Coverage

One counter per bin named in the spec, incremented exactly when the bin's
condition happens at a clock edge (e.g. `write_when_full`: `wr_en && full` at
posedge). Add bins the spec implies but does not list (FSM states, handshake
stall) as extra `COVER` lines — the gate checks the spec's bins; extra bins help
review. A bin with 0 hits fails the gate: fix the stimulus, not the bin.

## 6. Before you submit

1. Every REQ in the spec has a `REQ_RESULT` line with `checks > 0`.
2. Every spec bin has a `COVER` line.
3. Seed printed and used; the run ends with `SUMMARY` and `$finish`.
4. No expected value was copied from a failing run.
5. Assumptions about ambiguous requirements are listed as claims.
6. **Kill list.** For each REQ, name one plausible wrong design and the check
   that would catch it: output stuck at 0, flag one cycle late, `>` instead of
   `>=` at the limit, wrap off by one at depth−1, write accepted while full,
   clear that misses one register. If no check catches one, add the check.
   (In the VLSIT FIFO bench, 15 of 16 such mutants were caught; the one missed
   had no check.) List the kill list in your report as `unverified` claims: no
   mutant was actually run.
7. You cannot run the simulation yourself unless the brief allows it; results
   are cited only by evidence ID from `python3 tools/mae.py run sim`.

## 7. tb fix tasks

If triage suspects the testbench, the failing `CHECK_FAIL` lines and the triage
hypothesis are in the brief. Re-read the requirement the check implements. If
your model or sampling was wrong, fix it and say which requirement reading
changed. If the check was right, say so — do not alter it to agree with the DUT.
