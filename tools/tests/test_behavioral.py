"""Verify the de-synthesised behavioural RTL (04_behavioral.v) against the netlist.

  python test_behavioral.py

The emitted file's TEXT is what gets checked: a small independent evaluator parses the
assign/always statements back out of the Verilog, executes them, and co-simulates
against the gate-level netlist simulator cycle for cycle.  A bug in expression
emission (precedence, inversion placement, pin ordering) would show up here.
"""
import json
import os
import random
import re
import sys

import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for sibling test modules
from gds2v import paths
from gds2v.sim import GateSim, standard_stimulus
from testutil import Checks
from puzzle import vcdtool

HERE = str(paths.TOOLS_DIR)     # where out/ lives

IDENT = r"[A-Za-z_]\w*(?:\[\d+\])?"


class VerilogEval:
    """Executes the machine-generated behavioural RTL emitted by gds2v.

    Handles exactly the constructs that emitter produces: continuous assigns over
    ~ & | ^ ?: and parentheses, plus the three flop always-block shapes.
    """

    def __init__(self, path):
        src = open(path).read()
        self.inputs, self.outputs = [], []
        for m in re.finditer(r"^\s*(input|output)(?:\s+reg)?\s*(\[\d+:\d+\])?\s+(\w+);",
                             src, re.M):
            dirn, bus, name = m.groups()
            names = [name] if not bus else \
                [f"{name}[{i}]" for i in range(int(bus[1:-1].split(":")[1]),
                                               int(bus[1:-1].split(":")[0]) + 1)]
            (self.inputs if dirn == "input" else self.outputs).extend(names)

        self.assigns = []          # (target, code, identifiers)
        for m in re.finditer(rf"^\s*assign\s+({IDENT})\s*=\s*(.+?);", src, re.M):
            tgt, expr = m.group(1), m.group(2)
            expr = expr.replace("1'b1", "1").replace("1'b0", "0")
            tm = re.fullmatch(rf"({IDENT})\s*\?\s*({IDENT})\s*:\s*({IDENT})", expr.strip())
            if tm:
                py = f"(_e['{tm.group(2)}'] if _e['{tm.group(1)}'] else _e['{tm.group(3)}'])"
                ids = list(tm.groups())
            else:
                ids = re.findall(IDENT, expr)
                py = re.sub(IDENT, lambda mm: f"_e['{mm.group(0)}']", expr)
                py = re.sub(r"~(_e\['[^']+'\])", r"(1-\1)", py)
                py = py.replace("~(", "1^(")
            self.assigns.append((tgt, compile(py, f"<assign {tgt}>", "eval"), ids))

        self.flops = []            # (q, d, kind, ctrl_net)
        for m in re.finditer(
                rf"always @\(posedge ({IDENT}) or negedge ({IDENT})\)\s*\n"
                rf"\s*if \(!({IDENT})\) ({IDENT}) <= 1'b([01]);\s*\n"
                rf"\s*else ({IDENT}) <= ({IDENT});", src):
            _clk, rstn, _rstn2, q, val, _q2, d = m.groups()
            self.flops.append((q, d, "set" if val == "1" else "reset", rstn))
        for m in re.finditer(rf"always @\(posedge ({IDENT})\) ({IDENT}) <= ({IDENT});", src):
            _clk, q, d = m.groups()
            self.flops.append((q, d, "plain", None))

        # topological order of assigns
        producer = {t: i for i, (t, _c, _ids) in enumerate(self.assigns)}
        g = nx.DiGraph()
        g.add_nodes_from(range(len(self.assigns)))
        for i, (_t, _c, ids) in enumerate(self.assigns):
            for name in ids:
                if name in producer:
                    g.add_edge(producer[name], i)
        self.order = list(nx.topological_sort(g))

        self.idents = set()
        for t, _c, ids in self.assigns:
            self.idents.add(t)
            self.idents.update(ids)
        for q, d, _k, r in self.flops:
            self.idents.update(x for x in (q, d, r) if x)
        self.idents.update(self.inputs + self.outputs)

    def run(self, stimulus, outputs):
        """stimulus: [{input: 0/1}]; -> list of tuples of `outputs` values per cycle."""
        env = {i: 0 for i in self.idents}
        res = []
        for stim in stimulus:
            env.update(stim)

            def settle():
                for i in self.order:
                    tgt, code, _ids = self.assigns[i]
                    env[tgt] = eval(code, {"_e": env})

            settle()
            nxt = {}
            for q, d, kind, ctrl in self.flops:
                v = env[d]
                if kind == "reset" and not env[ctrl]:
                    v = 0
                elif kind == "set" and not env[ctrl]:
                    v = 1
                nxt[q] = v
            env.update(nxt)
            settle()
            res.append(tuple(env[o] for o in outputs))
        return res


def main(argv=None):
    c = Checks()
    rnd = random.Random(11)

    print("== warmup: 04_behavioral.v vs gate netlist ==")
    ev = VerilogEval(os.path.join(HERE, "out", "warmup", "04_behavioral.v"))
    sim = GateSim(os.path.join(HERE, "out", "warmup", "03_netlist.json"))
    c.check(f"parsed {len(ev.assigns)} assigns + {len(ev.flops)} always blocks",
          len(ev.flops) == 16)
    bad = 0
    for _t in range(20):
        trace = [(0, 0, 0, 0)] * 2 + \
            [(0 if rnd.random() < 0.01 else 1, int(rnd.random() < 0.7),
              rnd.getrandbits(1), rnd.getrandbits(1)) for _ in range(150)]
        stim = [{"rst_n": r, "en": e, "A": x, "B": y, "clk": 0} for r, e, x, y in trace]
        got = ev.run(stim, ["S"])
        ref = sim.run_named(stim, ["S"])
        bad += sum(1 for a, b in zip(got, ref) if a != b)
    c.check("20 x 152-cycle random traces match cycle-for-cycle", bad == 0, f"{bad} diffs")

    print("\n== puzzle: 04_behavioral.v vs gate netlist ==")
    ev = VerilogEval(os.path.join(HERE, "out", "puzzle", "04_behavioral.v"))
    sim = GateSim(os.path.join(HERE, "out", "puzzle", "03_netlist.json"))
    c.check(f"parsed {len(ev.assigns)} assigns + {len(ev.flops)} always blocks",
          len(ev.flops) == 92)
    outs = [f"O[{b}]" for b in range(8)] + ["success"]

    def compare(stim_tuples, label):
        stim = [{"rst_n": r, "enable": e, "I": i, "clk": 0} for r, e, i in stim_tuples]
        got = ev.run(stim, outs)
        ref = sim.run_named(stim, outs)
        d = sum(1 for a, b in zip(got, ref) if a != b)
        return c.check(label, d == 0, f"{d} diffs / {len(stim)} cycles")

    compare(vcdtool.stimulus_from_vcd(str(paths.EXAMPLE_VCD)),
            "example_inputs.vcd stimulus matches")
    sol = json.load(open(os.path.join(HERE, "out", "puzzle", "solution.json")))
    compare(standard_stimulus(sol["bits"]), "winning input matches (incl success)")
    compare(standard_stimulus("1" * 121, tail=20), "all-ones matches")
    compare(standard_stimulus("0" * 121, tail=20), "all-zeros matches")
    bad = 0
    for _t in range(30):
        bits = "".join(rnd.choice("01") for _ in range(121))
        stim = [{"rst_n": r, "enable": e, "I": i, "clk": 0}
                for r, e, i in standard_stimulus(bits, tail=15)]
        bad += sum(1 for a, b in zip(ev.run(stim, outs), sim.run_named(stim, outs))
                   if a != b)
    c.check("30 random 121-bit vectors match cycle-for-cycle", bad == 0, f"{bad} diffs")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
