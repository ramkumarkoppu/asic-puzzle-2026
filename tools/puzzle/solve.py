"""Solve the puzzle the extracted netlist implements, and verify the answer on it.

  python -m puzzle.solve

The design is an 11x11 Star Battle ("Two Not Touch") checker: exactly 2 stars in every
row, every column and every region; no two stars orthogonally or diagonally adjacent.
The region map is recovered from the netlist by analyze.py, not assumed.

The answer is checked three independent ways:
  * combinatorially - it satisfies every constraint and the solution is unique
  * by simulation    - the gate netlist raises `success` and prints the winning message
  * by perturbation  - all 121 single-bit flips fail
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from itertools import combinations

from puzzle.analyze import Analysis, GRID, N_BITS, check_connected
from gds2v import paths
from gds2v.sim import GateSim, standard_stimulus, message_of
from puzzle import vcdtool

WIN_MESSAGE = "(* TWO STARS *)"


def solve_star_battle(region_map, stars=2, limit=None):
    """All placements with `stars` per row, per column and per region, none adjacent."""
    sols = []

    def rec(r, chosen, colc, regc):
        if limit and len(sols) >= limit:
            return
        if r == GRID:
            if all(colc[c] == stars for c in range(GRID)) and \
               all(v == stars for v in regc.values()):
                sols.append([tuple(x) for x in chosen])
            return
        rows_left = GRID - r
        for c in range(GRID):                      # column feasibility prune
            if colc[c] + rows_left * stars < stars:
                return
        for combo in combinations(range(GRID), stars):
            if any(b - a < 2 for a, b in zip(combo, combo[1:])):
                continue                            # two stars touching within the row
            if any(colc[c] >= stars for c in combo):
                continue
            add = Counter(region_map[r][c] for c in combo)
            if any(regc[k] + v > stars for k, v in add.items()):
                continue
            if chosen and any(abs(p - n) <= 1 for p in chosen[-1] for n in combo):
                continue                            # vertical / diagonal adjacency
            for c in combo:
                colc[c] += 1
            for k, v in add.items():
                regc[k] += v
            chosen.append(combo)
            rec(r + 1, chosen, colc, regc)
            chosen.pop()
            for c in combo:
                colc[c] -= 1
            for k, v in add.items():
                regc[k] -= v

    rec(0, [], Counter({c: 0 for c in range(GRID)}), Counter())
    return sols


def to_bits(solution):
    """Row-major raster, one bit per grid cell - the order the design shifts them in."""
    return "".join("1" if c in solution[r] else "0" for r in range(GRID) for c in range(GRID))


def find_near_miss(region_map, answer_rows, stars=2):
    """A placement meeting every COUNT constraint with adjacency ignored, other than
    the unique full solution.  Any such placement must have touching stars, so it
    exercises the design's near-miss ("TWO NOT TOUCH") branch.  Returns row tuples
    or None if the branch is unreachable.
    """
    letters = sorted({c for row in region_map for c in row})
    idx = {ch: i for i, ch in enumerate(letters)}
    suffix = [[0] * len(letters) for _ in range(GRID + 1)]
    for r in range(GRID - 1, -1, -1):
        row_cnt = Counter(region_map[r])
        for i, ch in enumerate(letters):
            suffix[r][i] = suffix[r + 1][i] + row_cnt.get(ch, 0)
    pairs = list(combinations(range(GRID), stars))
    hit = []

    def rec(r, chosen, colc, regc):
        if hit:
            return
        if r == GRID:
            if [tuple(x) for x in chosen] != answer_rows:
                hit.append([tuple(x) for x in chosen])
            return
        left = GRID - r
        if any(stars - colc[c] > left for c in range(GRID)):
            return
        if any(stars - regc[i] > suffix[r][i] for i in range(len(letters))):
            return
        for combo in pairs:
            if any(colc[c] >= stars for c in combo):
                continue
            add = Counter(idx[region_map[r][c]] for c in combo)
            if any(regc[i] + v > stars for i, v in add.items()):
                continue
            for c in combo:
                colc[c] += 1
            for i, v in add.items():
                regc[i] += v
            chosen.append(combo)
            rec(r + 1, chosen, colc, regc)
            chosen.pop()
            for c in combo:
                colc[c] -= 1
            for i, v in add.items():
                regc[i] -= v

    rec(0, [], [0] * GRID, [0] * len(letters))
    return hit[0] if hit else None


def to_grid(bits):
    return [bits[r * GRID:(r + 1) * GRID] for r in range(GRID)]


def check_solution(bits, region_map):
    """Independent constraint check, not using the netlist at all."""
    g = [[int(x) for x in row] for row in to_grid(bits)]
    rows = [sum(r) for r in g]
    cols = [sum(g[r][c] for r in range(GRID)) for c in range(GRID)]
    reg = Counter()
    for r in range(GRID):
        for c in range(GRID):
            if g[r][c]:
                reg[region_map[r][c]] += 1
    touching = [(r, c) for r in range(GRID) for c in range(GRID) if g[r][c]
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if (dr or dc) and 0 <= r + dr < GRID and 0 <= c + dc < GRID
                and g[r + dr][c + dc]]
    return {"popcount": sum(rows), "rows": rows, "cols": cols, "regions": dict(sorted(reg.items())),
            "touching_pairs": len(touching) // 2,
            "ok": all(v == 2 for v in rows) and all(v == 2 for v in cols)
                  and len(reg) == 11 and all(v == 2 for v in reg.values()) and not touching}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netlist", default=str(paths.PUZZLE_OUT / "03_netlist.json"))
    ap.add_argument("--outdir", default=str(paths.PUZZLE_OUT))
    ap.add_argument("--skip-flips", action="store_true")
    a = ap.parse_args(argv)

    print("== recovering the region map from the netlist ==")
    an = Analysis(a.netlist, quiet=True)
    rec = an.recover_regions()
    rmap = rec["map"]
    for row in rmap:
        print("   " + " ".join(row))
    sizes = Counter(c for row in rmap for c in row)
    print(f"   regions={len(sizes)} sizes={dict(sorted(sizes.items()))} "
          f"covers={sum(sizes.values())}/121 connected={check_connected(rmap)}")

    print("\n== solving ==")
    sols = solve_star_battle(rmap)
    print(f"   solutions found: {len(sols)}")
    if len(sols) != 1:
        print("   !! expected exactly one solution")
        return 1
    bits = to_bits(sols[0])
    print(f"\n   answer ({len(bits)} bits, one per clock while enable is high):")
    print(f"   {bits}")
    print()
    for row in to_grid(bits):
        print("   " + " ".join("*" if ch == "1" else "." for ch in row))

    chk = check_solution(bits, rmap)
    print(f"\n   constraint check: {chk['ok']}  popcount={chk['popcount']} "
          f"rows={set(chk['rows'])} cols={set(chk['cols'])} "
          f"regions={set(chk['regions'].values())} touching={chk['touching_pairs']}")

    print("\n== verifying on the gate netlist ==")
    sim = GateSim(a.netlist)
    res = sim.run(standard_stimulus(bits))
    msg, succ = message_of(res), max(x[1] for x in res)
    print(f"   success={succ}  message={msg!r}")
    ok = succ == 1 and msg == WIN_MESSAGE

    print("\n== other responses ==")
    for label, s in (("all ones", "1" * N_BITS), ("all zeros", "0" * N_BITS)):
        r = sim.run(standard_stimulus(s))
        print(f"   {label:10s} success={max(x[1] for x in r)} message={message_of(r)!r}")

    if not a.skip_flips:
        print("\n== perturbation sweep ==")
        flips = [bits[:i] + str(1 - int(bits[i])) + bits[i + 1:] for i in range(N_BITS)]
        b = sim.run_batch(flips)
        n_succ = int(b["success"].sum())
        print(f"   {N_BITS} single-bit flips: success in {n_succ} cases (must be 0)")
        ok &= n_succ == 0

    # the near-miss branch: counts all correct but stars touching.  The exact message
    # depends on the genuinely floating net n293 (one character differs by polarity),
    # so both polarities are checked against the intended "TWO NOT TOUCH".
    print("\n== near-miss (TWO NOT TOUCH branch) ==")
    answer_rows = [tuple(c for c in range(GRID) if bits[r * GRID + c] == "1")
                   for r in range(GRID)]
    nm = find_near_miss(rmap, answer_rows)
    near_bits, near_msgs = None, {}
    if nm is None:
        print("   unreachable: no count-valid placement other than the solution")
    else:
        near_bits = to_bits(nm)
        for pol in (0, 1):
            s2 = GateSim(a.netlist, undriven=pol)
            r2 = sim.run(standard_stimulus(near_bits)) if pol == sim.undriven_value \
                else s2.run(standard_stimulus(near_bits))
            m2 = message_of(r2)
            near_msgs[pol] = m2
            good = max(x[1] for x in r2) == 0 and re.fullmatch(r"TWO.NOT TOUC.", m2)
            print(f"   n293={pol}: success={max(x[1] for x in r2)} message={m2!r} "
                  f"{'ok' if good else 'UNEXPECTED'}")
            ok &= bool(good)

    os.makedirs(a.outdir, exist_ok=True)
    json.dump({"bits": bits, "grid": to_grid(bits), "region_map": rmap,
               "message": msg, "check": chk,
               "near_miss": {"bits": near_bits, "messages": near_msgs}},
              open(os.path.join(a.outdir, "solution.json"), "w"), indent=1)

    # solution.vcd uses example_inputs.vcd's exact protocol (one idle cycle between
    # reset release and enable), so embed the response simulated on that protocol -
    # and require it to win there too (prove.py shows the protocols are equivalent).
    vcd = os.path.join(a.outdir, "solution.vcd")
    res_vcd = sim.run(standard_stimulus(bits, idle_after_reset=1))
    vcd_ok = max(x[1] for x in res_vcd) == 1 and message_of(res_vcd) == WIN_MESSAGE
    ok &= vcd_ok
    print(f"\n   VCD protocol (reset, 1 idle cycle, 121 bits - as the example): "
          f"success={max(x[1] for x in res_vcd)}")
    vcdtool.write_vcd(vcd, bits, res_vcd, comment="winning input for puzzle.gds")
    print(f"\nwrote {a.outdir}/solution.json")
    print(f"wrote {vcd}")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
