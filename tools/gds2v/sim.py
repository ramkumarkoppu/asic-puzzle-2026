"""Gate-level simulator for an extracted netlist.

Two evaluation modes share one code path (``cells.CellModel.eval`` parameterised by an
ops object):

  scalar  - plain 0/1 ints, one stimulus at a time
  batch   - numpy bool arrays, one lane per stimulus, all lanes advancing together

Per clock cycle the model is:

  1. evaluate combinational logic with the OLD flop state (inputs are already stable)
  2. capture each flop's D, applying asynchronous reset/set
  3. evaluate combinational logic again with the NEW flop state, and sample the outputs

Step 3 matters: ``O[7:0]`` is combinational from the flops, and the reference VCD records
its new value at the posedge itself.  This model reproduces ``example_inputs.vcd`` exactly.
"""
import json

import networkx as nx
import numpy as np

from . import cells
from .cells import SUPPLY_PINS, ScalarOps


class ArrayOps:
    """Boolean ops on numpy bool arrays - one lane per independent stimulus."""

    def __init__(self, lanes):
        self.ONE = np.ones(lanes, dtype=bool)
        self.ZERO = np.zeros(lanes, dtype=bool)

    @staticmethod
    def inv(a):
        return ~a


class GateSim:
    """Cycle-accurate two-valued simulator over a gds2v netlist JSON."""

    def __init__(self, netlist, undriven=0):
        if isinstance(netlist, (str, bytes)):
            netlist = json.load(open(netlist))
        self.nl = netlist
        self.undriven_value = undriven

        self.nets = {n["id"]: n for n in netlist["nets"]}
        self.n_nets = max(self.nets) + 1
        by_name = {n["name"]: n["id"] for n in netlist["nets"]}
        self.port = {p["name"]: by_name[p["net"]] for p in netlist["ports"]}

        self.comb, self.seq = [], []
        for inst in netlist["instances"]:
            model = cells.parse_cell(inst["cell"])
            pins = {p: v for p, v in inst["pins"].items() if p not in SUPPLY_PINS}
            if model.kind == "PHYS":
                continue
            if model.kind == "SEQ":
                self.seq.append((inst, model, pins))
            else:
                self.comb.append((inst, model, pins))

        self.order = self._levelise()

        driven = {pins[o] for _, m, pins in self.comb for o in m.outputs if o in pins}
        driven |= {pins["Q"] for _, _, pins in self.seq}
        driven |= set(self.port.values())
        self.undriven_nets = [n for n in self.nets
                              if n not in driven and self.nets[n]["name"] not in ("VPWR", "VGND")]

    # ------------------------------------------------------------------
    def _levelise(self):
        """Topological order of combinational instances. Raises if the logic has a loop."""
        producer = {}
        for idx, (_, model, pins) in enumerate(self.comb):
            for o in model.outputs:
                if o in pins:
                    producer[pins[o]] = idx
        g = nx.DiGraph()
        g.add_nodes_from(range(len(self.comb)))
        for idx, (_, model, pins) in enumerate(self.comb):
            for p in model.inputs:
                src = producer.get(pins[p])
                if src is not None:
                    g.add_edge(src, idx)
        if not nx.is_directed_acyclic_graph(g):
            cyc = nx.find_cycle(g)
            raise ValueError(f"combinational loop in netlist: {cyc[:6]}")
        return list(nx.topological_sort(g))

    def comb_depth(self):
        """Longest combinational path, in gates."""
        producer = {}
        for idx, (_, model, pins) in enumerate(self.comb):
            for o in model.outputs:
                if o in pins:
                    producer[pins[o]] = idx
        g = nx.DiGraph()
        g.add_nodes_from(range(len(self.comb)))
        for idx, (_, model, pins) in enumerate(self.comb):
            for p in model.inputs:
                src = producer.get(pins[p])
                if src is not None:
                    g.add_edge(src, idx)
        return len(list(nx.topological_generations(g)))

    # ------------------------------------------------------------------
    def _settle(self, netv, ops):
        for idx in self.order:
            _, model, pins = self.comb[idx]
            vals = {p: netv[pins[p]] for p in model.inputs}
            for p, v in model.eval(vals, ops).items():
                if p in pins:
                    netv[pins[p]] = v

    def _reset_state(self, ops):
        """Power-up state: dfstp cells reset high, everything else low."""
        return {inst["name"]: (ops.ONE if model.base.startswith("dfstp") else ops.ZERO)
                for inst, model, _ in self.seq}

    def _step(self, netv, state, ops, inputs, undriven):
        """Advance one clock. `inputs` maps input port names to this cycle's values."""
        for name, val in inputs.items():
            netv[self.port[name]] = val
        if "clk" in self.port:
            netv[self.port["clk"]] = ops.ZERO
        for n in self.undriven_nets:
            netv[n] = undriven
        for inst, _m, pins in self.seq:
            netv[pins["Q"]] = state[inst["name"]]

        self._settle(netv, ops)

        nxt = {}
        # async set/reset is applied at the capture point, which is exact whenever
        # RESET_B/SET_B are stable across the cycle - true for every whole-cycle
        # stimulus used here (the emitted Verilog is genuinely asynchronous)
        for inst, model, pins in self.seq:
            d = netv[pins["D"]]
            if model.base.startswith("dfrtp"):
                rb = netv[pins["RESET_B"]]
                d = d & rb
            elif model.base.startswith("dfstp"):
                sb = netv[pins["SET_B"]]
                d = d | ops.inv(sb)
            nxt[inst["name"]] = d

        for inst, _m, pins in self.seq:
            netv[pins["Q"]] = nxt[inst["name"]]
        self._settle(netv, ops)
        return nxt

    # ------------------------------------------------------------------
    def run_named(self, stimulus, outputs):
        """Simulate with arbitrary port names.

        stimulus: iterable of {input_port: 0/1} dicts, one per clock cycle.
        outputs:  port names to sample after each edge.
        -> list of tuples, one per cycle, in the order of `outputs`.
        """
        ops = ScalarOps
        netv = [0] * self.n_nets
        state = self._reset_state(ops)
        out = []
        for inp in stimulus:
            state = self._step(netv, state, ops,
                               {k: int(v) for k, v in inp.items()}, self.undriven_value)
            out.append(tuple(netv[self.port[o]] for o in outputs))
        self.last_state = state
        return out

    def run(self, stimulus):
        """stimulus: iterable of (rst_n, enable, I) ints. -> [(O_byte, success)] per cycle.

        Convenience wrapper for the puzzle's port set.
        """
        ops = ScalarOps
        netv = [0] * self.n_nets
        state = self._reset_state(ops)
        out = []
        for rst_n, enable, i_bit in stimulus:
            state = self._step(netv, state, ops,
                               {"rst_n": int(rst_n), "enable": int(enable), "I": int(i_bit)},
                               self.undriven_value)
            o = sum(netv[self.port[f"O[{b}]"]] << b for b in range(8))
            out.append((o, netv[self.port["success"]]))
        self.last_state = state
        return out

    def run_batch(self, bit_strings, reset_cycles=3, tail=40):
        """Simulate many 121-bit inputs at once, one numpy lane each.

        -> dict(success=bool[lanes], messages=[str], O=uint8[cycles, lanes])
        """
        bit_strings = list(bit_strings)
        lanes = len(bit_strings)
        n_bits = len(bit_strings[0])
        if any(len(b) != n_bits for b in bit_strings):
            raise ValueError("all stimuli must be the same length")
        ops = ArrayOps(lanes)
        bits = np.array([[int(c) for c in s] for s in bit_strings], dtype=bool).T  # [bit, lane]

        netv = [ops.ZERO] * self.n_nets
        state = self._reset_state(ops)
        undriven = ops.ONE if self.undriven_value else ops.ZERO

        total = reset_cycles + n_bits + tail
        o_hist = np.zeros((total, lanes), dtype=np.uint8)
        s_hist = np.zeros((total, lanes), dtype=bool)
        succ = np.zeros(lanes, dtype=bool)
        for cyc in range(total):
            if cyc < reset_cycles:
                rst_n, en, i_bit = ops.ZERO, ops.ZERO, ops.ZERO
            else:
                k = cyc - reset_cycles
                if k < n_bits:
                    rst_n, en, i_bit = ops.ONE, ops.ONE, bits[k]
                else:
                    rst_n, en, i_bit = ops.ONE, ops.ZERO, ops.ZERO
            state = self._step(netv, state, ops,
                               {"rst_n": rst_n, "enable": en, "I": i_bit}, undriven)
            byte = np.zeros(lanes, dtype=np.uint8)
            for b in range(8):
                byte |= netv[self.port[f"O[{b}]"]].astype(np.uint8) << b
            o_hist[cyc] = byte
            s_hist[cyc] = netv[self.port["success"]]
            succ |= s_hist[cyc]

        self.last_state = state
        messages = ["".join(chr(v) for v in o_hist[:, ln] if 32 <= v < 127)
                    for ln in range(lanes)]
        return {"success": succ, "messages": messages, "O": o_hist, "S": s_hist,
                "state": state}


def standard_stimulus(bits, reset_cycles=3, tail=40, idle_after_reset=0):
    """The protocol the design expects: reset, then 121 bits with enable high, then idle.

    `idle_after_reset` inserts reset-released/enable-low cycles before the first data
    bit; `example_inputs.vcd` has exactly one.  prove.py shows the two variants are
    equivalent for `success` and the message window, so the default stays back-to-back.
    """
    seq = [(0, 0, 0)] * reset_cycles
    seq += [(1, 0, 0)] * idle_after_reset
    seq += [(1, 1, int(c)) for c in bits]
    seq += [(1, 0, 0)] * tail
    return seq


def message_of(result):
    """Printable characters from a run() result."""
    return "".join(chr(o) for o, _s in result if 32 <= o < 127)
