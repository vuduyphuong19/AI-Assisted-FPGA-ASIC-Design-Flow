# NOTICE

## rtl-skills — Apache License 2.0
https://github.com/phamcuong21478/rtl-skills — Copyright the rtl-skills authors.
Licensed under the Apache License, Version 2.0: http://www.apache.org/licenses/LICENSE-2.0

The following files contain material **selected, condensed and modified** from rtl-skills
(`skills/shared/CodingStyle.md`, `DesignPatterns.md`, and the `vdesign`, `vfill`, `vtestgen`,
`vtestrun`, `vdebug`, `vsynth` skills):
- `.claude/skills/rtl-coding/SKILL.md`, `.claude/skills/rtl-coding/patterns.md`
- `.claude/skills/dv-testbench/SKILL.md`
- `.claude/skills/failure-triage/SKILL.md`
- `.claude/skills/vivado-flow/SKILL.md`
- `mae/style.py` (rule set derived from CodingStyle; code is new)
- `mae/vivado.py` (flow order derived from vsynth, with fixes; code is new)

## VLSIT skill library — Apache License 2.0
VLSIT ASIC/SoC AI skills (`vlsit-*`; RTL rules from
https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model) — Copyright the VLSIT authors.
Licensed under the Apache License, Version 2.0: http://www.apache.org/licenses/LICENSE-2.0

Method **selected, condensed and rewritten** (no RTL, scripts or rule text copied; its
SystemVerilog/asynchronous-reset ASIC rules were not adopted because they conflict with this
project's Verilog-2005 synchronous-reset style) into:
- `.claude/skills/rtl-coding/SKILL.md`, `patterns.md` (P17–P19, bugs 18–22: CDC mechanism
  catalogue, mutation results of the VLSIT FIFO benches)
- `.claude/skills/dv-testbench/SKILL.md` (protocol-rule checks, error/recovery and multi-clock
  phases, scoreboard drain, kill list)
- `.claude/skills/vivado-flow/SKILL.md`, `board-reference.md` (constraint precedence and
  anti-patterns, from `vlsit-sdc-timing-review` and the AsyncAPBBridge constraints)
- `.claude/skills/failure-triage/SKILL.md` (multi-clock row)
- `.claude/skills/spec-writing/SKILL.md` (from `vlsit-asic-ip-microspec`, `vlsit-ip-spec-review`)

## VeriFlow-CC — ideas only
https://github.com/bjwanneng/veriflow-cc has no license file; no text or code is copied.
Re-implemented ideas: symptom classes and the recurring-bug catalog, per-cycle comparison and
first divergence, timing-contract wording (reg/comb outputs), "do not guess timing" triage rule.

## xilinx-skill — ideas only, rewritten
https://github.com/QingquanYao/xilinx-skill (README states MIT; the repository has no LICENSE file).
No text is copied. Topics taken from its references (`xdc_constraints.md`, `vu9p_guide.md`,
`vivado_guide.md`, `tcl_commands.md`, `official-docs/index.md`) were rewritten in
`.claude/skills/vivado-flow/board-reference.md` and `SKILL.md`, with corrections: CDC constraints
now require ASYNC_REG and bounded `set_max_delay -datapath_only` for buses instead of blanket false
paths; the timing-closure order puts RTL fixes before constraint relaxation; the document index fixes
UG912 (Properties Reference) and DS890/DS925. New deterministic checks in `mae/vivado.py`
(check_timing, report_cdc, XDC audit) and `mae/style.py` (R16) enforce the guidance.
