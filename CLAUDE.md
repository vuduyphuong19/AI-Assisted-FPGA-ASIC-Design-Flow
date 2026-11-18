# IC multi-agent project: rules for every agent

This repository is driven by an orchestrator-worker team of Claude Code subagents.
Project state, gates and evidence belong to `python3 tools/mae.py`, not to any agent.

## Truth comes from tools
- Results (pass/fail, coverage, proof, area, timing) exist only as evidence records written by the
  tool runner: `python3 tools/mae.py run <style|lint|sim|formal|synth|impl>` (`sta` on the yosys
  back end). Cite them by ID (EV-...).
- Missing evidence is a failure. NOT_RUN and ERROR are failures. Estimates are labelled estimates.
- Agreement between agents is not evidence.

## Claims in reports are typed
`hypothesis` | `assumption` | `estimate` | `unverified` | `tool_result` (needs `evidence_refs`).
Agents never write `verified_fact`; only a passed gate creates one. Untyped or unsupported claims
are downgraded and recorded as findings.

## Ownership
| Path | Owner | Who may read |
| --- | --- | --- |
| `spec/` | human (drafts by spec-analyst in `spec-drafts/`) | everyone |
| `rtl/` | rtl-designer | everyone except dv-engineer, formal-engineer |
| `dv/` | dv-engineer | everyone except rtl-designer, formal-engineer |
| `formal/` | formal-engineer | everyone except rtl-designer, dv-engineer |
| `impl/` (Vivado XDC) | impl-engineer | everyone |
| `reports/<task>.json` | the agent that ran the task | per barriers above |
| `.mae/`, `.claude/`, `tools/`, `mae/` | nobody (hooks deny) | `.mae/` nobody |

To change something you do not own, write a finding in your report.

## Humans decide
Spec baseline, waivers, budget extensions and signoff: `python3 tools/mae.py approve ...`,
run by a person in their own terminal. Agents are blocked from running it.

## Skills (method; preloaded per agent)
| Skill | Agents | Content |
| --- | --- | --- |
| `rtl-coding` (+ `patterns.md`) | rtl-designer, design-reviewer | plan-before-code, hard rules checked by `mae run style`, Vivado-friendly RTL, patterns P1–P16, bug catalog |
| `dv-testbench` | dv-engineer, design-reviewer | reference model, per-cycle compare, output contract, stimulus phases, coverage |
| `failure-triage` | debug-triage | symptom classes, sim-vs-formal cross-check, root location, suspect decision |
| `vivado-flow` (+ `board-reference.md`) | impl-engineer, design-reviewer | XDC and CDC rules, runner's Vivado flow and its checks, reading synth/impl evidence, timing-closure order, board I/O, IP, official docs |
| `spec-writing` | spec-analyst | reading/citing `spec/sources/` (Markdown, PDF, draw.io via `tools/drawio_text.py`), atomic REQs, port/parameter/error/crossing facts, gap checklist, open points as options, coverage bins, self-review |

## Sessions
- `claude` → `ic-orchestrator`: runs the flow; writes nothing itself.
- `MAE_ROLE=project-maintainer claude --agent project-maintainer` → human-supervised session with
  full access (`full_access` in `mae-policy.json`): every tool, any command including `git push`,
  any app, any URL, any file, inside or outside the project. It is the only such role; all others
  keep their scopes and barriers, enforced by the hooks in `.claude/hooks/`.
- `code-searcher` (Haiku, Read/Grep/Glob only) → read-only file/symbol/text search with `path:line`
  results; not part of the flow. Run it alone (`MAE_ROLE=code-searcher claude --agent code-searcher`)
  or as a subagent of the maintainer session.
