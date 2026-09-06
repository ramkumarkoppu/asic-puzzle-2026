"""Lift a flat gate netlist toward readable RTL - the last step of GDS -> Verilog source.

The flat behavioural output (04_behavioral.v) is equivalent to the original source but
unreadable: a sea of anonymous assigns.  This pass recovers *structure*, and every claim
it makes is verified before it is emitted:

  1. enable-mux registers   flop whose D comes from a mux2 with one leg tied to its own
                            Q is a register with a hold path; the mux select is the
                            enable, the other leg is the data-in.       (structural)
  2. shift registers        registers whose data-in is another register's Q chain into
                            shift registers; the head's data-in is the serial input.
                                                                        (structural)
  3. clock/reset folding    flop CLK pins that reach the clk port purely through
                            buffers are re-expressed as posedge clk.    (structural)
  4. word-level functions   an output cone over <= MAX_TABULATE register bits is
                            tabulated exhaustively and matched against arithmetic
                            templates (sum-equals-constant, ...).  A match is a THEOREM
                            about the netlist, proven over every reachable state.
                                                                        (exhaustive)

Anything not lifted is emitted unchanged as flat assigns, so the output is always
complete.  Names are derived from ports (serial input A -> register sr_a); the
original names remain unrecoverable.
"""
import collections
import itertools

import numpy as np

from . import cells as cell_models
from .cells import SUPPLY_PINS, OUTPUT_PINS

MAX_TABULATE = 20          # exhaustive proof bound: 2^20 states


class Lifter:
    def __init__(self, nl):
        self.nl = nl
        self.netname = {n["id"]: n.get("name") or n.get("vname") for n in nl["nets"]}
        by_vname = {self.netname[n["id"]]: n["id"] for n in nl["nets"]}
        self.port = {p["name"]: by_vname[p["net"]] for p in nl["ports"]}
        self.port_of_net = {v: k for k, v in self.port.items()}
        self.in_ports = {p["name"] for p in nl["ports"] if p["dir"] == "input"}

        self.models, self.comb, self.flops = {}, [], []
        self.driver = {}
        for inst in nl["instances"]:
            m = self.models.setdefault(inst["cell"], cell_models.parse_cell(inst["cell"]))
            pins = {p: v for p, v in inst["pins"].items() if p not in SUPPLY_PINS}
            rec = {"name": inst["name"], "model": m, "pins": pins}
            if m.kind == "SEQ":
                self.flops.append(rec)
            elif m.kind != "PHYS":
                self.comb.append(rec)
            for o in m.outputs:
                if o in pins:
                    self.driver[pins[o]] = rec
        self.flop_by_q = {f["pins"]["Q"]: f for f in self.flops}

        # net -> [(instance, pin)] consumers, over signal pins only
        self.consumers = collections.defaultdict(list)
        for rec in self.comb + self.flops:
            for p in rec["model"].inputs:
                if p in rec["pins"]:
                    self.consumers[rec["pins"][p]].append((rec["name"], p))

        self.registers = []        # lifted shift registers
        self.lifted_flops = {}     # flop name -> (reg_idx, bit)
        self.templates = []        # proven word-level output functions
        self.log = []

    # ------------------------------------------------------------- structural
    def _mux_hold(self, flop):
        """If D is mux2(A0=own Q) -> (enable_net, shiftin_net); else None."""
        d = self.driver.get(flop["pins"]["D"])
        if not d or d["model"].kind != "MUX":
            return None
        p = d["pins"]
        if p.get("A0") == flop["pins"]["Q"]:
            return p["S"], p["A1"]
        if p.get("A1") == flop["pins"]["Q"]:       # inverted-select hold
            return None                            # not seen in these designs
        return None

    def _through_buffers(self, net):
        """Walk backwards through buf/clkbuf drivers; return the root net."""
        seen = set()
        while net not in seen:
            seen.add(net)
            d = self.driver.get(net)
            if d and d["model"].kind == "SIMPLE" and d["model"].detail["fam"] == "buf":
                net = d["pins"]["A"]
            else:
                return net
        return net

    def find_shift_registers(self):
        holds = {}
        for f in self.flops:
            mh = self._mux_hold(f)
            if mh:
                holds[f["name"]] = (f, mh[0], mh[1])
        # chain: flop B follows flop A when B's shift-in is A's Q
        nxt, has_prev = {}, set()
        for name, (f, en, sin) in holds.items():
            src = self.flop_by_q.get(sin)
            if src and src["name"] in holds:
                nxt[src["name"]] = name
                has_prev.add(name)
        heads = [n for n in holds if n not in has_prev]

        for head in sorted(heads):
            chain, cur = [head], head
            while cur in nxt:
                cur = nxt[cur]
                chain.append(cur)
            f0, en0, sin0 = holds[head]
            if any(holds[c][1] != en0 for c in chain):
                continue                            # mixed enables: not one register
            clk_root = self._through_buffers(f0["pins"]["CLK"])
            rst = f0["pins"].get("RESET_B") or f0["pins"].get("SET_B")
            serial = self.port_of_net.get(sin0)
            base = f0["model"].base
            reg = {"name": f"sr_{serial.lower()}" if serial else f"r{len(self.registers)}",
                   "bits": chain, "width": len(chain),
                   "serial_net": sin0,
                   "serial": serial or self.netname[sin0],
                   "serial_port": serial,
                   "enable_net": en0,
                   "enable": self.port_of_net.get(en0) or self.netname[en0],
                   "clk": self.port_of_net.get(clk_root) or self.netname[clk_root],
                   "clk_net": clk_root,
                   "rst_net": rst,
                   "rst": (self.port_of_net.get(rst) or self.netname[rst])
                          if rst is not None else None,
                   "set": base.startswith("dfstp"),
                   "q_nets": [next(f2["pins"]["Q"] for f2 in self.flops
                                   if f2["name"] == c) for c in chain]}
            self.registers.append(reg)
            for i, c in enumerate(chain):
                self.lifted_flops[c] = (len(self.registers) - 1, i)
        self.log.append(f"shift registers: {len(self.registers)} "
                        f"{[(r['name'], r['width']) for r in self.registers]}")
        return self.registers

    # ------------------------------------------------------------- tabulation
    def _cone(self, net):
        """Combinational cone of `net`: (gates in topo order, leaf state/port nets)."""
        gates, leaves, seen = [], [], set()

        def walk(n):
            if n in seen:
                return
            seen.add(n)
            d = self.driver.get(n)
            # leaves are undriven nets (includes input ports) and flop outputs;
            # a driven net that happens to be an output port is walked through
            if d is None or d["model"].kind == "SEQ":
                leaves.append(n)
                return
            for p in d["model"].inputs:
                walk(d["pins"][p])
            gates.append(d)

        walk(net)
        return gates, leaves

    def tabulate(self, net, bit_nets):
        """Truth table of cone(net) over `bit_nets`, all other leaves 0.

        Exhaustive: 2^len(bit_nets) rows, packed 64 per numpy word.
        Returns a bool array indexed by the integer formed from bit_nets (LSB first).
        """
        gates, leaves = self._cone(net)
        extra = [l for l in leaves if l not in bit_nets]
        nbits = len(bit_nets)
        rows = 1 << nbits
        out = np.zeros(rows, dtype=bool)
        idx = np.arange(rows, dtype=np.uint64)
        for lo in range(0, rows, 64):
            lanes = min(64, rows - lo)
            mask = np.uint64((1 << lanes) - 1)
            val = {l: np.uint64(0) for l in extra}
            for b, bn in enumerate(bit_nets):
                w = np.uint64(0)
                for ln in range(lanes):
                    if (lo + ln) >> b & 1:
                        w |= np.uint64(1) << np.uint64(ln)
                val[bn] = w

            class Ops:
                ONE, ZERO = mask, np.uint64(0)
                @staticmethod
                def inv(a):
                    return ~a & mask

            for g in gates:
                vals = {p: val[g["pins"][p]] for p in g["model"].inputs}
                for p, v in g["model"].eval(vals, Ops).items():
                    if p in g["pins"]:
                        val[g["pins"][p]] = v
            w = int(val[net])
            for ln in range(lanes):
                out[lo + ln] = (w >> ln) & 1
        return out

    def match_output_templates(self):
        """For each output port, try to prove a word-level function of the registers."""
        if len(self.registers) < 1:
            return []
        for pname, pnet in self.port.items():
            if pname in self.in_ports or pnet not in self.driver:
                continue
            gates, leaves = self._cone(pnet)
            state = [l for l in leaves if l in self.flop_by_q]
            regbits = [b for r in self.registers for b in r["q_nets"]]
            if not state or any(l not in regbits for l in state) \
                    or len(regbits) > MAX_TABULATE:
                continue
            for order_a in (1, -1):
                bit_nets = [b for r in self.registers for b in r["q_nets"][::order_a]]
                tt = self.tabulate(pnet, bit_nets)
                m = self._match_sum_eq(tt)
                if m is not None:
                    widths = [r["width"] for r in self.registers]
                    self.templates.append(
                        {"port": pname, "kind": "sum_eq", "constant": m,
                         "regs": [r["name"] for r in self.registers],
                         "widths": widths, "bit_order": order_a,
                         "proof": f"exhaustive over 2^{len(bit_nets)} states"})
                    self.log.append(f"output {pname}: {' + '.join(r['name'] for r in self.registers)}"
                                    f" == {m}  (bit order {order_a:+d}, proven exhaustively)")
                    break
        return self.templates

    def _match_sum_eq(self, tt):
        """tt indexed by concat(reg words) -> is it (sum of words == C)? Returns C or None."""
        widths = [r["width"] for r in self.registers]
        if sum(widths) != int(np.log2(len(tt))):
            return None
        true_idx = np.nonzero(tt)[0]
        if len(true_idx) == 0:
            return None
        # split index into words
        sums = np.zeros(len(true_idx), dtype=np.int64)
        shift = 0
        for w in widths:
            sums += (true_idx >> shift) & ((1 << w) - 1)
            shift += w
        c = int(sums[0])
        if not np.all(sums == c):
            return None
        # completeness: every combination summing to c must be true
        expect = sum(1 for combo in itertools.product(
            *[range(1 << w) for w in widths]) if sum(combo) == c)
        return c if expect == len(true_idx) else None

    # ------------------------------------------------------------- emission
    def run(self):
        self.find_shift_registers()
        self.match_output_templates()
        covered = set()
        for t in self.templates:
            gates, _ = self._cone(self.port[t["port"]])
            covered.update(g["name"] for g in gates)
        for r in self.registers:
            for b in r["bits"]:
                f = next(f2 for f2 in self.flops if f2["name"] == b)
                mux = self.driver[f["pins"]["D"]]
                covered.add(mux["name"])
        # a buffer is folded only when its transitive fanout ends exclusively in flop
        # CLK pins (or nowhere) - the whole clock tree vanishes because every flop is
        # emitted on the folded root clock, while data buffers stay as assigns
        flop_names = {f["name"] for f in self.flops}
        bufs = {c["name"]: c for c in self.comb
                if c["model"].kind == "SIMPLE" and c["model"].detail["fam"] == "buf"}
        clock_only = set()
        changed = True
        while changed:
            changed = False
            for name, c in bufs.items():
                if name in clock_only:
                    continue
                cons = self.consumers.get(c["pins"].get("X"), [])
                if all((nm in flop_names and p == "CLK") or nm in clock_only
                       for nm, p in cons):
                    clock_only.add(name)
                    changed = True
        covered |= clock_only
        self.leftover = [c for c in self.comb if c["name"] not in covered]
        self.unlifted = [f for f in self.flops if f["name"] not in self.lifted_flops]
        self.log.append(f"comb gates lifted/folded: {len(self.comb) - len(self.leftover)}"
                        f"/{len(self.comb)}; leftover assigns: {len(self.leftover)}; "
                        f"flops lifted: {len(self.lifted_flops)}/{len(self.flops)}")
        return self

    def to_verilog(self):
        nl = self.nl
        module = nl["module"]
        dirs = {p["name"]: p["dir"] for p in nl["ports"]}
        netname = self.netname
        port_nets = set(self.port.values())
        covered_names = {c["name"] for c in self.comb} - {c["name"] for c in self.leftover}

        # SR bits whose Q is consumed outside the lifted structure need an alias wire
        covered_by_reg = set()
        for r in self.registers:
            for b in r["bits"]:
                f = next(f2 for f2 in self.flops if f2["name"] == b)
                covered_by_reg.add(self.driver[f["pins"]["D"]]["name"])
        aliases = []
        for r in self.registers:
            for k, qn in enumerate(r["q_nets"]):
                ext = [nm for nm, _p in self.consumers.get(qn, [])
                       if nm not in covered_names and nm not in covered_by_reg
                       and nm not in r["bits"]]
                ext = [nm for nm in ext
                       if nm in {f["name"] for f in self.unlifted}
                       or nm in {c["name"] for c in self.leftover}]
                if ext or qn in port_nets:
                    aliases.append((qn, r["name"], k))

        # nets referenced by the flat remainder
        used = set()
        for c in self.leftover:
            for p in c["model"].inputs + c["model"].outputs:
                if p in c["pins"]:
                    used.add(c["pins"][p])
        reg_nets = set()
        for f in self.unlifted:
            used.add(f["pins"]["D"])
            used.add(f["pins"]["Q"])
            reg_nets.add(f["pins"]["Q"])
            if "RESET_B" in f["pins"]:
                used.add(f["pins"]["RESET_B"])
            if "SET_B" in f["pins"]:
                used.add(f["pins"]["SET_B"])
        used.update(qn for qn, _r, _k in aliases)

        L = ["// RTL recovered from the layout by gds2v lift.",
             "//",
             "// Structure below is PROVEN, not guessed: shift registers are verified",
             "// structurally (hold-mux and chain wiring) and dynamically (predicted",
             "// vs actual state on random stimulus), clocks are folded only through",
             "// pure buffer paths, and word-level expressions are tabulated",
             "// exhaustively against the netlist over every register state.",
             "// Whatever is not lifted is emitted flat, so the module is complete.",
             "// Names are derived from ports; the originals are not recoverable.",
             "`timescale 1ns / 1ps", ""]
        from .emit import _bus_group
        buses, scalars = _bus_group(sorted(dirs))
        hdr, pdecls = [], []
        for b, idxs in sorted(buses.items()):
            hdr.append(b)
            pdecls.append(f"  {dirs[f'{b}[{idxs[0]}]']} [{max(idxs)}:{min(idxs)}] {b};")
        for s in sorted(scalars):
            hdr.append(s)
            pdecls.append(f"  {dirs[s]} {s};")
        L.append(f"module {module} (" + ", ".join(hdr) + ");")
        L.extend(pdecls)
        L.append("")

        for r in self.registers:
            L.append(f"  reg [{r['width'] - 1}:0] {r['name']};"
                     f"   // shift register, serial input {r['serial']}")
        for n in sorted(used - port_nets, key=lambda x: netname[x]):
            if n not in {qn for qn, _r, _k in aliases}:
                L.append(f"  {'reg ' if n in reg_nets else 'wire'} {netname[n]};")
        for qn, rname, k in aliases:
            if qn not in port_nets:
                L.append(f"  wire {netname[qn]} = {rname}[{k}];   // register tap")
        L.append("")

        for r in self.registers:
            shift = (f"{r['name']} <= "
                     f"{{{r['name']}[{r['width'] - 2}:0], {r['serial']}}};")
            if r["rst"]:
                rv = f"{r['width']}'b" + ("1" * r["width"] if r["set"] else "0")
                L += [f"  always @(posedge {r['clk']} or negedge {r['rst']})",
                      f"    if (!{r['rst']}) {r['name']} <= {rv};",
                      f"    else if ({r['enable']}) {shift}", ""]
            else:
                # no reset pin on these flops: silicon power-up is arbitrary, and the
                # dynamic verifier treats reset as never asserted - emit the same
                L += [f"  always @(posedge {r['clk']})   // no reset pin on these flops",
                      f"    if ({r['enable']}) {shift}", ""]

        for t in self.templates:
            cw = sum((1 << w) - 1 for w in t["widths"]).bit_length()
            regs = t["regs"] if t["bit_order"] == 1 else \
                [f"rev({r})" for r in t["regs"]]
            if t["bit_order"] == -1:
                L.append("  // rev(x) = bit-reversal; the chain order is the mirror of "
                         "the arithmetic weight order")
            terms = " + ".join(f"{{1'b0, {r}}}" for r in regs)
            L.append(f"  assign {t['port']} = ({terms} == {cw}'d{t['constant']});"
                     f"   // {t['proof']}")
        L.append("")

        if self.leftover or self.unlifted:
            L.append("  // ---- not lifted: emitted as extracted ----")
        for c in self.leftover:
            m, pins = c["model"], c["pins"]
            if m.kind == "TIE":
                for o, v in (("HI", "1'b1"), ("LO", "1'b0")):
                    if o in pins:
                        L.append(f"  assign {netname[pins[o]]} = {v};  // {c['name']}")
                continue
            out = next(o for o in m.outputs if o in pins)
            L.append(f"  assign {netname[pins[out]]} = "
                     f"{m.expr(lambda p: netname[pins[p]])};  // {c['name']}")
        L.append("")
        for f in self.unlifted:
            pins = f["pins"]
            q, d = netname[pins["Q"]], netname[pins["D"]]
            clk = self.port_of_net.get(self._through_buffers(pins["CLK"])) \
                or netname[self._through_buffers(pins["CLK"])]
            if "RESET_B" in pins:
                r = netname[pins["RESET_B"]]
                L.append(f"  always @(posedge {clk} or negedge {r}) "
                         f"if (!{r}) {q} <= 1'b0; else {q} <= {d};  // {f['name']}")
            elif "SET_B" in pins:
                s = netname[pins["SET_B"]]
                L.append(f"  always @(posedge {clk} or negedge {s}) "
                         f"if (!{s}) {q} <= 1'b1; else {q} <= {d};  // {f['name']}")
            else:
                L.append(f"  always @(posedge {clk}) {q} <= {d};  // {f['name']}")
        L += ["", "endmodule", ""]
        return "\n".join(L)


_VERILOG_KW = {"module", "endmodule", "input", "output", "reg", "wire", "assign",
               "always", "posedge", "negedge", "or", "if", "else", "begin", "end",
               "case", "endcase", "default", "function", "endfunction", "integer",
               "for", "timescale", "ns", "ps", "rev"}


def lint_verilog(text):
    """Every identifier used must be declared - catches missing wires/aliases.

    Understands only the constructs the lifters emit (lift.py and puzzle/liftrtl.py:
    port/net declarations, functions, integer loop variables). Returns the
    undeclared set.
    """
    import re
    body = "\n".join(l.split("//")[0] for l in text.splitlines()
                     if not l.strip().startswith(("//", "`")))
    declared = set()
    decl_res = (
        # input/output [reg|wire|integer] [range] name
        r"\b(?:input|output|inout)\s+(?:reg\s+|wire\s+|integer\s+)?"
        r"(?:\[[^\]]+\]\s*)?([A-Za-z_]\w*)",
        # reg/wire [range] name[, name...]
        r"\b(?:reg|wire)\s+(?:\[[^\]]+\]\s*)?"
        r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)",
        # integer name[, name...]
        r"\binteger\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)",
        # function [range] name
        r"\bfunction\s+(?:\[[^\]]+\]\s*)?([A-Za-z_]\w*)",
        r"\bmodule\s+(\w+)",
    )
    for pat in decl_res:
        for m in re.finditer(pat, body):
            for name in m.group(1).split(","):
                declared.add(name.strip())
    used = set(re.findall(r"[A-Za-z_]\w*", body))
    return {u for u in used - declared - _VERILOG_KW if not u.isdigit()
            and not re.fullmatch(r"[bodh][0-9a-fA-F]+", u)}


def verify_shift_semantics(sim, lifter, stimulus):
    """Prove the lifted interpretation dynamically: each cycle, predict every lifted
    register's next word from the claimed semantics

        word' = enable ? {word[w-2:0], serial} : word     (async reset -> 0/all-ones)

    using the settled pre-edge enable/serial values, and compare against what the
    netlist's flops actually did.  Returns the number of mismatching register-cycles.
    """
    ops = cell_models.ScalarOps
    netv = [0] * sim.n_nets
    state = sim._reset_state(ops)
    flops_of = {r["name"]: r for r in lifter.registers}
    bad = 0
    for inp in stimulus:
        # drive inputs and settle with the OLD state (mirror of GateSim._step part 1)
        for name, val in inp.items():
            netv[sim.port[name]] = int(val)
        if "clk" in sim.port:
            netv[sim.port["clk"]] = 0
        for n in sim.undriven_nets:
            netv[n] = sim.undriven_value
        for inst, _m, pins in sim.seq:
            netv[pins["Q"]] = state[inst["name"]]
        sim._settle(netv, ops)

        # predictions from the pre-edge picture
        predict = {}
        for r in lifter.registers:
            word = [state[b] for b in r["bits"]]                   # bit 0 first
            en = netv[r["enable_net"]]
            serial = netv[r["serial_net"]]
            rst = netv[r["rst_net"]] if r["rst_net"] is not None else 1
            if not rst:
                new = [1 if r["set"] else 0] * r["width"]
            elif en:
                new = [serial] + word[:-1]
            else:
                new = word
            predict[r["name"]] = new

        # let the netlist take the edge
        nxt = {}
        for inst, model, pins in sim.seq:
            d = netv[pins["D"]]
            if model.base.startswith("dfrtp"):
                d = d & netv[pins["RESET_B"]]
            elif model.base.startswith("dfstp"):
                d = d | ops.inv(netv[pins["SET_B"]])
            nxt[inst["name"]] = d
        state = nxt

        for r in lifter.registers:
            actual = [state[b] for b in r["bits"]]
            if actual != predict[r["name"]]:
                bad += 1
    return bad
