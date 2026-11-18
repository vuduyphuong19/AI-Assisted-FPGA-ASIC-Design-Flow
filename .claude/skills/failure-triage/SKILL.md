---
name: failure-triage
description: How to diagnose a failing rtl_freeze gate — read the evidence, classify the symptom from values, find the first divergence, cross-check simulation against formal, match the bug catalog, and decide which artifact (rtl, tb, props, spec) is wrong, with a hypothesis that cites evidence. Use for triage-N tasks.
---

# Failure Triage

Distilled from rtl-skills `vdebug` (Apache-2.0: static trace, classification,
no code fixes) and VeriFlow-CC ideas (symptom classes, first divergence, "do not
guess the timing"). Your output decides who fixes what, so a wrong suspect costs
a whole fix iteration.

## 1. Collect (no conclusions yet)

```bash
python3 tools/mae.py evidence                 # list; note the latest FAIL/ERROR per kind
python3 tools/mae.py evidence <EV-ID> --log   # raw log of one run
```

From the logs, write down:
- **sim**: the first `CHECK_FAIL` lines — cycle, REQ, signal, expected, actual;
  which REQs fail and which pass; `COVER` bins at 0.
- **formal**: proven / counterexample (depth) / inconclusive, and which assert.
- **style / lint**: rule IDs and lines (a style or lint failure is always an RTL
  issue; no deeper triage needed).
- Prior iterations in the brief: what was suspected and what happened. Do not
  propose a hypothesis that already failed.

## 2. Classify the symptom from the values

| Class | How you recognize it |
|-------|---------------------|
| A computation | actual is plausible but wrong; no nearby cycle's expected value matches |
| B timing | actual equals the expected value of a nearby cycle (±1–3) |
| C protocol | handshake/ordering wrong: lost, duplicated or reordered transfers, stuck FSM |
| D initialization | x, 0, or wrong from the first checked cycle |
| E sequence | correct for N operations, then wrong |

Decide the class from the numbers, not from the design's shape. Pattern 9 in
`.claude/skills/rtl-coding/patterns.md` (repeated latency "fixes") comes from skipping this step.

## 3. Cross-check independent evidence

| Simulation | Formal | Most likely |
|------------|--------|-------------|
| fails REQ-k | counterexample on an assert for REQ-k | **rtl** — two independent checkers agree |
| fails REQ-k | proven | **tb** (model/sampling) or the property is weaker than the requirement — read both |
| passes | counterexample | **props** (over-strong assert or missing assume) or a corner the bench never reached — check `COVER` and the CEX depth |
| incomplete (ERROR) | any | **tb** (no SUMMARY: hang, crash, missing output lines) |
| multi-clock DUT: fails for some seeds (clock ratio/phase) only | — | **rtl** crossing (`patterns.md` P17, bug 21) if the value is lost/torn; **tb** if the check demands one fixed latency where the spec gives a bound |
| fails, with a reading the spec does not decide | — | **spec** — quote the ambiguous sentence |

## 4. Locate the root (you may read rtl, tb and props)

Start from the first divergent signal and trace backward:
1. Find the block that drives it (`always`/`assign`), evaluated in the cycle
   **before** the divergence.
2. List its inputs; decide which one must have been wrong; descend.
3. Stop at a statement that computes the wrong thing, a missing FSM arc, a
   stage-count mismatch, or a bench/property expression that contradicts the
   requirement text.

Then check `.claude/skills/rtl-coding/patterns.md` → bug catalog for a row with the same class
and symptom; a match is a lead, not a verdict.

## 5. Report

- `suspect_artifact`: one of `rtl`, `tb`, `props`, `spec`.
- One `hypothesis` claim containing: the location (file and block/line), the
  symptom class, what it does now, and the **required behavior** quoted from the
  requirement — in words, not code. Cite the evidence IDs you used
  (`evidence_refs`).
- If two artifacts are plausible, name the one the cross-check favors and state
  the alternative and what would distinguish them.

Never write code, never edit files other than your report, and never call a
failure "flaky": the runs are deterministic for a given seed.
