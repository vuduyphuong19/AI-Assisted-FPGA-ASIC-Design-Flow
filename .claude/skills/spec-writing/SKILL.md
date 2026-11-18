---
name: spec-writing
description: How to turn source documents (Markdown, PDF, draw.io in spec/sources/) into a requirements draft for this project — reading and citing each format, atomic testable REQs, the port/parameter/clock/error facts every downstream agent needs, the gap checklist, the option format for open points, coverage bins and a self-review before handing the draft to a human. Use for every spec-draft task.
---

# Spec Writing

Method from the VLSIT `vlsit-asic-ip-microspec` and `vlsit-ip-spec-review`
skills (Apache-2.0), cut down to this project's spec: one block, one JSON file,
approved by a human. The rtl-designer, dv-engineer and formal-engineer each
work **only** from what you write; whatever you leave open, one of them will
silently decide, and the other two may decide differently.

## 1. Output

| File | Content |
|------|---------|
| `spec-drafts/requirements.json` | Same shape as `spec/requirements.json`: `block`, `baseline`, `parameters`, `ports`, `requirements`, `coverage_bins`, `ppa_targets`. Do not invent new top-level keys. |
| `spec-drafts/<block>_notes.md` | Source table (§1a), the gap table (§3), open points with options (§4), decisions taken from the source, and one cycle table per operation (§2.4). |

The human copies the approved JSON to `spec/` and runs `mae approve spec_baseline`.

## 1a. Reading the sources (`spec/sources/`: Markdown, PDF, draw.io)

The human puts the source documents in `spec/sources/` (read-only for you).
Start by listing it (`Glob spec/sources/**`) and enter every file in the notes'
source table: file, title, revision/date, format, pages or sheets read. A file
you could not read fully is a gap. Do not assume it says nothing.

| Format | How to read | How to cite |
|--------|-------------|-------------|
| `.md` | `Read` directly. Headings are the clause structure. | `file.md §<heading>` |
| `.pdf` | `Read` with `pages` (at most 20 per call; required above 10 pages): table of contents first, then the chapters you need. Check every table number and timing value against the rendered page, since columns can shift in the extracted text. For a scanned page, read the image and label the numbers you transcribed. | `file.pdf p.<n> §<clause>` / `Table <k>` |
| `.drawio`, `.drawio.svg` | Read the text extract `<name>.drawio.md` beside it, produced by the human with `python3 tools/drawio_text.py <file> > <file>.md`. It lists every shape (label, container, properties) and every connector (from → to, label) per page. If the extract is missing, or older than the diagram, ask for it with an `assumption` claim. Do not guess from the raw XML: pages are usually compressed. | `file.drawio#<page>/<id>` |
| other (`.docx`, `.xlsx`, `.png`) | Not readable reliably: ask the human for a PDF/Markdown export, and record it in the source table as unread. | — |

Diagrams are sources, not illustrations:
- A **block diagram** gives ports, sub-blocks and connections. Every connector
  that crosses the block boundary must map to a `ports[]` entry, and vice versa.
- An **FSM diagram** gives states and transitions. Every connector becomes a
  transition with its condition, and a cycle table in the notes. A state with no
  exit, or a missing reset arrow, is a gap.
- A **timing/waveform drawing** gives cycle numbers. Write them as a cycle table,
  then turn them into "by cycle Z" REQs.
- Any connector under "missing end" in the extract is **not** a connection.
  Raise it as an open point (§4).

When sources conflict (diagram vs. text, PDF vs. Markdown, two revisions), do
not choose between them. Cite both and raise an open point. Newer revision wins
only if the source itself says so.

## 2. What the draft must state

### 2.1 Requirements
- One observable behaviour per REQ, in "when X, then Y by cycle Z" form, using
  port names. "Low latency", "fast", "robust" are not requirements: give the number.
- Each REQ is checkable from the ports alone, in simulation **and** as a property.
- Mark a requirement not stated in the source with `(derived)` and say why it follows.
- Cite the source clause for protocol behaviour (e.g. `IHI0024D §3.1`); never
  quote a standard from memory — if the document is not provided, the point is open.

### 2.2 Ports (`ports[].note`)
For every port: clock domain, reset/idle value, and signal type:

| Type | Write | Example |
|------|-------|---------|
| level | `level` (+ how long held) | `level, held until ready` |
| pulse | `pulse N cycles` + what happens if missed | `pulse 1 cycle per transfer` |
| data | `data, valid with <qualifier>` | `data, valid with m_valid` |
| static | `static` (config, constant in operation) | `static, set before enable` |
| clock / reset | polarity, sync/async | `active-low, synchronous` |

### 2.3 Parameters
Default, legal range, illegal values and their effect (e.g. "DEPTH must be a
power of two ≥ 2"). Widths that depend on a parameter are expressions, not literals.

### 2.4 Behaviour per operation
For each operation: trigger, latency in cycles from the accepting edge, what
each output shows in each cycle, back-pressure behaviour, completion. Put a
cycle table in the notes for each operation and for reset; a REQ cites it.

### 2.5 Errors and illegal input
For each illegal or unsupported input (write when full, bad opcode, misaligned
address, unknown response): the defined response (ignore / error flag / recover)
and what the next legal operation sees. "Undefined" is allowed only when stated
as such, and then no agent may test or rely on it.

### 2.6 Clocks, reset, crossings
Number of clocks and their relation; reset polarity and sync/async (this
project's RTL uses synchronous reset); for each clock crossing the mechanism
(`rtl-coding/patterns.md` P17) and a latency **bound**, never one exact value.

### 2.7 Coverage bins and PPA
- A bin for every behaviour that is easy to miss: full/empty, each boundary
  (`count == limit`), back-pressure ≥ 2 cycles, every error path, simultaneous
  events, wrap at depth−1, reset during operation.
- `ppa_targets`: Fmax with its condition; label any number you could not
  check as an estimate.

## 3. Gap checklist (fill the table in the notes)

| Topic | Typical gap | Status |
|-------|-------------|--------|
| Reset | value of each output after reset; reset during an operation | stated / open |
| Full/empty | data and flags on a rejected write/read | |
| Latency | registered or combinational output; exact cycle count | |
| Limits | parameter range; boundary compare (`>` vs `>=`) | |
| Concurrency | same-cycle events (read+write, set+clear) | |
| Illegal input | response and recovery | |
| Timeout | what happens when a peer never answers | |
| Crossings | mechanism and latency bound per crossing | |
| Secondary clear | flush/soft reset: what it clears, priority vs ops | |

Every row is `stated` (with the REQ), `open` (§4), or `not required` (with the
source's reason). An empty row is a defect of the draft.

## 4. Open points: options, not guesses

You cannot decide an open point. Write it as an `assumption` claim and in the
notes, in this shape, so the human can answer "1B":

```text
Q1. A write arrives while full=1. What happens? (source §4.2 is silent)
 A. Ignore it, data unchanged (recommended: no corruption; producer watches full)
 B. Overwrite the oldest word (newest data kept; silent loss)
 C. Ignore it and pulse an overflow flag (adds a port and a REQ)
 D. Other
```

Two to four options, each with a concrete example in the block's own signals,
at most one recommended with its reason. Until answered, the REQ that depends
on it stays out of the JSON.

## 5. Self-review before handing over

1. Every feature in the source maps to at least one REQ; every REQ has a source or `(derived)`.
   Every file in `spec/sources/` is in the source table, read fully or marked unread;
   every diagram connector maps to a port, a transition, or an open point.
2. Every REQ is atomic, uses port names and gives cycle numbers where timing matters.
3. Every port has type, domain and reset value; every parameter has a range.
4. Every error path has a defined response and a coverage bin.
5. No `TBD` in the JSON; every open point is in §4 form.
6. Gap table complete. Nothing copied from reference RTL — sources only.
