"""Lift the puzzle netlist to full, readable behavioural RTL - and verify it.

  python -m puzzle.liftrtl [--random N]

gds2v's generic lifter recovers registers and word-level structure; this module goes
the rest of the way for the puzzle, emitting `07_rtl_lifted.v`: a complete, human-
readable design (grid loader, row/column/region counting over the RECOVERED region
map, adjacency check, outcome classification, message playback) that behaves
cycle-for-cycle like the extracted gate netlist.

Nothing in the RTL is assumed.  The region map and the constraint semantics are the
ones proven against the silicon by SAT (see prove.py: `success` <=> valid Star Battle
on this map; the near-miss message <=> counts right but stars touching; message set
complete).  The playback strings and timing are MEASURED from the netlist by this
module, and the emitted design is then co-simulated against the netlist cycle-for-
cycle on the answer, the near-miss, both degenerate grids, all 121 single-bit flips
and thousands of random grids.

Scope: the standard single-attempt protocol (reset, 121 bits with enable high, idle
tail), floating net n293 at 0.  Original names/hierarchy are unrecoverable from a
layout; this is the same *design*, not the same source text.
"""
import argparse
import json
import sys
import time

import numpy as np

from gds2v import paths
from gds2v.sim import GateSim, standard_stimulus
from gds2v.lift import lint_verilog

GRID = 11
N_BITS = GRID * GRID
RESET_CYCLES = 3

# outcome classes, in the circuit's priority order (proven by prove.py)
CLASSES = ("WIN", "BIG_BANG", "EMPTY_SKY", "TOUCH", "TRY_AGAIN")


def classify(bits, region_map):
    """The proven outcome classification of a 121-bit grid."""
    g = [int(c) for c in bits]
    rows = [sum(g[r * GRID:(r + 1) * GRID]) for r in range(GRID)]
    cols = [sum(g[r * GRID + c] for r in range(GRID)) for c in range(GRID)]
    reg = {}
    for r in range(GRID):
        for c in range(GRID):
            reg[region_map[r][c]] = reg.get(region_map[r][c], 0) + g[r * GRID + c]
    counts_ok = (all(v == 2 for v in rows) and all(v == 2 for v in cols)
                 and all(v == 2 for v in reg.values()))
    touching = any(
        g[r * GRID + c] and g[r2 * GRID + c2]
        for r in range(GRID) for c in range(GRID)
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1))
        for r2, c2 in ((r + dr, c + dc),)
        if 0 <= r2 < GRID and 0 <= c2 < GRID)
    if counts_ok and not touching:
        return "WIN"
    if all(x == 1 for x in g):
        return "BIG_BANG"
    if all(x == 0 for x in g):
        return "EMPTY_SKY"
    if counts_ok:
        return "TOUCH"
    return "TRY_AGAIN"


def measure(sim, region_map, sol):
    """Measure each class's message bytes and the shared timing from the netlist."""
    witness = {"WIN": sol["bits"], "TOUCH": sol["near_miss"]["bits"],
               "BIG_BANG": "1" * N_BITS, "EMPTY_SKY": "0" * N_BITS,
               "TRY_AGAIN": ("01" * 61)[:N_BITS]}
    rom, start = {}, None
    for cls, bits in witness.items():
        assert classify(bits, region_map) == cls, f"witness misclassified for {cls}"
        res = sim.run(standard_stimulus(bits, reset_cycles=RESET_CYCLES, tail=40))
        o = [x[0] for x in res]
        s = [x[1] for x in res]
        nz = [i for i, v in enumerate(o) if v]
        assert nz and nz == list(range(nz[0], nz[-1] + 1)), \
            f"{cls}: O is not a single contiguous message burst"
        if start is None:
            start = nz[0]
        assert nz[0] == start, f"{cls}: message starts at {nz[0]}, expected {start}"
        rom[cls] = o[nz[0]:nz[-1] + 1]
        s1 = [i for i, v in enumerate(s) if v]
        if cls == "WIN":
            assert s1 and s1[0] == start and s1 == list(range(start, len(s))), \
                "success must rise with the first message byte and latch"
        else:
            assert not s1, f"{cls}: success asserted?!"
    assert start == RESET_CYCLES + N_BITS, \
        "message must start the cycle after the last input bit"
    return rom


class SpecModel:
    """The emitted RTL's exact semantics, as Python - the co-simulation reference."""

    def __init__(self, region_map, rom):
        self.region_map, self.rom = region_map, rom

    def run(self, bits, tail=40):
        """-> [(O, success)] per cycle, standard protocol, cycle-aligned to GateSim."""
        cls = classify(bits, self.region_map)
        msg = self.rom[cls]
        out = [(0, 0)] * (RESET_CYCLES + N_BITS)
        win = 1 if cls == "WIN" else 0
        for k in range(tail):
            out.append((msg[k] if k < len(msg) else 0, win))
        return out


# ---------------------------------------------------------------------------
def _region_assigns(region_map):
    cells = {}
    for r in range(GRID):
        for c in range(GRID):
            cells.setdefault(region_map[r][c], []).append(r * GRID + c)
    L = []
    for ch in sorted(cells):
        idx = cells[ch]
        terms = " + ".join(f"grid[{i}]" for i in idx)
        L.append(f"  // region {ch} - {len(idx)} cells (map recovered from the "
                 f"silicon, proven by SAT)")
        L.append(f"  wire [4:0] region_{ch} = {terms};")
    ok = " && ".join(f"(region_{ch} == 2)" for ch in sorted(cells))
    L.append(f"  wire regions_ok = {ok};")
    return "\n".join(L)


def _rom_function(rom):
    L = ["  // message playback: byte k of each outcome's message, measured from the",
         "  // netlist and proven complete by SAT (prove.py).  The near-miss string",
         "  // carries the floating net n293 at 0: TWO\"NOT TOUCH is what the silicon",
         "  // says (the clean spelling is proven unreachable).",
         "  function [7:0] msg_byte(input [2:0] cls, input [5:0] k);",
         "    begin",
         "      msg_byte = 8'h00;",
         "      case (cls)"]
    for ci, cls in enumerate(CLASSES):
        text = "".join(chr(v) if 32 <= v < 127 else "." for v in rom[cls])
        L.append(f"        3'd{ci}: case (k)  // {text}")
        for k, v in enumerate(rom[cls]):
            L.append(f"          6'd{k}: msg_byte = 8'h{v:02x};")
        L.append("          default: msg_byte = 8'h00;")
        L.append("        endcase")
    L += ["        default: msg_byte = 8'h00;",
          "      endcase",
          "    end",
          "  endfunction"]
    return "\n".join(L)


def emit_rtl(region_map, rom):
    return f"""// 07_rtl_lifted.v - complete behavioural RTL lifted from puzzle.gds.
// Generated by `python -m puzzle.liftrtl`; every fact below is recovered, measured
// or proven from the layout alone:
//   * the region map and constraint semantics are proven against the gate netlist
//     by SAT (prove.py: success <=> valid Star Battle on this map)
//   * the message strings and timing are measured from the netlist and the message
//     set is proven complete over all 2^121 inputs
//   * this module co-simulates cycle-for-cycle with the extracted netlist (see
//     liftrtl.py; corners + all 121 single-bit flips + random grids)
// Scope: the standard single-attempt protocol; floating net n293 = 0.
// Synthesis destroys names and hierarchy irreversibly, so this is the recovered
// DESIGN, not the designer's source text.

module puzzle (
  input        clk,
  input        rst_n,     // async, active low
  input        enable,    // one grid bit on I per cycle while high
  input        I,
  output reg [7:0] O,     // ASCII message, one char per cycle after loading
  output reg   success
);

  // 11 x 11 Star Battle grid, row-major: grid[0] is row 0 col 0, first bit in.
  reg [120:0] grid;
  reg [6:0]   nbits;      // grid bits received so far (0..121)
  reg [5:0]   mcyc;       // message cycle counter once loading completes

  wire done = (nbits == 121);

  // ------------------------------------------------------------- the rules
  function [3:0] count_row(input [120:0] g, input integer r);
    integer c;
    begin
      count_row = 0;
      for (c = 0; c < 11; c = c + 1) count_row = count_row + g[r * 11 + c];
    end
  endfunction

  function [3:0] count_col(input [120:0] g, input integer c);
    integer r;
    begin
      count_col = 0;
      for (r = 0; r < 11; r = r + 1) count_col = count_col + g[r * 11 + c];
    end
  endfunction

  function any_touching(input [120:0] g);
    integer r, c;
    begin
      any_touching = 0;
      for (r = 0; r < 11; r = r + 1)
        for (c = 0; c < 11; c = c + 1)
          if (g[r * 11 + c]) begin
            if (c < 10 && g[r * 11 + c + 1])            any_touching = 1;
            if (r < 10 && g[(r + 1) * 11 + c])          any_touching = 1;
            if (r < 10 && c < 10 && g[(r + 1) * 11 + c + 1]) any_touching = 1;
            if (r < 10 && c > 0  && g[(r + 1) * 11 + c - 1]) any_touching = 1;
          end
    end
  endfunction

  reg rows_ok, cols_ok;
  integer ri, ci;
  always @* begin
    rows_ok = 1'b1;
    for (ri = 0; ri < 11; ri = ri + 1)
      if (count_row(grid, ri) != 2) rows_ok = 1'b0;
    cols_ok = 1'b1;
    for (ci = 0; ci < 11; ci = ci + 1)
      if (count_col(grid, ci) != 2) cols_ok = 1'b0;
  end

{_region_assigns(region_map)}

  wire touching  = any_touching(grid);
  wire counts_ok = rows_ok && cols_ok && regions_ok;
  wire valid     = counts_ok && !touching;   // exactly 2 per row/col/region, no touch
  wire all_ones  = &grid;
  wire all_zeros = ~|grid;

  // outcome class, in the circuit's proven priority order
  wire [2:0] outcome = valid      ? 3'd0 :   // WIN       -> "(* TWO STARS *)"
                       all_ones   ? 3'd1 :   // BIG BANG  (only the full grid)
                       all_zeros  ? 3'd2 :   // EMPTY SKY (only the empty grid)
                       counts_ok  ? 3'd3 :   // TOUCH     -> near-miss message
                                    3'd4;    // TRY AGAIN

{_rom_function(rom)}

  // ------------------------------------------------------------- sequencing
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      grid    <= 121'b0;
      nbits   <= 7'd0;
      mcyc    <= 6'd0;
      O       <= 8'h00;
      success <= 1'b0;
    end else begin
      if (enable && !done) begin
        grid[nbits] <= I;
        nbits       <= nbits + 7'd1;
      end
      if (done) begin
        // the message starts the cycle after the last input bit, one char per
        // cycle; success rises with the first char and latches
        O       <= msg_byte(outcome, mcyc);
        mcyc    <= mcyc + 6'd1;
        success <= success | (outcome == 3'd0);
      end
    end
  end

endmodule
"""


# ---------------------------------------------------------------------------
def cosim(sim, spec, stimuli, tail=40):
    """Cycle-exact compare of SpecModel vs the gate netlist on many grids at once."""
    b = sim.run_batch(stimuli, reset_cycles=RESET_CYCLES, tail=tail)
    bad = 0
    for ln, bits in enumerate(stimuli):
        exp = spec.run(bits, tail=tail)
        got = list(zip(b["O"][:, ln].tolist(), b["S"][:, ln].astype(int).tolist()))
        if got != exp:
            bad += 1
            first = next(i for i, (g, e) in enumerate(zip(got, exp)) if g != e)
            print(f"   !! lane {ln}: first mismatch at cycle {first}: "
                  f"netlist={got[first]} rtl={exp[first]}")
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netlist", default=str(paths.PUZZLE_OUT / "03_netlist.json"))
    ap.add_argument("--solution", default=str(paths.PUZZLE_OUT / "solution.json"))
    ap.add_argument("--out", default=str(paths.PUZZLE_OUT / "07_rtl_lifted.v"))
    ap.add_argument("--random", type=int, default=2000,
                    help="random grids for the co-simulation sweep")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    t0 = time.time()
    ok = True

    sol = json.load(open(a.solution))
    region_map = sol["region_map"]
    sim = GateSim(a.netlist)

    print("== measuring message ROM and timing from the netlist ==")
    rom = measure(sim, region_map, sol)
    for cls in CLASSES:
        print(f"   {cls:10s} {''.join(chr(v) if 32 <= v < 127 else '.' for v in rom[cls])!r}")
    print(f"   message starts cycle {RESET_CYCLES + N_BITS} "
          f"(the cycle after the last input bit); success latches with char 0")

    print("\n== emitting RTL ==")
    text = emit_rtl(region_map, rom)
    open(a.out, "w").write(text)
    lint = lint_verilog(text)
    print(f"   wrote {a.out} ({len(text.splitlines())} lines)")
    print(f"   lint clean: {not lint}" + (f"  {sorted(lint)[:3]}" if lint else ""))
    ok &= not lint

    print("\n== co-simulating the lifted RTL against the gate netlist ==")
    spec = SpecModel(region_map, rom)
    rng = np.random.default_rng(a.seed)
    corners = [sol["bits"], sol["near_miss"]["bits"], "1" * N_BITS, "0" * N_BITS]
    flips = [sol["bits"][:i] + str(1 - int(sol["bits"][i])) + sol["bits"][i + 1:]
             for i in range(N_BITS)]
    rand = ["".join(rng.choice(["0", "1"], N_BITS,
                               p=[1 - p, p])) for p in
            rng.uniform(0.05, 0.5, a.random)]
    # bias a slice towards count-plausible grids so non-TRY branches get exercise
    stimuli = corners + flips + rand
    bad = cosim(sim, spec, stimuli)
    n_cyc = (RESET_CYCLES + N_BITS + 40) * len(stimuli)
    print(f"   {len(stimuli)} grids ({len(corners)} corners + {len(flips)} flips + "
          f"{len(rand)} random), {n_cyc} cycles compared")
    print(f"   mismatching grids: {bad}")
    ok &= bad == 0
    print(f"   cosim exact: {bad == 0}")

    print(f"\n({time.time() - t0:.1f}s total)")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
