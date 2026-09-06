"""Validate the flow on an open-source ASIC design with published ground truth.

  python test_opensource.py [--dir out/opensource/caravel]

Target: Efabless caravel_user_project's `user_proj_example` (a 16-bit wishbone
counter), hardened with OpenLane on sky130_fd_sc_hd.  The repo publishes everything
needed to grade us: the GDS (input), the DEF and gate-level netlist (structural
ground truth), and the RTL (behavioural ground truth).

Files expected in --dir (fetch once, ~160 MB total):
  https://raw.githubusercontent.com/efabless/caravel_user_project/main/gds/user_proj_example.gds
  .../def/user_proj_example.def
  .../verilog/gl/user_proj_example.v   -> user_proj_example.gl.v
  .../verilog/rtl/user_proj_example.v  -> user_proj_example.rtl.v

Known, deliberate scope limits (asserted, not hidden):
  * fill/decap/tap instances are pruned (487k placements, <1% logic) - the
    structural comparison covers every SIGNAL-bearing cell
  * the design has a muxed clock (la_oenb[64] selects wb_clk_i or la_data_in[64]);
    the simulator is single-clock, so behavioural co-sim drives la_oenb[64]=1
"""
import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for sibling test modules
from gds2v import emit, paths
from gds2v.extract import Extraction
from gds2v.sim import GateSim
from gds2v.validate import validate
from testutil import Checks

HERE = str(paths.TOOLS_DIR)     # where out/ lives

BITS = 16


class GoldenCounter:
    """Statement-for-statement mirror of user_proj_example.rtl.v (BITS=16)."""

    def __init__(self):
        self.count = 0
        self.ready = 0
        self.rdata = 0

    def step(self, i):
        """i: dict of input port values (la_oenb[64] and [65] assumed 1)."""
        valid = i["wbs_cyc_i"] & i["wbs_stb_i"]
        wstrb = [i[f"wbs_sel_i[{k}]"] & i["wbs_we_i"] for k in range(4)]
        la_write = [(1 - i[f"la_oenb[{48 + k}]"]) & (1 - valid) for k in range(BITS)]
        la_input = [i[f"la_data_in[{48 + k}]"] for k in range(BITS)]
        wdata = sum(i[f"wbs_dat_i[{k}]"] << k for k in range(BITS))
        rst = i["wb_rst_i"]

        if rst:
            self.count = 0
            self.ready = 0
        else:
            new_ready = 0
            new_count = self.count
            if not any(la_write):
                new_count = (self.count + 1) & 0xFFFF
            if valid and not self.ready:
                new_ready = 1
                self.rdata = self.count
                if wstrb[0]:
                    new_count = (new_count & 0xFF00) | (wdata & 0x00FF)
                if wstrb[1]:
                    new_count = (new_count & 0x00FF) | (wdata & 0xFF00)
            elif any(la_write):
                new_count = sum(la_write[k] & la_input[k] and (1 << k)
                                for k in range(BITS))
            self.count = new_count
            self.ready = new_ready

    def outputs(self):
        o = {}
        for k in range(BITS):
            o[f"io_out[{k}]"] = (self.count >> k) & 1
            o[f"la_data_out[{k}]"] = (self.count >> k) & 1
        for k in range(BITS, 128):
            o[f"la_data_out[{k}]"] = 0
        o["wbs_ack_o"] = self.ready
        for k in range(32):
            o[f"wbs_dat_o[{k}]"] = (self.rdata >> k) & 1 if k < BITS else 0
        return o


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.join(HERE, "out", "opensource", "caravel"))
    ap.add_argument("--cycles", type=int, default=400)
    a = ap.parse_args(argv)
    c = Checks()
    gds = os.path.join(a.dir, "user_proj_example.gds")
    if not os.path.exists(gds):
        print(f"missing {gds} - fetch the files listed in the docstring first")
        return 2

    print(f"== extracting {os.path.basename(gds)} (487k placements, pruned) ==")
    t0 = time.time()
    e = Extraction(gds, verbose=True, prune_physical=True)
    print(f"  extraction took {time.time() - t0:.0f}s")
    c.check("extraction completes with instances and nets",
            len(e.instances) > 0 and len(e.nets) > 0,
            f"{len(e.instances)} instances, {len(e.nets)} nets")
    c.check("no unresolved pins", not e.misses, f"{len(e.misses)} misses")
    logic = e.logic_instances()
    c.check("~700 signal-bearing cells recovered",
          600 < len(logic) < 1300, f"{len(logic)} logic instances "
          f"({len(e.instances)} total after pruning)")

    print("\n== structural check vs DEF + gate-level netlist ==")
    rep, idmap, gold = validate(e, os.path.join(a.dir, "user_proj_example.def"),
                                os.path.join(a.dir, "user_proj_example.gl.v"))
    print("   " + "\n   ".join(rep))
    text = "\n".join(rep)
    c.check("net partition identical to the published netlist (signal pins)",
          "partition identical: True" in text)
    c.check("zero net mismatches", "MISMATCHES: 0" in text)

    print("\n== behavioural check vs RTL (16-bit wishbone counter) ==")
    nl = emit.build(e)
    sim = GateSim(emit.to_json(nl))
    c.check("netlist levelises (no combinational loops)",
          len(sim.order) == len(sim.comb))

    in_ports = [p["name"] for p in nl["ports"] if p["dir"] == "input"]
    out_check = [f"io_out[{k}]" for k in range(BITS)] + ["wbs_ack_o"] + \
                [f"wbs_dat_o[{k}]" for k in range(BITS)] + \
                [f"la_data_out[{k}]" for k in range(BITS)]
    missing = [p for p in out_check if p not in sim.port]
    c.check("all compared output ports present", not missing, str(missing[:5]))

    rnd = random.Random(2)

    def stim_cycle(rst, wb=None, la=None):
        i = {p: 0 for p in in_ports}
        for k in range(128):
            i[f"la_oenb[{k}]"] = 1          # LA disabled: clk=wb_clk, rst=wb_rst
        i["wb_rst_i"] = rst
        if wb:
            i.update(wb)
        if la:
            i.update(la)
        return i

    trace = [stim_cycle(1)] * 3
    for _ in range(a.cycles):
        kind = rnd.random()
        if kind < 0.05:
            trace.append(stim_cycle(1))
        elif kind < 0.25:                    # wishbone write/read
            wb = {"wbs_cyc_i": 1, "wbs_stb_i": 1, "wbs_we_i": rnd.getrandbits(1)}
            for k in range(4):
                wb[f"wbs_sel_i[{k}]"] = rnd.getrandbits(1)
            for k in range(32):
                wb[f"wbs_dat_i[{k}]"] = rnd.getrandbits(1)
            trace.append(stim_cycle(0, wb=wb))
        elif kind < 0.35:                    # LA load of the counter
            la = {}
            for k in range(BITS):
                la[f"la_oenb[{48 + k}]"] = rnd.getrandbits(1)
                la[f"la_data_in[{48 + k}]"] = rnd.getrandbits(1)
            trace.append(stim_cycle(0, la=la))
        else:
            trace.append(stim_cycle(0))

    t0 = time.time()
    got = sim.run_named(trace, out_check)
    gold_model = GoldenCounter()
    bad = 0
    first = None
    for i, stim in enumerate(trace):
        gold_model.step(stim)
        exp = gold_model.outputs()
        expected = tuple(exp[p] for p in out_check)
        if got[i] != expected and i >= 3:
            bad += 1
            if first is None:
                first = (i, [(p, g, x) for p, g, x in
                             zip(out_check, got[i], expected) if g != x][:4])
    c.check(f"cycle-for-cycle match with the RTL over {len(trace)} cycles "
                f"({time.time() - t0:.0f}s)", bad == 0,
          f"{bad} mismatching cycles, first: {first}")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
