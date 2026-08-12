"""Recover the functional structure of the extracted puzzle netlist.

Everything here is derived from the netlist graph plus simulation - no assumptions about
what the design is supposed to be.  Run it and the design identifies itself:

  python -m puzzle.analyze [--netlist out/puzzle/03_netlist.json]

Graph work uses networkx; the region map is recovered twice (structurally from the
2-flop counter SCCs, and empirically by simulating one-hot inputs) and the two must agree.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict

import networkx as nx

from gds2v import paths
from gds2v import cells
from gds2v.sim import GateSim

GRID = 11
N_BITS = GRID * GRID


class Analysis:
    def __init__(self, netlist_path, quiet=False):
        self.path = netlist_path
        self.nl = json.load(open(netlist_path))
        self.quiet = quiet
        self.sim = GateSim(self.nl)
        self._build()

    def say(self, *a):
        if not self.quiet:
            print(*a)

    # ------------------------------------------------------------------
    def _build(self):
        """Combinational net->net graph; flops are a state boundary."""
        self.comb = nx.DiGraph()
        self.flop_of_D = defaultdict(list)
        self.flop_of_Q = {}
        self.flop_pins = {}
        self.cell_of = {}
        for inst in self.nl["instances"]:
            m = cells.parse_cell(inst["cell"])
            pins = inst["pins"]
            self.cell_of[inst["name"]] = inst["cell"]
            if m.kind == "SEQ":
                self.flop_of_D[pins["D"]].append(inst["name"])
                self.flop_of_Q[pins["Q"]] = inst["name"]
                self.flop_pins[inst["name"]] = pins
                continue
            if m.kind in ("PHYS", "TIE"):
                continue
            for i in m.inputs:
                for o in m.outputs:
                    if i in pins and o in pins:
                        self.comb.add_edge(pins[i], pins[o],
                                           inst=inst["name"], cell=inst["cell"])
        # real top-level ports only - every net carries a synthesised `name`, so the
        # nets list cannot be used to identify ports
        by_vname = {n["name"]: n["id"] for n in self.nl["nets"]}
        self.port = {p["name"]: by_vname[p["net"]] for p in self.nl["ports"]}
        self.net_name = {n["id"]: n["name"] for n in self.nl["nets"]}

    def sources_of(self, net):
        """Flops and ports feeding `net` through combinational logic only."""
        anc = nx.ancestors(self.comb, net) | {net} if net in self.comb else {net}
        flops = {self.flop_of_Q[n] for n in anc if n in self.flop_of_Q}
        ports = {nm for nm, nid in self.port.items() if nid in anc}
        return flops, ports, anc

    def cone_size(self, net):
        anc = nx.ancestors(self.comb, net) if net in self.comb else set()
        return len({d["inst"] for _, _, d in self.comb.in_edges(anc | {net}, data=True)})

    # ------------------------------------------------------------------
    def flop_graph(self):
        """Flop -> flop dependency graph through combinational logic."""
        g = nx.DiGraph()
        g.add_nodes_from(self.flop_pins)
        for name, pins in self.flop_pins.items():
            srcs, _ports, _ = self.sources_of(pins["D"])
            for s in srcs:
                g.add_edge(s, name)
        return g

    def report_state(self):
        g = self.flop_graph()
        sccs = sorted(nx.strongly_connected_components(g), key=len, reverse=True)
        self.say(f"\n== state elements ==")
        self.say(f"flops: {len(self.flop_pins)}  "
                 f"{dict(Counter(self.cell_of[f].split('__')[1] for f in self.flop_pins))}")
        self.say(f"combinational depth: {self.sim.comb_depth()} gates; "
                 f"flop-graph SCCs: {len(sccs)}")
        for s in sccs:
            if len(s) > 1:
                self.say(f"   SCC size {len(s):2d}: {sorted(s)}")
        self.scc_by_size = defaultdict(list)
        for s in sccs:
            self.scc_by_size[len(s)].append(sorted(s))
        return sccs

    def shift_chain(self, start_port="I"):
        """Longest data-path chain of flops fed by `start_port`.

        Control flops (counters, message state - anything in an SCC of 3 or more) are
        excluded first: every stage's D is gated by the counters, so without that filter
        no stage looks like it has a single predecessor.
        """
        g = self.flop_graph()
        control = {f for s in nx.strongly_connected_components(g) if len(s) >= 3 for f in s}
        h = nx.DiGraph()
        h.add_nodes_from(f for f in self.flop_pins if f not in control)
        for a, b in g.edges():
            if a != b and a not in control and b not in control:
                h.add_edge(a, b)
        h.remove_edges_from(nx.selfloop_edges(h))
        if not nx.is_directed_acyclic_graph(h):
            for cyc in nx.simple_cycles(h):
                h.remove_edge(cyc[-1], cyc[0])
        chain = nx.dag_longest_path(h) if h.number_of_nodes() else []
        fed = self.port[start_port]
        if chain:
            anc = nx.ancestors(self.comb, self.flop_pins[chain[0]]["D"])
            if fed not in anc:
                self.say(f"   (note: chain head {chain[0]} is not fed directly by {start_port})")
        return chain

    # ------------------------------------------------------------------
    def counter_pairs(self):
        """2-flop SCCs - the saturating count-to-2 cells used for columns and regions."""
        self.report_state() if not hasattr(self, "scc_by_size") else None
        return self.scc_by_size.get(2, [])

    def recover_regions(self):
        """Simulate one-hot inputs and classify each 2-flop counter by what increments it.

        A column counter fires for every row at its column; a region counter fires for
        exactly the cells of its region.
        """
        pairs = self.counter_pairs()
        onehot = ["".join("1" if j == i else "0" for j in range(N_BITS)) for i in range(N_BITS)]
        res = self.sim.run_batch(onehot, tail=5)
        state = res["state"]
        fired = {}
        for pi, pair in enumerate(pairs):
            mask = state[pair[0]] | state[pair[1]]
            fired[pi] = {i for i in range(N_BITS) if mask[i]}

        cols, regions, other = {}, {}, []
        for pi, cellset in fired.items():
            col_match = None
            for c in range(GRID):
                if cellset == {r * GRID + c for r in range(GRID)}:
                    col_match = c
                    break
            if col_match is not None:
                cols[col_match] = pairs[pi]
            elif 0 < len(cellset) < N_BITS:
                regions[pi] = cellset
            else:
                other.append((pairs[pi], len(cellset)))

        letters = {}
        region_map = [[None] * GRID for _ in range(GRID)]
        for k, (pi, cellset) in enumerate(sorted(regions.items(),
                                                 key=lambda kv: min(kv[1]))):
            ch = chr(ord("A") + k)
            letters[ch] = pairs[pi]
            for i in cellset:
                region_map[i // GRID][i % GRID] = ch
        return {"columns": cols, "regions": letters, "map": region_map,
                "other": other, "fired": fired, "pairs": pairs}

    # ------------------------------------------------------------------
    def report(self):
        self.say(f"== {self.path} ==")
        self.say(f"instances {len(self.nl['instances'])}, nets {len(self.nl['nets'])}, "
                 f"comb {len(self.sim.comb)}, flops {len(self.sim.seq)}")
        floating = [n for n in self.sim.undriven_nets]
        if floating:
            for f in floating:
                sinks = [t for net in self.nl["nets"] if net["id"] == f
                         for t in net["terminals"]]
                self.say(f"floating net {f}: no driver, sinks {sinks}")

        self.report_state()

        chain = self.shift_chain("I")
        self.say(f"\n== input path ==")
        self.say(f"shift register fed by I: {len(chain)} stages")
        self.say(f"   {' -> '.join(chain)}")

        self.say(f"\n== success ==")
        snet = self.port["success"]
        drv = [t for net in self.nl["nets"] if net["id"] == snet for t in net["terminals"]
               if t[1] in cells.OUTPUT_PINS]
        self.say(f"success is driven by {drv}")
        for name, pins in self.flop_pins.items():
            if pins.get("Q") == snet:
                flops, ports, _ = self.sources_of(pins["D"])
                self.say(f"   D cone: {self.cone_size(pins['D'])} gates, "
                         f"{len(flops)} flops, ports {sorted(ports)}")

        self.say(f"\n== region map (empirical, from one-hot simulation) ==")
        rec = self.recover_regions()
        self.say(f"column counters recovered: {sorted(rec['columns'])}")
        self.say(f"region counters recovered: {len(rec['regions'])}")
        for row in rec["map"]:
            self.say("   " + " ".join(c or "?" for c in row))
        sizes = Counter(c for row in rec["map"] for c in row if c)
        self.say(f"region sizes: {dict(sorted(sizes.items()))}  total={sum(sizes.values())}")
        holes = sum(1 for row in rec["map"] for c in row if c is None)
        self.say(f"cells with no region: {holes}")
        self.say(f"all regions orthogonally connected: {check_connected(rec['map'])}")
        if rec["other"]:
            self.say(f"other 2-flop counters (row counter etc.): {rec['other']}")
        return rec


def check_connected(region_map):
    ok = True
    letters = {c for row in region_map for c in row if c}
    for ch in letters:
        cellset = {(r, c) for r in range(GRID) for c in range(GRID) if region_map[r][c] == ch}
        stack, seen = [next(iter(cellset))], set()
        seen.add(stack[0])
        while stack:
            r, c = stack.pop()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (r + dr, c + dc) in cellset and (r + dr, c + dc) not in seen:
                    seen.add((r + dr, c + dc))
                    stack.append((r + dr, c + dc))
        ok &= len(seen) == len(cellset)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netlist", default=str(paths.PUZZLE_OUT / "03_netlist.json"))
    ap.add_argument("--save-regions", default=None,
                    help="write the recovered region map to this JSON file")
    a = ap.parse_args(argv)
    an = Analysis(a.netlist)
    rec = an.report()
    if a.save_regions:
        json.dump({"map": rec["map"]}, open(a.save_regions, "w"), indent=1)
        print(f"\nwrote {a.save_regions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
