# Solution

`puzzle.gds` is an **11 × 11 Star Battle ("Two Not Touch") checker**.

The 121 bits presented on `I`, one per clock while `enable` is high, are a **row-major
raster of an 11 × 11 grid** — one bit per cell, not a character stream. The design asserts
`success` when the grid holds a valid Star Battle solution.

## The answer

```
0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000
```

121 bits, popcount 22. Drives `success` high and makes `O[7:0]` spell `(* TWO STARS *)`.

```
. . . . . . . * . * .
* . . . . * . . . . .
. . . . . . . * . * .
* . * . . . . . . . .
. . . . * . * . . . .
. . * . . . . . * . .
. . . . * . . . . . *
. * . . . . * . . . .
. . . * . . . . . . *
. . . . . * . . * . .
. * . * . . . . . . .
```

A ready-to-load waveform is at `tools/out/puzzle/solution.vcd`, in the same format as
`example_inputs.vcd` (1 ps timescale, 10 ns clock, `rst_n` released at 30 ns, `enable`
asserted at 40 ns for 121 cycles).

## What the hardware checks

All five conditions, enforced in silicon and recovered from the netlist:

1. every **row** has exactly 2 stars
2. every **column** has exactly 2 stars
3. every **region** has exactly 2 stars
4. no two stars **touch**, including diagonally
5. the total is **22**

The regions are not a stored table — they are a combinational decoder over the row and
column counters, a 147-cell flop-free block in the bottom right of the die. Recovered
by simulating one-hot inputs and watching which region counter increments:

```
A A A A A B B C D D E        A 14   B 21   C  7
A A F A A B C C D D E        D  5   E 28   F  8
A A F B B B B C C D E        G 11   H  9   I  6
A A F B G G G E C C E        J  8   K  4
F A F B G E E E E E E
F F F B G G G E H H H        all 11 regions orthogonally connected,
B B B B B B G E H I I        covering all 121 cells with no overlaps
B J J J G G G E H I I
B J J K E E E E H I I
B B J K K E E E H H H
B J J K E E E E E E E
```

Given that map, the puzzle has **exactly one solution** (exhaustive search), and it is the
one above.

## Messages on `O[7:0]`

One character per clock, starting the cycle after the last input bit. Which string is
emitted is a pure function of the outcome — verified across hundreds of inputs.

| condition | message |
|---|---|
| valid solution | `(* TWO STARS *)` |
| counts all correct but stars touch | `TWO NOT TOUCH` * |
| all 121 bits set | `BIG BANG` |
| all 121 bits clear | `EMPTY SKY` |
| anything else | `TRY AGAIN` |

All five branches are reachable and confirmed by simulation. The near-miss branch is
exercised by placements that satisfy every count but have touching stars — `solve.py`
finds one and runs it. (*) One character of that message rides on the genuinely floating
net `n293`: the design emits `TWO"NOT TOUCH` or `TWO NOT TOUCJ` depending on the value
the floating node happens to settle at — the intended text is obviously `TWO NOT TOUCH`,
and `success` is unaffected either way.

## The planted hint

`example_inputs.vcd` contains two failed attempts. Read as 11-bit frames — 8 data bits
LSB-first plus 3 idle zeros — their 121 bits decode to ASCII:

```
trial 0   54 68 65 20 6e 69 67 68 74 20 73   "The night s"
trial 1   6b 79 20 61 77 61 69 74 73 20 20   "ky awaits  "
```

Concatenated: **`"The night sky awaits"`**. Stars. The example stimulus is a signpost to
the puzzle type, hidden in an encoding the design itself does not use.

## How this was established

The chain is validated against ground truth at every step; nothing rests on guesswork.

**Extraction.** `tools/gds2v` recovers a gate-level netlist from GDS using KLayout's
`LayoutToNetlist`. On the warmup design, where the vendor ships the answer, the recovered
netlist is **identical to `warmup/01_netlist.v` up to renaming**: 230/230 instances mapped
to DEF components, 84/84 nets, net partition identical, emitted Verilog round-trips.

**Cell models.** All 69 `sky130_fd_sc_hd__*` types used are modelled from their names
alone — no PDK. Derived pin names match the extracted netlist exactly for every type.

**The decisive check.** Replaying `example_inputs.vcd` through the extracted *puzzle*
netlist reproduces all **312 cycles of `O[7:0]` and `success` with 0 mismatches**, emitting
`TRY AGAINTRY AGAIN`. That validates extraction, cell models and simulator on the real
design, not just the warmup.

**The answer.** Verified three independent ways: it satisfies every constraint
combinatorially and is the unique solution; the gate netlist raises `success` and prints
the winning message; and all 121 single-bit perturbations fail.

**The recovered Verilog.** Every Verilog file is produced automatically by the tool from
the GDS: `03_netlist.v` (structural), `04_behavioral.v` (de-synthesised `assign`/`always`
RTL) and `06_rtl_recovered.v` (lifted RTL — recovers the 12-stage input shift register and
per-output structure). All are functionally equivalent to the layout, not the original
source: synthesis is many-to-one, so the designer's names, module hierarchy and coding
style are irrecoverable. The equivalence is what's proven — by the 0-mismatch VCD replay
above and an independent **Icarus Verilog** simulation of the extracted netlist on the
winning input (both raise `success` and print `(* TWO STARS *)`).

Reproduce all of it with:

```powershell
cd tools
.\.venv\Scripts\python.exe test_regression.py -v
```

## Two things worth knowing about the layout

**The physical arrangement really does hint at the function.** Dataflow runs left to right
across four vertical bands: the bit and row counters at x ≈ 26–43, the shift register and
popcount at x ≈ 75–97, the column and region counters at x ≈ 113–133, and the output
generator at x ≈ 150–189, with the region-decoder ROM in the bottom-right corner. See
`tools/out/figures/placement.png`, coloured by recovered function.

**The spiral is decoration.** The circular mark at (43.45, 43.75) is 1366 met2 squares on a
0.3 µm grid — a 57 × 57 bitmap of Jane Street's logo. It is electrically isolated: no vias,
no cells, and routing detours around it. No payload.

**One net is genuinely floating.** Net 293 joins the `A1` inputs of two adjacent gates with
complete routing — li1 at each pin, a 3.82 µm met1 strap, a met2 jog — and no driver
anywhere. This is a property of the layout, not an extraction defect: every pin label
resolves, no net has two drivers, and the nearest other net is 200 nm away on every layer.
It reaches `O[1]` and `O[4]` through 11 gates but no flop. Its one observable effect is a
single character of the near-miss message (`TWO"NOT TOUCH` vs `TWO NOT TOUCJ` by
polarity); it cannot affect `success`, the winning message, or any other branch.
