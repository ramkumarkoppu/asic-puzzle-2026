"""Emit netlists from an Extraction.

Three consumers, three formats:

  build()                  assign Verilog net/instance names and infer port
                           directions - every other emitter starts from its result
  to_json()                the canonical machine-readable netlist (consumed by
                           GateSim, Lifter, the schematic renderer and the tests)
  to_verilog()             structural Verilog: one cell instance per gate
  to_behavioral_verilog()  de-synthesised RTL: one assign per gate, one always
                           block per flop - equivalent by construction, but NOT
                           the original source (names/hierarchy are unrecoverable)
"""
import collections
import re

from . import cells as cell_models
from .extract import OUTPUT_PINS, SUPPLY_PINS, POWER_NETS


def _bus_group(names):
    """['O[0]','O[1]','clk'] -> ({'O': [0, 1]}, ['clk'])"""
    buses = collections.defaultdict(list)
    scalars = []
    for n in names:
        m = re.fullmatch(r"(\w+)\[(\d+)\]", n)
        if m:
            buses[m.group(1)].append(int(m.group(2)))
        else:
            scalars.append(n)
    return dict(buses), scalars


def build(extraction, module_name=None):
    """Assign Verilog names and port directions. Returns the netlist dict."""
    nets, insts = extraction.nets, extraction.instances

    used = {n["name"] for n in nets if n["name"]}
    for n in nets:
        if n["name"]:
            n["vname"] = n["name"]
        else:
            cand = f"n{n['id']}"
            while cand in used:
                cand += "_"
            n["vname"] = cand
            used.add(cand)

    for d in insts:
        d["iname"] = f"U{d['id']}_{d['cell'].replace('sky130_fd_sc_hd__', '')}"

    ports = []
    for n in nets:
        if not n["name"] or n["name"] in POWER_NETS:
            continue
        driven = any(pin in OUTPUT_PINS for (_, pin) in n["terminals"])
        ports.append({"name": n["name"], "dir": "output" if driven else "input",
                      "net": n["vname"]})
    return {"module": module_name or extraction.top_name,
            "ports": ports, "nets": nets, "instances": insts}


def to_json(nl):
    """The canonical netlist-JSON shape used by GateSim / Lifter / schematic / tests.

    Keep this the single point of truth for the serialised format: everything
    downstream loads exactly this structure (03_netlist.json on disk).
    """
    return {"module": nl["module"], "ports": nl["ports"],
            "instances": [{"name": d["iname"], "cell": d["cell"], "pins": d["pins"]}
                          for d in nl["instances"]],
            "nets": [{"id": n["id"], "name": n["vname"],
                      "terminals": [[i, p] for (i, p) in n["terminals"]]}
                     for n in nl["nets"]]}


def to_verilog(nl, include_power=False):
    mod, ports, nets, insts = nl["module"], nl["ports"], nl["nets"], nl["instances"]
    dirs = {p["name"]: p["dir"] for p in ports}
    buses, scalars = _bus_group([p["name"] for p in ports])

    L = ["// Structural netlist recovered from GDS by gds2v.",
         "//",
         "// Cell types and pin names are ground truth read out of the layout.",
         "// Instance and net names are synthesised - the originals are not in the GDS.",
         ""]

    hdr, decls = [], []
    for b, idxs in sorted(buses.items()):
        hdr.append(b)
        decls.append(f"  {dirs[f'{b}[{idxs[0]}]']} [{max(idxs)}:{min(idxs)}] {b};")
    for s in sorted(scalars):
        hdr.append(s)
        decls.append(f"  {dirs[s]} {s};")

    L.append(f"module {mod} (" + ", ".join(hdr) + ");")
    L.extend(decls)
    L.append("")

    portnets = {p["net"] for p in ports}
    wires = sorted(n["vname"] for n in nets
                   if n["vname"] not in portnets and n["vname"] not in POWER_NETS)
    for w in wires:
        L.append(f"  wire {w};")
    if include_power:
        L.append("  supply1 VPWR;")
        L.append("  supply0 VGND;")
    L.append("")

    netname = {n["id"]: n["vname"] for n in nets}
    for d in insts:
        conns = [f".{pin}({netname[nid]})"
                 for pin, nid in sorted(d["pins"].items())
                 if include_power or pin not in SUPPLY_PINS]
        L.append(f"  {d['cell']} {d['iname']} (" + ", ".join(conns) + ");")

    L += ["", "endmodule", ""]
    return "\n".join(L)


def to_behavioral_verilog(nl):
    """De-synthesised RTL: one `assign` per gate, one `always` block per flop.

    This is the netlist re-expressed in the style of hand-written Verilog - the same
    logic BEFORE it was mapped to standard cells.  It is equivalent by construction
    (built from the same cell models the VCD-validated simulator executes), but it is
    not the original source: real signal names, module hierarchy and coding idioms are
    destroyed by synthesis and cannot be recovered.
    """
    mod, ports, nets, insts = nl["module"], nl["ports"], nl["nets"], nl["instances"]
    netname = {n["id"]: n["vname"] for n in nets}
    dirs = {p["name"]: p["dir"] for p in ports}
    buses, scalars = _bus_group([p["name"] for p in ports])

    # nets driven by a flop are regs; everything else driven is a wire
    reg_nets, models = set(), {}
    for d in insts:
        m = models.setdefault(d["cell"], cell_models.parse_cell(d["cell"]))
        if m.kind == "SEQ" and "Q" in d["pins"]:
            reg_nets.add(d["pins"]["Q"])

    L = ["// De-synthesised behavioural RTL recovered from the layout by gds2v.",
         "//",
         "// Each assign is one standard cell of the netlist; each always block is one",
         "// flip-flop.  Equivalent to the extracted netlist by construction, but NOT",
         "// the original source - synthesis destroys names and hierarchy irrecoverably.",
         "`timescale 1ns / 1ps", ""]

    hdr, decls = [], []
    for b, idxs in sorted(buses.items()):
        d = dirs[f"{b}[{idxs[0]}]"]
        hdr.append(b)
        decls.append(f"  {d} [{max(idxs)}:{min(idxs)}] {b};")
    portreg = {p["net"] for p in ports} & {netname[n] for n in reg_nets}
    for s in sorted(scalars):
        hdr.append(s)
        kw = f"{dirs[s]} reg" if dirs[s] == "output" and s in portreg else dirs[s]
        decls.append(f"  {kw} {s};")

    L.append(f"module {mod} (" + ", ".join(hdr) + ");")
    L.extend(decls)
    L.append("")

    portnets = {p["net"] for p in ports}
    for n in sorted(nets, key=lambda x: x["vname"]):
        if n["vname"] in portnets or n["vname"] in POWER_NETS:
            continue
        kw = "reg " if n["id"] in reg_nets else "wire"
        L.append(f"  {kw} {n['vname']};")
    L.append("")

    assigns, flops = [], []
    for d in insts:
        m = models[d["cell"]]
        pins = d["pins"]
        pname = lambda p: netname[pins[p]]
        tag = f"// {d['iname']}"
        if m.kind in ("SIMPLE", "COMPOUND", "MUX", "PROPOSED"):
            out = next(o for o in m.outputs if o in pins)
            assigns.append(f"  assign {pname(out)} = {m.expr(pname)};  {tag}")
        elif m.kind == "TIE":
            for o, v in (("HI", "1'b1"), ("LO", "1'b0")):
                if o in pins:
                    assigns.append(f"  assign {pname(o)} = {v};  {tag}")
        elif m.kind == "SEQ":
            q, dd, clk = pname("Q"), pname("D"), pname("CLK")
            if "RESET_B" in pins:
                flops.append(f"  {tag}\n  always @(posedge {clk} or negedge {pname('RESET_B')})\n"
                             f"    if (!{pname('RESET_B')}) {q} <= 1'b0;\n"
                             f"    else {q} <= {dd};")
            elif "SET_B" in pins:
                flops.append(f"  {tag}\n  always @(posedge {clk} or negedge {pname('SET_B')})\n"
                             f"    if (!{pname('SET_B')}) {q} <= 1'b1;\n"
                             f"    else {q} <= {dd};")
            else:
                flops.append(f"  {tag}\n  always @(posedge {clk}) {q} <= {dd};")
        # PHYS cells carry no logic

    L += sorted(assigns) + [""] + flops
    L += ["", "endmodule", ""]
    return "\n".join(L)
