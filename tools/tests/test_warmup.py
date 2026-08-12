"""Verify gds2v against the complete warmup ground-truth ladder.

  python test_warmup.py [-v]

The warmup ships every intermediate stage the puzzle hides, so each one is checked:

  00_source.v                    behavioural: the extracted netlist must ACT like the RTL
                                 (two 8-bit shift registers, adder, S = (a+b == 496))
  01_netlist.v                   structural: net partition identical up to renaming
  02_netlist_with_power_rails.v  structural: same check (02 is 01 plus supply pins)
  03_post_place_and_route.def    placement: 230/230 instances matched by coordinate

Input is only 04_final.gds; everything else is used as the answer key. Exits non-zero
on any failure.
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for sibling test modules
from gds2v import emit, paths
from gds2v.extract import Extraction
from gds2v.sim import GateSim
from gds2v.validate import validate, roundtrip
from testutil import Checks

HERE = str(paths.TOOLS_DIR)     # where out/ lives
REPO = str(paths.REPO_ROOT)
WARMUP = str(paths.WARMUP_DIR)


class Golden:
    """Reference model of warmup/00_source.v.

    a_reg/b_reg shift left with serial_in as the new LSB while en is high;
    async active-low reset clears both; S = (a_reg + b_reg == 496), combinational.
    """

    def __init__(self):
        self.a = 0
        self.b = 0

    def step(self, rst_n, en, A, B):
        if not rst_n:
            self.a = self.b = 0
        elif en:
            self.a = ((self.a << 1) | A) & 0xFF
            self.b = ((self.b << 1) | B) & 0xFF

    def S(self):
        return int(self.a + self.b == 496)


def behavioural_compare(sim, traces):
    """Run each (label, [(rst_n, en, A, B)]) trace on netlist and golden; count diffs."""
    bad = []
    for label, trace in traces:
        got = sim.run_named(({"rst_n": r, "en": e, "A": a, "B": b} for r, e, a, b in trace),
                            outputs=["S"])
        gold = Golden()
        for i, (r, e, a, b) in enumerate(trace):
            gold.step(r, e, a, b)
            if got[i][0] != gold.S():
                bad.append((label, i, got[i][0], gold.S()))
                break
    return bad


def shift_trace(a, b, lead_in=3, tail=4):
    """Reset, then shift a and b in MSB-first over 8 enabled cycles."""
    t = [(0, 0, 0, 0)] * lead_in
    for k in range(7, -1, -1):
        t.append((1, 1, (a >> k) & 1, (b >> k) & 1))
    t += [(1, 0, 0, 0)] * tail
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    c = Checks()

    gds = os.path.join(WARMUP, "04_final.gds")
    print(f"== extracting {gds} ==")
    e = Extraction(gds, verbose=a.verbose)
    c.check("extraction: 230 instances", len(e.instances) == 230)
    c.check("extraction: no unresolved pins", not e.misses)
    c.check("extraction: 84 signal nets", len(e.signal_nets()) == 84,
          str(len(e.signal_nets())))
    c.check("extraction: every net has exactly one driver", not e.undriven_nets())

    # emitted structural Verilog, reused for both round-trip checks
    nl = emit.build(e)
    out_v = os.path.join(HERE, "out", "warmup", "03_netlist.v")
    os.makedirs(os.path.dirname(out_v), exist_ok=True)
    open(out_v, "w").write(emit.to_verilog(nl))

    def_path = os.path.join(WARMUP, "03_post_place_and_route.def")
    for ref, tag in (("01_netlist.v", "01"), ("02_netlist_with_power_rails.v", "02")):
        print(f"\n== structural check vs {ref} + DEF ==")
        rep, idmap, gold = validate(e, def_path, os.path.join(WARMUP, ref))
        if a.verbose:
            print("   " + "\n   ".join(rep))
        text = "\n".join(rep)
        c.check(f"{tag}: 230/230 instances matched to DEF coordinates",
              "instance match: 230/230" in text)
        c.check(f"{tag}: net partition identical", "partition identical: True" in text)
        c.check(f"{tag}: zero mismatches", "MISMATCHES: 0" in text)
        rt = "\n".join(roundtrip(out_v, idmap, gold))
        c.check(f"{tag}: emitted Verilog round-trips",
              "round-trip partition identical: True" in rt
              and "cell-type census identical: True" in rt)

    print("\n== behavioural check vs 00_source.v ==")
    sim = GateSim(emit.to_json(nl))
    c.check("netlist levelises with no combinational loops",
          len(sim.order) == len(sim.comb))

    # directed: every (a, b) pair with a+b == 496, plus the near misses 495 and 497
    directed = []
    for total in (495, 496, 497):
        for va in range(max(0, total - 255), min(255, total) + 1):
            directed.append((va, total - va))
    traces = [(f"a={va} b={vb} (sum {va + vb})", shift_trace(va, vb)) for va, vb in directed]
    bad = behavioural_compare(sim, traces)
    c.check(f"directed: {len(directed)} shift-in cases (sums 495/496/497) match RTL",
          not bad, str(bad[:3]))
    hits = [t for t in directed if t[0] + t[1] == 496]
    ok_s = all(sim.run_named((dict(zip(("rst_n", "en", "A", "B"), c)) for c in shift_trace(va, vb)),
                             outputs=["S"])[-1][0] == 1 for va, vb in hits[:5])
    c.check("S goes high for a+b == 496 loads", ok_s)

    # randomised: long traces with en toggling and mid-run resets
    rnd = random.Random(1)
    traces = []
    for t in range(40):
        trace = [(0, 0, 0, 0)] * 2
        for _ in range(200):
            trace.append((0 if rnd.random() < 0.01 else 1,
                          int(rnd.random() < 0.7), rnd.getrandbits(1), rnd.getrandbits(1)))
        traces.append((f"random {t}", trace))
    bad = behavioural_compare(sim, traces)
    c.check("randomised: 40 x 202-cycle traces (resets, en gaps) match RTL",
          not bad, str(bad[:3]))

    # the emitted behavioural Verilog TEXT, evaluated independently, vs the RTL golden
    print("\n== emitted 04_behavioral.v text vs 00_source.v ==")
    behav_v = os.path.join(HERE, "out", "warmup", "04_behavioral.v")
    open(behav_v, "w").write(emit.to_behavioral_verilog(nl))
    from test_behavioral import VerilogEval
    ev = VerilogEval(behav_v)
    c.check("behavioural RTL parses: 63 assigns + 16 always blocks",
          len(ev.assigns) == 63 and len(ev.flops) == 16,
          f"{len(ev.assigns)} assigns, {len(ev.flops)} flops")
    bad = []
    for label, trace in [(f"a={va} b={vb}", shift_trace(va, vb)) for va, vb in directed] \
            + traces[:10]:
        got = ev.run([{"rst_n": r, "en": e, "A": x, "B": y, "clk": 0}
                      for r, e, x, y in trace], ["S"])
        gold = Golden()
        for i, (r, e, x, y) in enumerate(trace):
            gold.step(r, e, x, y)
            if got[i][0] != gold.S():
                bad.append((label, i, got[i][0], gold.S()))
                break
    c.check(f"emitted Verilog text matches 00_source.v behaviour "
                f"({len(directed)} directed + 10 random traces)", not bad, str(bad[:3]))

    # ---- the lifted RTL: structure recovery all the way back to source form ----
    print("\n== lifted RTL (06_rtl_recovered.v) ==")
    from gds2v.lift import Lifter, lint_verilog, verify_shift_semantics
    nl_json = emit.to_json(nl)
    lf = Lifter(nl_json).run()
    srs = sorted((r["width"], r["serial"]) for r in lf.registers)
    c.check("lift: two 8-bit shift registers with serial inputs A and B",
          srs == [(8, "A"), (8, "B")], str(srs))
    t = lf.templates[0] if lf.templates else {}
    c.check("lift: S proven == (sr_a + sr_b == 496) over all 2^16 states",
          t.get("kind") == "sum_eq" and t.get("constant") == 496
          and "exhaustive" in t.get("proof", ""), str(t))
    stim = [{"rst_n": 0, "en": 0, "A": 0, "B": 0, "clk": 0}] * 2 + \
           [{"rst_n": 0 if rnd.random() < 0.02 else 1, "en": int(rnd.random() < 0.7),
             "A": rnd.getrandbits(1), "B": rnd.getrandbits(1), "clk": 0}
            for _ in range(2000)]
    bad = verify_shift_semantics(sim, lf, stim)
    c.check("lift: shift semantics proven dynamically over 2002 cycles", bad == 0,
          f"{bad} mismatching register-cycles")
    text = lf.to_verilog()
    c.check("lift: everything lifted (0 flat gates, 0 flat flops), lint clean",
          not lf.leftover and not lf.unlifted and not lint_verilog(text),
          f"leftover={len(lf.leftover)} unlifted={len(lf.unlifted)} "
          f"undeclared={sorted(lint_verilog(text))[:4]}")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
