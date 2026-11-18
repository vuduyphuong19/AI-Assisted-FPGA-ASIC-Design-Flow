# ic-agents: a Claude Code agent team for digital IC design

An orchestrator-worker team of [Claude Code](https://docs.claude.com/en/docs/claude-code) subagents
that takes a digital block from **specification to RTL, verification, FPGA implementation and a
running board**, with tool-backed evidence at every step.

- The main session runs as **`ic-orchestrator`** and delegates every engineering task to specialist
  subagents (`.claude/agents/`), each preloaded with a method **skill** (`.claude/skills/`).
- A small Python CLI, **`tools/mae.py`**, owns the workflow state, runs the EDA tools, signs evidence
  and evaluates gates. Agents never judge results themselves.
- **Hooks** (`.claude/hooks/`) enforce write scopes, independence barriers between design and
  verification, brief integrity, report validity and honest stopping.

**Reference project in this repository:** `uart_apb`, an APB3 UART (23 requirements), taken through
the whole flow and run on a **ZCU104** board (Zynq UltraScale+). The full walkthrough, results and
lessons learned are in **[docs/FPGA_FLOW.md](docs/FPGA_FLOW.md)**.

| Document | Content |
|----------|---------|
| [docs/FPGA_FLOW.md](docs/FPGA_FLOW.md) | The FPGA flow stage by stage, with the uart_apb results, lessons and the ASIC outlook |
| [CLAUDE.md](CLAUDE.md) | Rules every agent follows (evidence, typed claims, ownership, human decisions) |
| [impl/board/zcu104/README.md](impl/board/zcu104/README.md) | Board integration: Block Design, BD testbench, software, JTAG |
| [spec/uart_apb_notes.md](spec/uart_apb_notes.md) | Human-readable notes for the reference spec |
| [NOTICE.md](NOTICE.md) | Third-party material the skills are distilled from |

---

## Contents

1. [How it works](#how-it-works)
2. [The team: agents and skills](#the-team-agents-and-skills)
3. [Requirements](#requirements)
4. [Quick start (this repository)](#quick-start-this-repository)
5. [Start a new project with these agents and skills](#start-a-new-project-with-these-agents-and-skills)
6. [Customising the team](#customising-the-team)
7. [Writing the spec from source documents](#writing-the-spec-from-source-documents)
8. [Human decisions](#human-decisions)
9. [Board integration](#board-integration)
10. [Testing the control plane without a model](#testing-the-control-plane-without-a-model)
11. [Repository layout](#repository-layout)
12. [Maintenance sessions](#maintenance-sessions)
13. [Known limits](#known-limits)
14. [License and credits](#license-and-credits)

---

## How it works

```
human ──approve──▶ .mae/decisions.jsonl
                         ▲
ic-orchestrator (main thread, claude --agent / settings "agent")
   │  mae next ──▶ ready actions        mae submit ◀── reports/<task>.json
   │  mae brief ─▶ Agent(subagent, prompt=brief)  [guard_spawn.py checks BRIEF-ID]
   ▼
rtl-designer   dv-engineer   formal-engineer   debug-triage   design-reviewer   impl-engineer
   │ Write/Edit/Read  [guard_tools.py: write scope, read barriers, Bash allow-list]
   │ mae run <tool>  ──▶ runner ──▶ .mae/ledger.jsonl (HMAC-signed, hash-chained)
   └ SubagentStop [check_report.py: report must validate before the subagent may finish]
```

The workflow template (one block):

```
spec_baseline (human)
   ├─▶ rtl (rtl-designer) ─┬─▶ style, lint ─────────┐
   ├─▶ tb (dv-engineer) ───┼─▶ sim ─────────────────┼─▶ rtl_freeze ─▶ synth ─▶ impl ─▶ review ─▶ signoff (human)
   ├─▶ props (formal-eng.) ┴─▶ formal ──────────────┘        ▲          (Vivado; or synth ─▶ sta with yosys)
   └─▶ constraints (impl-engineer, Vivado only) ─────────────┘
            failing run ─▶ triage-N (debug-triage) ─▶ fix-N (owner of the faulty artifact) ─▶ re-run
```

Core rules (details in [CLAUDE.md](CLAUDE.md)):

- **Truth comes from tools.** Results exist only as evidence records (`EV-...`) written by the
  runner. Missing evidence, `NOT_RUN` and `ERROR` are failures. Agreement between agents is not
  evidence.
- **Typed claims.** `hypothesis | assumption | estimate | unverified | tool_result`; only a passed
  gate creates a verified fact.
- **Independence.** RTL, testbench and properties are written from the approved spec only; the
  hooks stop their authors from reading each other's files.
- **Humans decide.** Spec baseline, waivers, budget extensions and signoff are
  `python3 tools/mae.py approve ...`, which agents cannot run.

---

## The team: agents and skills

| Agent | Model | Skills | Writes | Role |
|-------|-------|--------|--------|------|
| `ic-orchestrator` | sonnet | - | nothing | Main session. Follows `mae next`, briefs and spawns subagents, submits reports. Never judges results. |
| `spec-analyst` | opus | `spec-writing` | `spec-drafts/`, `reports/spec-*.json` | Turns source documents into atomic, testable requirements for human approval |
| `rtl-designer` | sonnet | `rtl-coding` | `rtl/` | Synthesizable RTL |
| `dv-engineer` | opus | `dv-testbench` | `dv/` | Self-checking testbench with reference model and coverage |
| `formal-engineer` | opus | `verification-code-style` (your own, see below) | `formal/` | Formal properties (Yosys k-induction) |
| `impl-engineer` | sonnet | `vivado-flow` | `impl/` | XDC constraints, implementation scripts |
| `debug-triage` | opus | `failure-triage` | `reports/triage-*.json` | Decides which artifact is wrong when a run fails |
| `design-reviewer` | opus | `rtl-coding`, `dv-testbench`, `vivado-flow` | `reports/review.json` | Independent adversarial review of every artifact, claim and evidence record |
| `project-maintainer` | opus | - | anything (human-supervised) | Maintains the framework itself; not part of the flow |
| `code-searcher` | haiku | - | nothing | Read-only `path:line` search |

| Skill | Content |
|-------|---------|
| `rtl-coding` (+ `patterns.md`) | plan-before-code, hard rules checked by `mae run style` (R01–R16), Vivado-friendly RTL, patterns P1–P19, bug catalog |
| `dv-testbench` | reference model, per-cycle compare, output contract, stimulus phases, coverage |
| `failure-triage` | symptom classes, sim-vs-formal cross-check, root location, suspect decision |
| `vivado-flow` (+ `board-reference.md`) | XDC and CDC rules, the runner's Vivado flow and checks, reading synth/impl evidence, timing-closure order, board I/O, IP, official docs |
| `spec-writing` | reading and citing `spec/sources/`, atomic REQs, port/parameter/error/crossing facts, gap checklist, open points as options, coverage bins |

---

## Requirements

- **Claude Code** with subagents and hooks.
- **Python 3.10+** (standard library only).
- **Icarus Verilog** (sim), **Verilator** (lint), **Yosys** (formal; `yosys` or
  `pip install yowasp-yosys`).
- **Back end, chosen at `init`:**
  - `--impl-tool vivado` (default): Vivado synthesis + place and route, `vivado` on `PATH`.
  - `--impl-tool yosys`: generic synthesis + optional OpenSTA.
  Without the tool, the synth/timing gates fail with `NOT_RUN`, as they should.
- **Board integration only:** Vivado + Vitis (tested with 2022.2) and the ZCU104 board files.

Tool versions used for the reference project: Icarus Verilog 11.0, Verilator 4.038, Yosys 0.71,
Vivado/Vitis 2022.2.

---

## Quick start (this repository)

```bash
git clone <this repo> ic-agents && cd ic-agents
python3 -m unittest discover -s tests            # control plane works (no model, no EDA needed)
python3 tools/mae.py init --impl-tool vivado --part xczu7ev-ffvc1156-2-e   # reads spec/requirements.json
python3 tools/mae.py approve spec_baseline --signer you --reason "baseline reviewed"   # human
claude                                           # settings.json makes ic-orchestrator the main agent
> Run the flow.
```

Headless/CI: `claude -p "Run the flow until it needs a human." --agent ic-orchestrator`.

`.mae/` (state, signed ledger, runner key) and `reports/` are generated per checkout and are not in
git, so a fresh clone re-runs the flow from the spec baseline.

---

## Start a new project with these agents and skills

The repository has two parts. Copy the **framework**; replace the **project** part.

| Framework (reuse as is) | Project (specific to one block) |
|-------------------------|----------------------------------|
| `CLAUDE.md`, `.claude/` (agents, skills, hooks, settings, policy) | `spec/` (requirements, notes, sources) |
| `mae/`, `tools/` (engine and CLI) | `rtl/`, `dv/`, `formal/`, `impl/` (agent outputs) |
| `tests/`, `examples/` (control-plane tests, replay fixtures) | `reports/`, `.mae/` (generated) |
| `.gitignore`, `NOTICE.md`, `LICENSE-rtl-skills` | `impl/board/<board>/` (board integration) |

The tests use temporary directories and the FIFO fixture in `examples/`, so they do not depend on
any project files and keep working in a new project.

### Option A: new repository from this one

```bash
git clone <this repo> my-block && cd my-block
rm -rf .git spec rtl dv formal impl reports .mae
mkdir -p spec/sources rtl dv formal impl reports
git init
```

### Option B: add the team to an existing directory

```bash
SRC=/path/to/ic-agents
cd /path/to/my-block
rsync -a --exclude __pycache__ \
  $SRC/CLAUDE.md $SRC/.claude $SRC/mae $SRC/tools $SRC/tests $SRC/examples \
  $SRC/.gitignore $SRC/NOTICE.md $SRC/LICENSE-rtl-skills .
mkdir -p spec/sources rtl dv formal impl reports
```

### Then, in both cases

```bash
python3 -m unittest discover -s tests                 # framework intact: "OK"

# 1. spec: draft it from your sources, or write it by hand in the same shape as
#    spec/requirements.json of this repo or examples/fifo-scripted/requirements.json
claude --agent spec-analyst                           # -> spec-drafts/requirements.json + notes
cp spec-drafts/requirements.json spec/requirements.json   # after your review

# 2. initialise: file names come from "block", the clock period from ppa_targets.fmax_mhz
python3 tools/mae.py init --impl-tool vivado --part <your part>   # or --impl-tool yosys
python3 tools/mae.py status                           # waiting_for_human: spec_baseline

# 3. approve and run
python3 tools/mae.py approve spec_baseline --signer you --reason "baseline reviewed"
claude
> Run the flow.
```

This sequence was dry-run on a copy of the framework with the FIFO spec: the tests pass, `init`
derives `rtl/sync_fifo.v`, `impl/sync_fifo.xdc` and a 5.0 ns period, and the flow waits for
`spec_baseline`.

### New-project checklist

- [ ] `spec/requirements.json` has `block`, `parameters`, `ports`, `requirements` (`REQ-n`, atomic),
      `coverage_bins` and `ppa_targets.fmax_mhz` (and `part` for FPGA).
- [ ] `init --part` matches the target device. Board/part-specific constraints (such as
      `HD.CLK_SRC` sites) must be read from the device, not copied from another family.
- [ ] The `formal-engineer` skill exists on this machine (next section).
- [ ] Tools on `PATH`: `iverilog`, `verilator`, `yosys`, plus `vivado` for the Vivado back end.
- [ ] For a board: copy `impl/board/zcu104/` as a template and change the part, board part, pins,
      address map and software (see its README).

---

## Customising the team

**The `formal-engineer` skill.** `formal-engineer` preloads `verification-code-style`, a personal
skill that is not in this repository. On a new machine either put your own skill in
`~/.claude/skills/verification-code-style/SKILL.md` (or in `.claude/skills/`), or change the
`skills:` list in `.claude/agents/formal-engineer.md`.

**Change the target or back end.** Re-run `init` with `--impl-tool`, `--part`, `--period-ns`
(if the spec has no `fmax_mhz`), `--logic-level-budget` or `--directive` (Vivado place/route
directive). The Vivado checks live in `mae/vivado.py` and `_vivado_parse` in `mae/runner.py`.

**Improve a skill.** Edit `.claude/skills/<skill>/SKILL.md` (method) and its reference files. Keep
rules that a tool can check in the tool (`mae/style.py`, `mae/vivado.py`) and reference them from
the skill. Run the tests afterwards.

**Add or rename an agent.** An agent is wired in several places; change all of them:

1. `.claude/agents/<name>.md`: frontmatter (`name`, `description`, `tools`, `model`, `skills`) and
   the prompt.
2. `.claude/mae-policy.json`: a role entry with its `write` scope and `read_deny` barriers.
3. `.claude/agents/ic-orchestrator.md`: add the name to `tools: Agent(...)` so it can be spawned.
4. `mae/flow.py`: a `Task` in `Project._template()` (and `OWNER` if it owns a fixable artifact);
   `mae/briefs.py` if it needs extra brief context.
5. A test in `tests/` for its scope (see `TestImplPolicy` in `tests/test_vivado_style.py`).

**Add a flow stage** (for example place and route for ASIC): add tool tasks and gates in
`Project._template()` and `Project.gate()`, a `ToolSpec` and parser in `mae/runner.py`, and a
signoff check in `signoff_checks()`.

**Keep updates from this repository** in a project created with Option A or B:

```bash
git remote add upstream <this repo>
git fetch upstream
git checkout upstream/main -- .claude mae tools tests examples CLAUDE.md   # framework only
python3 -m unittest discover -s tests
```

Editing `.claude/hooks/`, `settings.json` or `mae-policy.json` changes what every agent may do;
review those diffs carefully.

---

## Writing the spec from source documents

Put the sources in `spec/sources/`: Markdown (`.md`), PDF (`.pdf`), and draw.io diagrams
(`.drawio` or `.drawio.svg`; export `.drawio.png` as one of those). The spec-analyst reads Markdown
and PDF directly. Diagram pages are usually compressed, so create a text extract for each diagram
and re-run it whenever the diagram changes:

```bash
python3 tools/drawio_text.py spec/sources/fifo_block.drawio > spec/sources/fifo_block.drawio.md
```

The extract lists shapes and connectors per page and flags connectors that are not attached to a
shape. Then ask `claude --agent spec-analyst` for a draft: `spec-drafts/requirements.json` plus
`<block>_notes.md` (source table, gaps, open points as options). Review them, copy the JSON to
`spec/requirements.json`, then `approve spec_baseline`. Only the approved JSON is the contract.

---

## Human decisions

From your own terminal (agents are blocked from running these):

```bash
python3 tools/mae.py status
python3 tools/mae.py approve spec_baseline --signer you --reason "..."
python3 tools/mae.py approve waive:timing --signer you --reason "..."
python3 tools/mae.py approve extend_budget --amount 2 --signer you --reason "..."
python3 tools/mae.py approve signoff --signer you --reason "..."
python3 tools/mae.py report        # writes reports/signoff_report.md
```

---

## Board integration

After the block flow, `impl/board/zcu104/` integrates the signed-off RTL on a ZCU104: a Block Design
(PS → smartconnect → AXI-APB bridge → uart_apb), a BD-level testbench driven by the PS simulation
model, a bare-metal demo and a JTAG run script. It is a manual step and produces no mae evidence.

```bash
source /tools/Xilinx/Vivado/2022.2/settings64.sh; source /tools/Xilinx/Vitis/2022.2/settings64.sh
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/build_bd.tcl   # bitstream + XSA
vivado -mode batch -nolog -nojournal -source impl/board/zcu104/sim_bd.tcl     # BD testbench
xsct impl/board/zcu104/sw/build_sw.tcl                                        # uart_apb_demo.elf
xsct impl/board/zcu104/sw/run_jtag.tcl                                        # program + run
```

Results for uart_apb and the lessons from the board bring-up are in
[docs/FPGA_FLOW.md §7](docs/FPGA_FLOW.md#7-stage-8-board-integration-on-zcu104-manual-outside-mae).

---

## Testing the control plane without a model

```bash
python3 -m unittest discover -s tests -v
python3 examples/replay_session.py --scenario default --human-waives-timing
python3 examples/replay_session.py --scenario regressing-fix --human-waives-timing
python3 examples/replay_session.py --scenario noop-fix
python3 examples/replay_session.py --backend vivado-fake     # Vivado path with a FAKE stub
python3 examples/replay_session.py --backend vivado          # real Vivado, if installed
```

The replay feeds the real hooks the JSON payloads Claude Code sends and runs the real tools; only
agent-written content is scripted (`examples/fifo-scripted/`, a FIFO with a seeded overflow bug).
`--backend vivado-fake` puts `examples/fake_vivado/vivado` on `PATH`: it writes canned reports so the
Vivado runner, parser and gates run end to end. Its version string says FAKE and signoff refuses
evidence from it (`real_tools` check).

---

## Repository layout

```
.
├── CLAUDE.md                 rules for every agent
├── .claude/
│   ├── agents/               10 agent definitions
│   ├── skills/               5 method skills
│   ├── hooks/                guard_tools, guard_spawn, check_report, orchestrator_stop
│   ├── settings.json         main agent, permissions, hook wiring
│   └── mae-policy.json       per-role write scopes, read barriers, Bash allow-lists
├── mae/                      engine: flow, gates, runner, evidence ledger, briefs, style, vivado parser
├── tools/                    mae.py (CLI), drawio_text.py
├── tests/                    control-plane tests (no model needed)
├── examples/                 FIFO replay fixture, FAKE Vivado stub, replay_session.py
├── docs/FPGA_FLOW.md         the FPGA flow, end to end
├── spec/                     reference project: uart_apb requirements + notes
├── rtl/  dv/  formal/        reference project: RTL, testbench, properties
└── impl/
    ├── uart_apb.xdc          out-of-context constraints (block flow)
    └── board/zcu104/         Block Design, BD testbench, software, JTAG scripts
```

Not in git: `.mae/` (state, ledger, runner key), `reports/`, `impl/board/*/build/`, Vivado scratch
files.

---

## Maintenance sessions

The default session (`claude`) is the orchestrator and cannot write files. To change the spec, the
engine, skills, agents, examples or tests, start the supervised maintainer:

```bash
MAE_ROLE=project-maintainer claude --agent project-maintainer          # bash / zsh
$env:MAE_ROLE="project-maintainer"; claude --agent project-maintainer  # PowerShell
```

It has full access (`"full_access": true` in `.claude/mae-policy.json`); Claude Code's permission
prompts still ask before risky calls. After it edits `spec/requirements.json`, re-run
`approve spec_baseline` yourself.

Read-only search (Haiku, no write tools): `MAE_ROLE=code-searcher claude --agent code-searcher`.

---

## Known limits

- Hooks are guardrails inside one machine, not a security boundary. For production, run the runner
  as a separate service holding the signing key, and give each agent a container with only its own
  mounts.
- `mae evidence --log` on lint output can show RTL source lines to verification agents.
- The workflow template is one block. Multi-block, ASIC place and route and DFT need new tasks in
  `Project._template()` (see [docs/FPGA_FLOW.md §9](docs/FPGA_FLOW.md#9-toward-the-asic-flow-plan-not-yet-run)).
- Formal runs k-induction with a fixed depth (`-maxsteps 20`); long sequential behaviour usually
  rests on simulation. Plan the formal scope per block.
- The Vivado parser has been exercised on real Vivado 2022.2 output (reference project) and on the
  FAKE stub's canned reports.

---

## License and credits

The skills contain material selected, condensed and modified from **rtl-skills** and the **VLSIT**
skill library (both Apache-2.0; see [LICENSE-rtl-skills](LICENSE-rtl-skills)), and ideas from
VeriFlow-CC and xilinx-skill. Details per file are in [NOTICE.md](NOTICE.md).
