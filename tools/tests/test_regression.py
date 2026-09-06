"""End-to-end regression for the whole flow.

  python test_regression.py [-v]

Runs every check that establishes the result, from GDS in to answer out, and exits
non-zero if any of them fails.
"""
import argparse
import json
import os
import subprocess
import sys
import time

PY = sys.executable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
from testutil import Checks
from gds2v import paths

HERE = str(paths.TOOLS_DIR)     # where out/ lives
REPO = str(paths.REPO_ROOT)

ANSWER_POPCOUNT = 22
WIN = "(* TWO STARS *)"


def run(cmd, cwd=HERE):
    t = time.time()
    p = subprocess.run([PY, "-u"] + cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, time.time() - t


def run_test(name):
    """Run one of the sibling test scripts (which live alongside this file)."""
    return run([os.path.join(TESTS_DIR, name)])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--skip-extract", action="store_true", help="reuse existing out/")
    a = ap.parse_args(argv)
    c = Checks()

    # ---------------------------------------------------------------- extraction
    print("== extraction ==")
    if not a.skip_extract:
        rc, out, dt = run(["-m", "gds2v", os.path.join(REPO, "warmup", "04_final.gds"),
                           "-o", "out/warmup",
                           "--def", os.path.join(REPO, "warmup", "03_post_place_and_route.def"),
                           "--ref", os.path.join(REPO, "warmup", "01_netlist.v")])
        if a.verbose:
            print(out)
        c.check("warmup extraction runs", rc == 0, f"{dt:.1f}s")
        c.check("warmup: 230/230 instances mapped to DEF", "instance match: 230/230" in out)
        c.check("warmup: 84 nets, partition identical to 01_netlist.v",
                "partition identical: True" in out and "gold signal nets: 84" in out)
        c.check("warmup: zero net mismatches", "MISMATCHES: 0" in out)
        c.check("warmup: emitted Verilog round-trips",
                "round-trip partition identical: True" in out
                and "cell-type census identical: True" in out)
        c.check("warmup: every net has exactly one driver",
                "nets without exactly one driver: 0" in out)

        rc, out, dt = run(["-m", "gds2v", os.path.join(REPO, "puzzle.gds"), "-o", "out/puzzle"])
        if a.verbose:
            print(out)
        c.check("puzzle extraction runs", rc == 0, f"{dt:.1f}s")
        c.check("puzzle: 1618 instances, 741 nets",
                "1618 cells" in out and "741 nets" in out)
        c.check("puzzle: no unresolved pins", "unresolved pins: 0" in out)
        # one net (293) is genuinely floating in the layout - see SOLUTION_WRITEUP.md
        c.check("puzzle: exactly one floating net, as documented",
                "nets without exactly one driver: 1" in out)
    else:
        print("  (skipped, reusing out/)")

    # ------------------------------------------------- warmup ground-truth ladder
    print("\n== warmup ground-truth ladder (00/01/02/03) ==")
    rc, out, dt = run_test("test_warmup.py")
    if a.verbose:
        print(out)
    c.check("test_warmup.py: all four reference files verified",
            rc == 0 and "22/22 checks passed" in out, f"{dt:.1f}s")

    # ------------------------------------------ de-synthesised RTL and schematics
    print("\n== behavioural RTL + schematics ==")
    rc, out, dt = run_test("test_behavioral.py")
    if a.verbose:
        print(out)
    c.check("test_behavioral.py: emitted RTL text matches netlist cycle-for-cycle",
            rc == 0 and "8/8 checks passed" in out, f"{dt:.1f}s")
    for tag in ("warmup", "puzzle"):
        c.check(f"{tag}: 04_behavioral.v exists",
                os.path.exists(os.path.join(HERE, "out", tag, "04_behavioral.v")))
        c.check(f"{tag}: 05_schematic.svg exists",
                os.path.exists(os.path.join(HERE, "out", tag, "05_schematic.svg")))
        c.check(f"{tag}: 06_rtl_recovered.v exists",
                os.path.exists(os.path.join(HERE, "out", tag, "06_rtl_recovered.v")))
    p6 = os.path.join(HERE, "out", "puzzle", "06_rtl_recovered.v")
    if os.path.exists(p6):
        t6 = open(p6).read()
        from gds2v.lift import lint_verilog
        c.check("puzzle lift: 12-stage input shift register recovered",
                "reg [11:0] sr_i" in t6)
        c.check("puzzle lift: recovered RTL lint-clean",
                not lint_verilog(t6), str(sorted(lint_verilog(t6)))[:60])

    # ---------------------------------------------------------------- cell models
    print("\n== cell models ==")
    sys.path.insert(0, HERE)
    from gds2v import cells
    for tag in ("warmup", "puzzle"):
        p = os.path.join(HERE, "out", tag, "03_netlist.json")
        n, probs = cells.check_against_netlist(json.load(open(p)))
        c.check(f"{tag}: derived pin names match all {n} cell types",
                not probs, str(probs[:2]) if probs else "")

    # ---------------------------------------------------------------- simulation
    print("\n== simulation vs ground truth ==")
    from gds2v.sim import GateSim, standard_stimulus, message_of
    from puzzle import vcdtool
    sim = GateSim(os.path.join(HERE, "out", "puzzle", "03_netlist.json"))
    ref = os.path.join(REPO, "example_inputs.vcd")
    got = sim.run(vcdtool.stimulus_from_vcd(ref))
    exp = vcdtool.expected_from_vcd(ref)
    n = sum(1 for o, s in exp if o is not None)
    mis = sum(1 for (a1, b1), (a2, b2) in zip(got, exp)
              if a2 is not None and (a1 != a2 or b1 != b2))
    c.check(f"example_inputs.vcd replays with 0 mismatches over {n} cycles", mis == 0,
            f"{mis} mismatches")
    c.check("reference trace emits 'TRY AGAINTRY AGAIN'",
            message_of(got) == "TRY AGAINTRY AGAIN", repr(message_of(got)))

    # ---------------------------------------------------------------- solve
    print("\n== solve ==")
    rc, out, dt = run(["-m", "puzzle.solve"])
    if a.verbose:
        print(out)
    c.check("solve.py runs and passes", rc == 0 and "RESULT: PASS" in out, f"{dt:.1f}s")
    c.check("exactly one Star Battle solution", "solutions found: 1" in out)
    c.check("region map covers 121 cells, all connected",
            "covers=121/121 connected=True" in out)
    c.check("all 121 single-bit flips fail", "success in 0 cases" in out)

    sol = json.load(open(os.path.join(HERE, "out", "puzzle", "solution.json")))
    bits = sol["bits"]
    c.check("answer is 121 bits with popcount 22",
            len(bits) == 121 and bits.count("1") == ANSWER_POPCOUNT,
            f"{len(bits)} bits, popcount {bits.count('1')}")
    res = sim.run(standard_stimulus(bits))
    c.check("answer raises success on the gate netlist", max(x[1] for x in res) == 1)
    c.check(f"answer emits {WIN!r}", message_of(res) == WIN, repr(message_of(res)))
    for label, s, expect in (("all ones", "1" * 121, "BIG BANG"),
                             ("all zeros", "0" * 121, "EMPTY SKY")):
        m = message_of(sim.run(standard_stimulus(s)))
        c.check(f"{label} emits {expect!r}", m == expect, repr(m))

    # the fifth branch: counts right, stars touching.  One character rides on the
    # floating net n293, so both polarities are accepted against the intended text.
    import re as _re
    nm = sol.get("near_miss", {})
    c.check("near-miss input found (TWO NOT TOUCH branch reachable)",
            bool(nm.get("bits")))
    if nm.get("bits"):
        for pol in (0, 1):
            s2 = GateSim(os.path.join(HERE, "out", "puzzle", "03_netlist.json"),
                         undriven=pol)
            r2 = s2.run(standard_stimulus(nm["bits"]))
            m2 = message_of(r2)
            c.check(f"near-miss n293={pol}: success=0, message is a TOUCH variant",
                    max(x[1] for x in r2) == 0
                    and _re.fullmatch(r"TWO.NOT TOUC.", m2) is not None, repr(m2))

    # ---------------------------------------------------------------- vcd writer
    print("\n== generated VCD ==")
    vcd = os.path.join(HERE, "out", "puzzle", "solution.vcd")
    c.check("solution.vcd exists", os.path.exists(vcd))
    if os.path.exists(vcd):
        st = vcdtool.stimulus_from_vcd(vcd)
        r2 = sim.run(st)
        c.check("solution.vcd replays to success=1", max(x[1] for x in r2) == 1)
        c.check("solution.vcd replays to the winning message", message_of(r2) == WIN)
        e2 = vcdtool.expected_from_vcd(vcd)
        m2 = sum(1 for (x1, y1), (x2, y2) in zip(r2, e2)
                 if x2 is not None and (x1 != x2 or y1 != y2))
        c.check("O/success recorded in solution.vcd match re-simulation", m2 == 0)

    c.check("behavioural cell models generated",
            os.path.exists(os.path.join(HERE, "out", "puzzle", "cells_sim.v")))

    # ------------------------------------------------- SAT proofs (z3)
    print("\n== SAT proofs on the circuit (z3) ==")
    rc, out, dt = run(["-m", "puzzle.prove"])
    if a.verbose:
        print(out)
    c.check("prove.py runs and passes", rc == 0 and "RESULT: PASS" in out, f"{dt:.1f}s")
    c.check("answer is the only success input (blocking it is UNSAT)",
            "unique: True" in out)
    c.check("SAT witness equals the solved answer",
            "witness matches solution.json: True" in out)
    c.check("message set proven complete over all 2^121 inputs",
            "messages proven complete: True" in out)
    c.check("BIG BANG / EMPTY SKY trigger exclusivity proven",
            "triggers exclusive: True" in out)
    c.check("success provably independent of the floating net",
            "success independent of floating net: True" in out)
    c.check("success/window independent of un-reset flop power-up state",
            "independent of power-up state: True" in out)
    c.check("success <=> valid Star Battle on the recovered map (SAT)",
            "success <=> valid Star Battle on the recovered map: True" in out)
    c.check("near-miss message <=> counts ok but stars touching (SAT)",
            "<=> counts ok but stars touching: True" in out)
    c.check("constant-folded success samples proven all zero",
            "all zero: True" in out)
    c.check("flop state provably reaches a fixed point within the tail",
            "state reaches a fixed point within the tail: True" in out)
    c.check("example's idle-cycle protocol proven equivalent",
            "gap and back-to-back protocols provably equivalent: True" in out)

    # ------------------------------------------------------- full RTL lift
    print("\n== full RTL lift (07_rtl_lifted.v) ==")
    rc, out, dt = run(["-m", "puzzle.liftrtl"])
    if a.verbose:
        print(out)
    c.check("liftrtl.py runs and passes", rc == 0 and "RESULT: PASS" in out,
            f"{dt:.1f}s")
    c.check("lifted RTL co-simulates cycle-for-cycle with the netlist",
            "cosim exact: True" in out)
    c.check("lifted RTL lint-clean", "lint clean: True" in out)
    c.check("07_rtl_lifted.v exists",
            os.path.exists(os.path.join(HERE, "out", "puzzle", "07_rtl_lifted.v")))

    # ------------------------------------- independent simulator (opt-in)
    rc, out, dt = run_test("test_iverilog.py")
    if "SKIP" in out:
        print("\n== iverilog cross-check == (skipped: no iverilog found)")
    else:
        print("\n== iverilog cross-check ==")
        if a.verbose:
            print(out)
        c.check("test_iverilog.py: netlist + behavioural + lifted RTL all pass "
                "under Icarus", rc == 0 and "3/3 checks passed" in out, f"{dt:.1f}s")

    # ------------------------------------------ generality (any valid GDS)
    print("\n== generality (arbitrary valid GDS) ==")
    rc, out, dt = run_test("test_generality.py")
    if a.verbose:
        print(out)
    c.check("test_generality.py: synthetic stress files handled or degraded honestly",
            rc == 0 and "14/14 checks passed" in out, f"{dt:.1f}s")
    rc, out, dt = run_test("test_llmassist.py")
    if a.verbose:
        print(out)
    c.check("test_llmassist.py: LLM-assist proposals gated deterministically "
            "(fake transport, no network)",
            rc == 0 and "18/18 checks passed" in out, f"{dt:.1f}s")

    # ------------------------------------------ open-source design (opt-in)
    caravel = os.path.join(HERE, "out", "opensource", "caravel", "user_proj_example.gds")
    if os.path.exists(caravel):
        print("\n== open-source design (caravel user_proj_example) ==")
        rc, out, dt = run_test("test_opensource.py")
        if a.verbose:
            print(out)
        c.check("test_opensource.py: extract + validate vs DEF/GL + co-sim vs RTL",
                rc == 0 and "8/8 checks passed" in out, f"{dt:.1f}s")
    else:
        print("\n== open-source design == (skipped: run "
              "tools/fetch_opensource.py to enable)")

    # ---------------------------------------------------------------- figures
    print("\n== figures ==")
    rc, out, dt = run(["-m", "puzzle.visualize"])
    if a.verbose:
        print(out)
    c.check("visualize.py runs", rc == 0, f"{dt:.1f}s")
    for f in ("placement.png", "regions.png", "logo.png", "communities.png"):
        c.check(f"figure {f}", os.path.exists(os.path.join(HERE, "out", "figures", f)))

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
