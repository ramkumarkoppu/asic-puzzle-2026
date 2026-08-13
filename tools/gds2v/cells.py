"""Behavioural models for sky130_fd_sc_hd standard cells, derived from the cell NAME.

No PDK and no network access are needed: the sky130 high-density library names encode
each cell's function completely.  The rules, all confirmed against the extracted netlists
(derived pin names match the netlist's pin names for every cell type used):

  and2 / or3 / nand4 / nor2 / xor2 / xnor2 / inv / buf / clkbuf
      Obvious.  Non-inverting outputs are X, inverting outputs are Y.

  a<digits>o  /  a<digits>oi        AND-OR / AND-OR-INVERT
      Digits give the widths of the AND terms, which are OR'd together.
      Groups are lettered A, B, C, D and pins numbered within a group.
        a21o    X = (A1&A2) | B1
        a31oi   Y = !((A1&A2&A3) | B1)
        a22o    X = (A1&A2) | (B1&B2)
        a2111oi Y = !((A1&A2) | B1 | C1 | D1)

  o<digits>a  /  o<digits>ai        OR-AND / OR-AND-INVERT
        o21a    X = (A1|A2) & B1
        o31ai   Y = !((A1|A2|A3) & B1)
        o2bb2a  X = (!A1_N | !A2_N) & (B1|B2)

  'b' marks inverted inputs, named with an _N suffix.  Where the b sits differs by family:
      AND family (and*b, nand*b) inverts the LEADING inputs:   and4bb -> A_N,B_N,C,D
      OR  family (or*b,  nor*b)  inverts the TRAILING inputs:  or4bb  -> A,B,C_N,D_N
      In compound cells a 'b' binds to the digit group it follows: o2bb2a -> A1_N,A2_N,B1,B2

  mux2       X = S ? A1 : A0
  conb       HI = 1, LO = 0                      (tie cell)
  dfrtp      Q <= D @posedge CLK; RESET_B=0 -> Q=0 async
  dfstp      Q <= D @posedge CLK; SET_B=0   -> Q=1 async
  dfxtp      Q <= D @posedge CLK
  decap / tapvpwrvgnd / fill / diode            physical only, no logic

The drive-strength suffix (_1 / _2 / _4 / _8 / _16) is functionally irrelevant.
"""
import re
from functools import reduce

OUTPUT_PINS = {"X", "Y", "Q", "Q_N", "COUT", "COUT_N", "SUM", "HI", "LO"}
SUPPLY_PINS = {"VPWR", "VGND", "VPB", "VNB"}

PHYS_CELLS = ("decap", "tapvpwrvgnd", "fill", "diode")

_SIMPLE_RE = re.compile(r"(and|or|nand|nor)(\d)(b*)")
_COMPOUND_RE = re.compile(r"([ao])((?:\db*)+)(oi|o|ai|a)")
_GROUP_RE = re.compile(r"(\d)(b*)")


class ScalarOps:
    """Boolean ops on plain 0/1 ints."""
    ONE, ZERO = 1, 0

    @staticmethod
    def inv(a):
        return 1 - a


class WordOps:
    """Boolean ops on fixed-width integers, for bit-parallel simulation.

    Each bit position is an independent stimulus lane, so NOT must mask.
    """

    def __init__(self, mask):
        self.ONE, self.ZERO, self._mask = mask, 0, mask

    def inv(self, a):
        return ~a & self._mask


def _and(vals):
    return reduce(lambda a, b: a & b, vals)


def _or(vals):
    return reduce(lambda a, b: a | b, vals)


class CellModel:
    """Function of one standard-cell type.

    kind is one of SIMPLE, COMPOUND, MUX, TIE, SEQ, PHYS, BLACKBOX
    (BLACKBOX = an unrecognised cell whose pins are known but function is not).
    """

    def __init__(self, cell, kind, inputs, outputs, base, detail=None):
        self.cell = cell            # full sky130_fd_sc_hd__xxx_N name
        self.base = base            # name with prefix and drive suffix stripped
        self.kind = kind
        self.inputs = inputs        # signal inputs, in library order
        self.outputs = outputs
        self.detail = detail or {}

    # -- evaluation -------------------------------------------------------
    def eval(self, vals, ops=ScalarOps):
        """vals: {pin: value}. Returns {output_pin: value}. Sequential cells return {}."""
        get = lambda p: ops.inv(vals[p]) if p.endswith("_N") else vals[p]

        if self.kind == "SIMPLE":
            fam = self.detail["fam"]
            a = [get(p) for p in self.inputs]
            if fam == "and":
                return {"X": _and(a)}
            if fam == "nand":
                return {"Y": ops.inv(_and(a))}
            if fam == "or":
                return {"X": _or(a)}
            if fam == "nor":
                return {"Y": ops.inv(_or(a))}
            if fam == "xor2":
                return {"X": a[0] ^ a[1]}
            if fam == "xnor2":
                return {"Y": ops.inv(a[0] ^ a[1])}
            if fam == "inv":
                return {"Y": ops.inv(a[0])}
            if fam == "buf":
                return {"X": a[0]}

        if self.kind == "COMPOUND":
            groups, term = self.detail["groups"], self.detail["term"]
            k, terms = 0, []
            for width, _nb in groups:
                g = [get(self.inputs[k + i]) for i in range(width)]
                k += width
                terms.append(g)
            r = _or([_and(g) for g in terms]) if self.detail["fam"] == "a" \
                else _and([_or(g) for g in terms])
            return {"Y": ops.inv(r)} if term in ("oi", "ai") else {"X": r}

        if self.kind == "MUX":
            s = vals["S"]
            return {"X": (s & vals["A1"]) | (ops.inv(s) & vals["A0"])}

        if self.kind == "TIE":
            return {"HI": ops.ONE, "LO": ops.ZERO}

        return {}

    # -- Verilog ----------------------------------------------------------
    def verilog(self):
        """Behavioural module definition, so an extracted netlist can be simulated."""
        if self.kind == "PHYS" and not self.inputs:
            ports = list(SUPPLY_PINS)
            return (f"module {self.cell} ({', '.join(ports)});\n"
                    f"  input {', '.join(ports)};\n"
                    f"endmodule\n")

        ports = self.outputs + self.inputs + sorted(SUPPLY_PINS)
        head = f"module {self.cell} ({', '.join(ports)});\n"
        decls = ""
        if self.outputs:
            kw = "output reg" if self.kind == "SEQ" else "output"
            decls += f"  {kw} {', '.join(self.outputs)};\n"
        decls += f"  input {', '.join(self.inputs + sorted(SUPPLY_PINS))};\n"

        body = ""
        if self.kind == "SEQ":
            k = self.detail["seq"]
            if k == "dfrtp":
                body = ("  always @(posedge CLK or negedge RESET_B)\n"
                        "    if (!RESET_B) Q <= 1'b0; else Q <= D;\n")
            elif k == "dfstp":
                body = ("  always @(posedge CLK or negedge SET_B)\n"
                        "    if (!SET_B) Q <= 1'b1; else Q <= D;\n")
            else:
                body = "  always @(posedge CLK) Q <= D;\n"
        elif self.kind == "TIE":
            body = "  assign HI = 1'b1;\n  assign LO = 1'b0;\n"
        elif self.kind == "MUX":
            body = "  assign X = S ? A1 : A0;\n"
        elif self.kind in ("SIMPLE", "COMPOUND"):
            body = f"  assign {self.outputs[0]} = {self.expr()};\n"

        return head + decls + body + "endmodule\n"

    def expr(self, name=None):
        """Boolean expression as Verilog source.

        `name` maps a pin name to the identifier to print (defaults to the pin name
        itself); pass a net-name lookup to build a flattened behavioural assign.
        """
        name = name or (lambda p: p)
        t = lambda p: (f"~{name(p)}" if p.endswith("_N") else name(p))
        if self.kind == "MUX":
            return f"{name('S')} ? {name('A1')} : {name('A0')}"
        if self.kind == "SIMPLE":
            fam = self.detail["fam"]
            a = [t(p) for p in self.inputs]
            if fam in ("and", "nand"):
                e = " & ".join(a)
            elif fam in ("or", "nor"):
                e = " | ".join(a)
            elif fam in ("xor2", "xnor2"):
                e = f"{a[0]} ^ {a[1]}"
            else:
                e = a[0]
            return f"~({e})" if fam in ("nand", "nor", "xnor2", "inv") else e
        if self.kind == "COMPOUND":
            k, parts = 0, []
            for width, _nb in self.detail["groups"]:
                g = [t(self.inputs[k + i]) for i in range(width)]
                k += width
                join = " & " if self.detail["fam"] == "a" else " | "
                parts.append(f"({join.join(g)})" if width > 1 else g[0])
            outer = " | " if self.detail["fam"] == "a" else " & "
            e = outer.join(parts)
            return f"~({e})" if self.detail["term"] in ("oi", "ai") else e
        return ""

    def __repr__(self):
        return f"<{self.base} {self.kind} in={self.inputs} out={self.outputs}>"


def blackbox_model(cell, pins):
    """An opaque cell of unknown function - pins known, directions not.

    Used when the standard-cell library is not recognised, so that CONNECTIVITY still
    extracts and a structural netlist / schematic still emit.  All pins are recorded as
    inputs (there is no way to know outputs); simulation and lifting refuse to run on a
    design containing blackboxes.
    """
    sig = [p for p in pins if p not in SUPPLY_PINS]
    return CellModel(cell, "BLACKBOX", sig, [], cell, {})


# Models registered at runtime for cells the naming grammar cannot decode - e.g.
# verified LLM-assist proposals (see gds2v/llmassist.py).  Consulted before the
# grammar so every consumer (sim, emit, lift) sees them transparently.
_REGISTERED = {}


def register_model(model):
    """Make `model` the function of its cell name for this process."""
    _REGISTERED[model.cell] = model


def parse_cell(cell, pins=None):
    """cell name -> CellModel.

    Recognises the sky130 standard-cell grammar (all _hd/_hs/_ms/_ls/_hdll variants and
    the sky130_ef_sc_hd fill family), plus any model registered at runtime.  On an
    unrecognised name: if ``pins`` (the pin names seen in the GDS) is given, returns a
    BLACKBOX model; otherwise raises ValueError.
    """
    if cell in _REGISTERED:
        return _REGISTERED[cell]
    base = re.sub(r"_\d+$", "", re.sub(r"^sky130_\w+?_sc_[a-z]+__", "", cell))

    if base in PHYS_CELLS or base.startswith(PHYS_CELLS):
        ins = ["DIODE"] if base == "diode" else []
        return CellModel(cell, "PHYS", ins, [], base)

    if base.startswith("df"):
        kind = base
        ins = ["CLK", "D"]
        if base.startswith("dfrtp"):
            ins.append("RESET_B")
        elif base.startswith("dfstp"):
            ins.append("SET_B")
        elif not base.startswith("dfxtp"):
            raise ValueError(f"unsupported sequential cell {cell}")
        return CellModel(cell, "SEQ", ins, ["Q"], base, {"seq": kind})

    if base == "conb":
        return CellModel(cell, "TIE", [], ["HI", "LO"], base)

    if base == "mux2":
        return CellModel(cell, "MUX", ["A0", "A1", "S"], ["X"], base)

    m = _SIMPLE_RE.fullmatch(base)
    if m:
        fam, width, nb = m.group(1), int(m.group(2)), len(m.group(3))
        # AND family inverts leading inputs, OR family inverts trailing inputs
        inv = set(range(nb)) if fam in ("and", "nand") else {width - 1 - i for i in range(nb)}
        ins = [chr(65 + i) + ("_N" if i in inv else "") for i in range(width)]
        out = "Y" if fam in ("nand", "nor") else "X"
        return CellModel(cell, "SIMPLE", ins, [out], base, {"fam": fam})

    if base in ("xor2", "xnor2"):
        return CellModel(cell, "SIMPLE", ["A", "B"],
                         ["X" if base == "xor2" else "Y"], base, {"fam": base})
    if base in ("inv", "clkinv"):
        return CellModel(cell, "SIMPLE", ["A"], ["Y"], base, {"fam": "inv"})
    if base in ("buf", "clkbuf") or re.fullmatch(
            r"dlygate\d+sd\d+|dlymetal\d+s\d+s?|clkdlybuf\d+s\d+", base):
        # delay cells are functionally plain buffers
        return CellModel(cell, "SIMPLE", ["A"], ["X"], base, {"fam": "buf"})

    m = _COMPOUND_RE.fullmatch(base)
    if m:
        fam, term = m.group(1), m.group(3)
        groups = [(int(g.group(1)), len(g.group(2))) for g in _GROUP_RE.finditer(m.group(2))]
        ins = []
        for gi, (width, nb) in enumerate(groups):
            letter = chr(65 + gi)
            ins += [f"{letter}{i + 1}" + ("_N" if i < nb else "") for i in range(width)]
        out = "Y" if term in ("oi", "ai") else "X"
        return CellModel(cell, "COMPOUND", ins, [out], base,
                         {"fam": fam, "groups": groups, "term": term})

    if pins is not None:
        return blackbox_model(cell, pins)
    raise ValueError(f"unrecognised cell name {cell!r}")


def models_for(cell_names):
    return {c: parse_cell(c) for c in sorted(set(cell_names))}


def to_verilog_models(cell_names, header=True):
    """Behavioural Verilog for every cell type named, so a netlist can be simulated."""
    out = []
    if header:
        out.append("// Behavioural sky130_fd_sc_hd models generated by gds2v/cells.py.\n"
                   "// Derived from cell naming conventions - no PDK required.\n"
                   "// Simulation only: no timing, no drive strength.\n"
                   "`timescale 1ns/1ps\n")
    for cell, m in models_for(cell_names).items():
        out.append(m.verilog())
    return "\n".join(out)


def check_against_netlist(netlist_json):
    """Assert derived pin names equal the pins present in an extracted netlist.

    Returns (n_types, problems).
    """
    seen = {}
    for inst in netlist_json["instances"]:
        seen.setdefault(inst["cell"], set()).update(
            p for p in inst["pins"] if p not in SUPPLY_PINS)
    problems = []
    for cell, pins in sorted(seen.items()):
        m = parse_cell(cell)
        expect = set(m.inputs) | set(m.outputs)
        if expect != pins:
            problems.append((cell, sorted(expect - pins), sorted(pins - expect)))
    return len(seen), problems
