# gds2v — reverse-engineer a GDSII layout back to Verilog

This directory takes a finished chip layout (a `.gds` file — polygons on metal layers,
no netlist, no source) and recovers, in order:

1. a **gate-level netlist** — which standard cell connects to which, on which net;
2. **simulatable Verilog** — behavioural models so the netlist runs in any simulator;
3. **de-synthesised RTL** — the same logic as `assign`/`always` blocks;
4. **lifted RTL** — recovered *structure* (shift registers, `a + b == constant`);
5. a **gate-symbol schematic** (SVG);
6. a **capability report** stating exactly what was and was not recovered.

It was built to solve the [Jane Street ASIC puzzle](https://blog.janestreet.com/can-you-reverse-engineer-an-asic/)
(that answer is in [`../SOLUTION.md`](../SOLUTION.md)), but the extractor is not
puzzle-specific: it works on **any valid GDS**, and where full recovery is impossible it
degrades honestly instead of guessing. It reproduces the published netlist and RTL of a
real third-party chip (Efabless Caravel) as a regression.

Nothing here is puzzle data; the repo root stays source-data-only.

---

## The one idea that makes it work

A GDS is just polygons. Turning polygons back into a netlist normally needs the foundry's
cell library (LEF/Liberty) to know where each cell's pins are and what each cell does.

These layouts hand us a shortcut: **every standard cell carries its pin *names* as text
labels inside its own geometry** (in sky130, on the li1/met1 label layers). So each pin is
a *probe point* — a coordinate with a known name. That turns extraction into a purely
geometric procedure needing no PDK:

```
        cell definition in the GDS                 what we read out
        ┌───────────────────────┐
        │  ▓ "A"      ▓ "Y"      │   pin label "A" at (x1,y1)  ── input
        │  ▓ "B"                 │   pin label "B" at (x2,y2)  ── input
        │  "VPWR" ▔▔▔▔▔▔▔▔▔      │   pin label "Y" at (x3,y3)  ── output
        └───────────────────────┘   ("VPWR"/"VGND" = supply, ignored)
```

The flow is then:

```
  1. record every leaf-cell instance and its placement transform
  2. flatten the layout so vias and cell interiors become one coordinate space
  3. let KLayout's LayoutToNetlist merge metal + vias into electrical nets
  4. for each cell pin: transform its label to global coords, probe_net() it
  5. the top-level port labels name the external nets
```

Cells are identified as standard cells by being **leaf cells that carry pin labels** — not
by any name prefix — so connectivity extracts for any library. What the *cells do* is a
separate question answered by [`cells.py`](gds2v/cells.py) from the sky130 naming grammar;
an unknown library still yields connectivity, with functions marked "blackbox".

> **The subtle trap that cost hours.** `LayoutToNetlist.connect(a, b)` declares *inter*-layer
> connectivity only. `connect(a)` — a single argument — declares *intra*-layer connectivity.
> Omit the latter and touching polygons on the *same* layer stay separate nets, silently
> fragmenting the extraction (172 nets instead of 84 on the warmup). Both are needed.

---

## Pipeline

```mermaid
flowchart LR
    GDS([input.gds]) --> EX[extract.py<br/>geometry → nets]
    TP[techprofile.py<br/>layer map] --> EX
    EX --> NL[(netlist JSON<br/>+ report.txt)]
    NL --> EM[emit.py]
    EM --> SV[03_netlist.v<br/>structural]
    NL --> CE[cells.py<br/>cell models]
    CE --> BV[04_behavioral.v<br/>assign / always]
    CE --> SIM[sim.py<br/>cycle-accurate]
    NL --> LF[lift.py] --> RTL[06_rtl_recovered.v<br/>registers + a+b==k]
    NL --> SC[schematic.py] --> SVG[05_schematic.svg]
    SIM -.verify.-> BV
    SIM -.verify.-> RTL
    DEF([DEF + ref netlist]) --> VAL[validate.py<br/>equivalence proof]
    NL --> VAL
```

Each box is one module in [`gds2v/`](gds2v/); the dotted arrows are the *self-checks* — the
lifted and behavioural RTL are only trusted after they co-simulate against the gate netlist.

---

## Project layout

```
tools/
├── pyproject.toml          installable package (gds2v + puzzle); pins the dependencies
├── requirements.txt        one line: `-e .` (editable install of the above)
├── gds2v/                  CORE LIBRARY — GDS → Verilog, reusable and PDK-agnostic
│   ├── __main__.py           the `python -m gds2v` CLI
│   ├── paths.py              one source of truth for every data path
│   ├── extract.py            geometry → cells + nets  (+ capability report)
│   ├── techprofile.py        layer-map profiles: built-in sky130 + auto-detect
│   ├── cells.py              cell-name grammar → boolean / sequential models
│   ├── emit.py               netlist naming → JSON / structural / behavioural Verilog
│   ├── sim.py                cycle-accurate 2-valued simulator (numpy bit-parallel)
│   ├── lift.py               shift-register + word-level structure recovery
│   ├── schematic.py          gate-symbol SVG renderer
│   └── validate.py           equivalence check vs a DEF + reference netlist
├── puzzle/                 APPLICATION — the Star Battle solver, built on gds2v
│   ├── analyze.py            recover the FSM / counters / region map
│   ├── solve.py              solve, verify on the netlist, write solution.vcd
│   ├── visualize.py          floorplan / region / logo figures
│   └── vcdtool.py            read + write the puzzle's VCD traces
├── tests/                  ALL tests + fixtures
│   ├── test_regression.py    the whole pipeline, end to end (run this)
│   ├── test_{warmup,behavioral,generality,opensource}.py
│   ├── testutil.py           shared PASS/FAIL bookkeeping
│   ├── make_test_gds.py      synthetic GDS generator (generality fixtures)
│   └── fetch_opensource.py   third-party Caravel fixture downloader
└── out/                    all generated output (gitignored)
```

---

## Setup

Requires **CPython 3.12**. One command installs the toolchain (editable) and its
dependencies from `pyproject.toml`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # editable install of gds2v + puzzle
.\.venv\Scripts\python.exe tests\test_regression.py             # ~4 min; expect "47/47 checks passed"
```

The install makes `gds2v` and `puzzle` importable from anywhere and registers the
`python -m gds2v` CLI, so nothing depends on your current directory.

(A fresh run reports **47/47**. The 48th check is the third-party Caravel suite, which is
skipped until you fetch its ~160 MB fixtures — see [Testing](#testing).)

| package | used for |
|---|---|
| `klayout` | `LayoutToNetlist` connectivity extraction (gdstk cannot do this) |
| `gdstk` | fast GDS reads: placements, polygons, cell census |
| `numpy` | bit-parallel simulation, one lane per stimulus |
| `vcdvcd` | VCD parsing |
| `networkx` | flop dependency graph, SCCs, logic cones, levelisation |
| `matplotlib` | schematic and placement figures |

> **Two Windows environment traps (both cost real debugging time):**
> - **Run from PowerShell, not the Bash/MSYS shell.** That shell's profile loads the nRF
>   Connect SDK env, which puts MinGW DLLs on `PATH` that collide with numpy's OpenBLAS —
>   `import numpy` segfaults outright.
> - **Do not use the nRF toolchain Python** (`C:\ncs\toolchains\...`). Its `ctypes` fails to
>   initialise regardless of `PATH`, so `klayout`/`gdstk` cannot load there. The venv here
>   sidesteps both.

---

## Run it

```powershell
.\.venv\Scripts\python.exe -m gds2v <input.gds> -o <outdir> [options]
```

| option | meaning |
|---|---|
| `--profile sky130 \| auto` | technology profile; default = match a built-in, else auto-detect |
| `--top <name>` | choose the top cell when a GDS has several |
| `--prune-fill` | drop decap/tap/fill before extraction (needed for fill-dominated dies) |
| `--cone <port>` | also draw the logic cone of one output as a separate schematic |
| `--def D.def --ref R.v` | validate the result against a DEF + reference netlist |
| `-m, --module <name>` | override the emitted module name (default = top cell) |
| `--power` | include `VPWR`/`VGND` connections in the emitted Verilog |
| `--no-schematic`, `-q` | skip the SVG / quiet mode |

Outputs written to `<outdir>`:

| file | contents |
|---|---|
| `report.txt` | capability report — technology, hierarchy, counts, which stages ran |
| `01_cells.json` | placed cells: type, coordinates, orientation |
| `02_nets.json` | nets and their `(instance, pin)` terminals |
| `03_netlist.json` | canonical machine-readable netlist (consumed by every other stage) |
| `03_netlist.v` | **structural Verilog** — one cell instance per gate |
| `04_behavioral.v` | **de-synthesised RTL** — one `assign` per gate, one `always` per flop † |
| `05_schematic.svg` | **circuit diagram** — IEEE gate symbols, D-flop boxes, inversion bubbles |
| `06_rtl_recovered.v` | **lifted RTL** — registers + proven word-level functions † |
| `cells_sim.v` | behavioural models for every cell type used (simulation) † |
| `extract.log` | run log |

† `04`, `06`, `cells_sim.v` need every cell's *function* to be known (recognised library).
On an unknown library they are skipped with a reason; `03_netlist.v` and the schematic are
still produced from connectivity alone.

---

## Worked example: recovering `a + b == 496` from a layout

The `warmup/` design is a tiny chip whose ground-truth source we know, so it makes a clean
demonstration. Its `00_source.v` is two 8-bit shift registers feeding an adder and a
`== 496` comparator — but we only feed the tool the **final GDS**:

```powershell
.\.venv\Scripts\python.exe -m gds2v ..\warmup\04_final.gds -o out\warmup
```

**`out/warmup/report.txt`** — what was recovered:

```
== gds2v capability report: ..\warmup\04_final.gds ==
  top cell        : adder_demo
  technology      : sky130
  hierarchy       : flat
  logic instances : 79 (16 known types, 0 blackbox)
  nets            : 86 (8 named from labels)
  unresolved pins : 0
  downstream stages:
    [available] structural_verilog
    [available] simulation
    [available] behavioral_verilog
    [available] lift_rtl
    [available] schematic
```

**`out/warmup/06_rtl_recovered.v`** — the tool re-derives the *design*, from the layout
alone, with the register widths, the shift semantics, and the compare constant all recovered
(and the `== 496` **proven exhaustively over all 2¹⁶ register states**):

```verilog
module adder_demo (A, B, S, clk, en, rst_n);
  input A; input B; output S; input clk; input en; input rst_n;

  reg [7:0] sr_a;   // shift register, serial input A
  reg [7:0] sr_b;   // shift register, serial input B

  always @(posedge clk or negedge rst_n)
    if (!rst_n) sr_a <= 8'b0;
    else if (en) sr_a <= {sr_a[6:0], A};

  always @(posedge clk or negedge rst_n)
    if (!rst_n) sr_b <= 8'b0;
    else if (en) sr_b <= {sr_b[6:0], B};

  assign S = ({1'b0, sr_a} + {1'b0, sr_b} == 9'd496);   // exhaustive over 2^16 states
endmodule
```

Compare to the original `warmup/00_source.v`: two `shift_register`s, `assign sum = a + b`,
`assign eq = (val == 9'd496)`. Same design. The only thing not recovered is the *names*
(`sr_a` is synthesised; the original was `sr_a`/`a_reg`) — because synthesis destroys names
and module hierarchy irreversibly. What **is** proven is behavioural equivalence.

`test_warmup.py` grades this run against all four reference files the warmup ships:

```
.\.venv\Scripts\python.exe test_warmup.py
...
22/22 checks passed
```

—placement matched to the DEF (230/230 by coordinate), net partition identical to the
gate netlist up to renaming, and the recovered logic simulated against a golden model of
`00_source.v` over every `(a,b)` summing to 495/496/497 plus random reset/enable traces.

---

## Any valid GDS: what happens, and the honest limits

The extractor never crashes on a valid file and never claims more than it proved.
`test_generality.py` drives synthetic files that each break a naïve sky130-only extractor:

| stress case | behaviour |
|---|---|
| **unknown PDK layers** | layer stack auto-detected from geometry; connectivity + structural netlist + schematic produced; functions marked **blackbox**, simulation/RTL disabled *with a reason* |
| **nested hierarchy** | descends into sub-modules (not just the top's direct children) |
| **AREF arrays** | array placements expanded to individual instances |
| **multiple top cells** | one chosen (or `--top`), the rest warned about; no crash |
| **no pin labels** | reported as "cannot recover a pin-level netlist"; no crash |
| **empty file** | reported; no crash |

**Hard limits, stated plainly:**

- **No pin labels → no pins.** Foundry GDS often ships *abstract* cells whose pin geometry
  lives in a separate LEF. Without labels or LEF, connectivity to cell pins is unrecoverable.
- **Unknown library → no functions.** Cell *function* comes from the naming grammar
  ([`cells.py`](gds2v/cells.py), sky130) or a Liberty model. An unfamiliar library still
  extracts connectivity but its cells stay blackbox.
- **FPGAs are out of scope.** An FPGA design compiles to a *bitstream* configuring fixed
  silicon — there is no GDS of *your* logic to reverse. That is bitstream RE, a different
  problem.
- **Names are never recoverable.** Synthesis is many-to-one; original signal/module names
  and hierarchy are gone. Equivalence *up to renaming* is the strongest true claim.

---

## Module map

| module | responsibility |
|---|---|
| [`gds2v/extract.py`](gds2v/extract.py) | GDS → cells + nets; multi-top, hierarchy, arrays, capability report |
| [`gds2v/techprofile.py`](gds2v/techprofile.py) | layer-map profiles: built-in sky130 + geometry auto-detection |
| [`gds2v/cells.py`](gds2v/cells.py) | cell-name grammar → boolean/sequential models; blackbox fallback |
| [`gds2v/emit.py`](gds2v/emit.py) | netlist naming + JSON / structural / behavioural Verilog |
| [`gds2v/sim.py`](gds2v/sim.py) | cycle-accurate 2-valued simulator (numpy bit-parallel) |
| [`gds2v/lift.py`](gds2v/lift.py) | shift-register + word-level structure recovery, self-verified |
| [`gds2v/schematic.py`](gds2v/schematic.py) | gate-symbol SVG renderer |
| [`gds2v/validate.py`](gds2v/validate.py) | equivalence check vs a DEF + reference netlist |

The puzzle-specific analysis (built on the core library) lives in the [`puzzle/`](puzzle/)
package, each module runnable as `python -m puzzle.<name>`:

| module | run | responsibility |
|---|---|---|
| [`puzzle/analyze.py`](puzzle/analyze.py) | `python -m puzzle.analyze` | recover the FSM / region structure |
| [`puzzle/solve.py`](puzzle/solve.py) | `python -m puzzle.solve` | solve the Star Battle, write `solution.vcd` |
| [`puzzle/visualize.py`](puzzle/visualize.py) | `python -m puzzle.visualize` | floorplan / region / logo figures |
| [`puzzle/vcdtool.py`](puzzle/vcdtool.py) | `python -m puzzle.vcdtool <vcd>` | read + write VCD traces |

---

## Testing

```powershell
.\.venv\Scripts\python.exe tests\test_regression.py -v   # ~4 min → 47/47, or 48/48 with Caravel fetched
```

The regression composes the focused suites in [`tests/`](tests/), each runnable alone
(`python tests\test_warmup.py`, etc.):

| suite | proves |
|---|---|
| `tests/test_warmup.py` | GDS → netlist/RTL matches all four warmup reference files (22 checks) |
| `tests/test_behavioral.py` | emitted `04_behavioral.v` *text* re-parses and co-simulates identically |
| `tests/test_generality.py` | arbitrary-GDS handling / honest degradation (14 checks) |
| `tests/test_opensource.py` | Caravel: netlist partition + RTL behaviour match the published files |

Shared PASS/FAIL bookkeeping is in [`tests/testutil.py`](tests/testutil.py). Fetch the
third-party fixtures once with `python tests\fetch_opensource.py` (~160 MB, git-ignored) to
enable the Caravel suite; it is skipped otherwise.

---

## Notes on `puzzle.gds` (the original target)

- **Net 293 is genuinely floating** — two `A1` sinks, complete routing, no driver. Not an
  extraction defect: every pin resolves, no net has two drivers, the nearest other net is
  200 nm away. It reaches `O[1]`/`O[4]` but no flop; its only observable effect is one
  character of the near-miss message. The regression asserts exactly one such net.
- **15 `clkbuf_4` cells have unloaded outputs** — clock-tree balancing dummies. The tree is
  coherent: `clk` → one `clkbuf_16` → 16 `clkbuf_8` → 16 leaf nets carrying all 92 flops.
- **36 `INTERNAL_*` placeholders** sit in one row below the die — anonymisation leftovers
  with no real geometry.

The full solution write-up — the Star Battle answer, region map, and verification
transcript — is in [`../SOLUTION.md`](../SOLUTION.md).
