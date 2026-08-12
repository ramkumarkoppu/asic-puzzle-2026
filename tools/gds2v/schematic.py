"""Draw a gate-level schematic of an extracted netlist.

Gates get their IEEE-style symbols (AND D-shape, OR/XOR crescents, triangle
buffers/inverters, inversion bubbles, mux trapezoids, D-flip-flop boxes with a clock
wedge); complex AOI/OAI cells are drawn as labelled boxes with pin stubs, which is how
schematic viewers render them too.  Layout is layered by combinational depth, columns
ordered by a barycentre pass to reduce crossings.

High-fanout nets (clocks, resets) and undriven nets are shown as pin labels instead of
wires - standard schematic practice, and the only thing that keeps a real netlist
readable.  Use `cone` to draw just the logic feeding one output.
"""
import collections
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon, Rectangle, PathPatch
from matplotlib.path import Path

from . import cells as cell_models
from .extract import SUPPLY_PINS

INK = "#2e4057"
WIRE = "#4f6d8f"
FEEDBACK = "#9db4c8"
LABEL = "#8a2b2b"
FILL = "white"

COL_W = 3.6
ROW_GAP = 0.55


class _Node:
    __slots__ = ("nid", "kind", "label", "sub", "ins", "outs", "w", "h", "x", "y", "col")

    def __init__(self, nid, kind, label, sub, ins, outs, w, h):
        self.nid, self.kind, self.label, self.sub = nid, kind, label, sub
        self.ins, self.outs = ins, outs          # ins: [(pin, net, inverted)], outs: [(pin, net)]
        self.w, self.h = w, h
        self.x = self.y = 0.0
        self.col = 0


def _glyph_kind(model):
    base = model.base
    if model.kind == "SEQ":
        return "DFF"
    if model.kind == "MUX":
        return "MUX"
    if model.kind == "TIE":
        return "TIE"
    if model.kind == "SIMPLE":
        fam = model.detail["fam"]
        if fam in ("and", "nand"):
            return "AND"
        if fam in ("or", "nor"):
            return "OR"
        if fam == "xor2":
            return "XOR"
        if fam == "xnor2":
            return "XOR"
        if fam == "inv":
            return "INV"
        if fam == "buf":
            return "BUF"
    return "BOX"


def _out_bubble(model):
    if model.kind == "SIMPLE":
        return model.detail["fam"] in ("nand", "nor", "xnor2", "inv")
    return False


def build_nodes(nl, cone=None, fanout_wire_limit=8):
    """-> (nodes, wires, labelled_nets). Wires: (src_node, src_pin, dst_node, dst_pin, net)."""
    netname = {n["id"]: n.get("name") or n.get("vname") for n in nl["nets"]}
    port_dir = {p["name"]: p["dir"] for p in nl["ports"]}
    port_net = {p["name"]: p["net"] for p in nl["ports"]}
    net_of_vname = {v: k for k, v in netname.items()}

    nodes, models = [], {}
    driver, sinks = {}, collections.defaultdict(list)

    for d in nl["instances"]:
        if d["cell"] not in models:
            # pins let an unrecognised cell fall back to a labelled blackbox box
            models[d["cell"]] = cell_models.parse_cell(d["cell"], pins=list(d["pins"]))
        m = models[d["cell"]]
        if m.kind == "PHYS":
            continue
        pins = {p: v for p, v in d["pins"].items() if p not in SUPPLY_PINS}
        ins = [(p, pins[p], p.endswith("_N")) for p in m.inputs if p in pins]
        outs = [(o, pins[o]) for o in m.outputs if o in pins]
        kind = _glyph_kind(m)
        npin = max(len(ins), 1)
        if kind == "DFF":
            w, h = 1.5, 1.3
        elif kind == "BOX":
            w, h = 1.6, max(0.8, 0.30 * npin + 0.3)
        elif kind == "TIE":
            w, h = 0.7, 0.5
        else:
            w, h = 1.2, max(0.9, 0.30 * npin)
        node = _Node(d["name"], kind, m.base, d["name"], ins, outs, w, h)
        nodes.append(node)
        for o, net in outs:
            driver[net] = (node, o)
        for p, net, _inv in ins:
            sinks[net].append((node, p))

    # ports as nodes
    for name, dirn in port_dir.items():
        net = net_of_vname[port_net[name]]
        if dirn == "input":
            node = _Node(f"PI:{name}", "PIN", name, "", [], [("Y", net)], 0.9, 0.4)
            nodes.append(node)
            driver.setdefault(net, (node, "Y"))
        else:
            node = _Node(f"PO:{name}", "POUT", name, "", [("A", net, False)], [], 0.9, 0.4)
            nodes.append(node)
            sinks[net].append((node, "A"))

    # optional cone restriction: keep the comb ancestors of the given output ports.
    # A flop directly driving a requested port is expanded through its D input (that
    # is the interesting logic); all other flops are kept as sources, D cone omitted.
    if cone:
        want_nets = {net_of_vname[port_net[c]] for c in cone if c in port_net}
        seed_flops = {id(driver[n][0]) for n in want_nets
                      if n in driver and driver[n][0].kind == "DFF"}
        keep, frontier = set(), set(want_nets)
        while frontier:
            net = frontier.pop()
            if net in keep:
                continue
            keep.add(net)
            if net not in driver:
                continue
            node, _ = driver[net]
            if node.kind == "PIN" or (node.kind == "DFF" and id(node) not in seed_flops):
                continue
            for _p, n2, _i in node.ins:
                if n2 not in keep:
                    frontier.add(n2)
        alive = set()
        for node in nodes:
            if any(net in keep for _o, net in node.outs) or \
               (node.kind == "POUT" and node.ins[0][1] in keep and node.label in cone):
                alive.add(id(node))
        nodes = [n for n in nodes if id(n) in alive]
        present = {id(n) for n in nodes}
        sinks = collections.defaultdict(list)
        for node in nodes:
            for p, net, _inv in node.ins:
                sinks[net].append((node, p))
        driver = {net: dv for net, dv in driver.items() if id(dv[0]) in present}

    # high-fanout and undriven nets become labels, not wires
    labelled = set()
    for net, ss in sinks.items():
        if net not in driver or len(ss) > fanout_wire_limit:
            labelled.add(net)

    wires = []
    for net, ss in sinks.items():
        if net in labelled:
            continue
        src, spin = driver[net]
        for dst, dpin in ss:
            if dst is not src:
                wires.append((src, spin, dst, dpin, net))
    return nodes, wires, labelled, netname, driver


def _levelise(nodes, wires):
    """Column per node: ports and flop-Q sources at 0, comb by depth, flops+outputs last."""
    by_id = {id(n): n for n in nodes}
    fanin = collections.defaultdict(list)
    for src, _sp, dst, _dp, _net in wires:
        fanin[id(dst)].append(id(src))

    level = {}

    def lv(nid, stack=()):
        if nid in level:
            return level[nid]
        node = by_id[nid]
        if node.kind in ("PIN", "DFF", "TIE") or nid in stack:
            level[nid] = 0
            return 0
        srcs = [s for s in fanin[nid] if by_id[s].kind not in ("DFF", "PIN", "TIE")]
        level[nid] = 1 + max((lv(s, stack + (nid,)) for s in srcs), default=0) \
            if node.kind != "PIN" else 0
        if not srcs:
            level[nid] = 1 if node.kind not in ("PIN", "DFF", "TIE") else 0
        return level[nid]

    maxlv = 0
    for n in nodes:
        if n.kind not in ("DFF", "POUT"):
            maxlv = max(maxlv, lv(id(n)))
    has_in = {id(dst) for _s, _sp, dst, _dp, _n in wires}
    for n in nodes:
        if n.kind in ("PIN", "TIE"):
            n.col = 0
        elif n.kind == "DFF":
            # a flop with no drawn fan-in (cone view) is a pure source: put it left
            n.col = 0 if id(n) not in has_in else maxlv + 1
        elif n.kind == "POUT":
            n.col = maxlv + 2
        else:
            n.col = lv(id(n))
    return maxlv + 2


def _place(nodes, wires, ncols):
    cols = collections.defaultdict(list)
    for n in nodes:
        cols[n.col].append(n)

    # initial order, then barycentre sweeps
    neigh = collections.defaultdict(list)
    for src, _sp, dst, _dp, _net in wires:
        neigh[id(dst)].append(src)
        neigh[id(src)].append(dst)
    for c in cols:
        cols[c].sort(key=lambda n: n.nid)
    for _sweep in range(4):
        for c in range(ncols + 1):
            order = {id(n): i for col in cols.values() for i, n in enumerate(col)}
            cols[c].sort(key=lambda n: (
                sum(order.get(id(m), 0) for m in neigh[id(n)]) / max(1, len(neigh[id(n)]))))
    for c, col in cols.items():
        total = sum(n.h + ROW_GAP for n in col)
        y = total / 2
        for n in col:
            n.x = c * COL_W
            n.y = y - n.h / 2
            y -= n.h + ROW_GAP


def _pin_pos(node):
    """-> ({in_pin: (x, y)}, {out_pin: (x, y)})"""
    ip, op = {}, {}
    k = len(node.ins)
    for i, (p, _net, _inv) in enumerate(node.ins):
        fy = 0 if k == 1 else (i / (k - 1) - 0.5) * node.h * 0.6
        ip[p] = (node.x - node.w / 2, node.y - fy)
    m = len(node.outs)
    for i, (p, _net) in enumerate(node.outs):
        fy = 0 if m == 1 else (i / (m - 1) - 0.5) * node.h * 0.5
        op[p] = (node.x + node.w / 2, node.y - fy)
    return ip, op


# ------------------------------------------------------------------ glyphs
def _draw_and(ax, n, bubble):
    x0, y0, w, h = n.x - n.w / 2, n.y, n.w, n.h * 0.95
    flat = x0 + w * 0.45
    verts = [(x0, y0 - h / 2), (flat, y0 - h / 2),
             (x0 + w * 1.05, y0 - h / 2), (x0 + w * 1.05, y0 + h / 2), (flat, y0 + h / 2),
             (x0, y0 + h / 2), (x0, y0 - h / 2)]
    codes = [Path.MOVETO, Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
             Path.LINETO, Path.CLOSEPOLY]
    ax.add_patch(PathPatch(Path(verts, codes), fc=FILL, ec=INK, lw=1.4))
    if bubble:
        ax.add_patch(Circle((n.x + n.w / 2 + 0.07, n.y), 0.07, fc=FILL, ec=INK, lw=1.2))


def _draw_or(ax, n, xor, bubble):
    x0, y0, w, h = n.x - n.w / 2, n.y, n.w, n.h * 0.95
    tip = (x0 + w * 1.02, y0)
    def crescent(xoff):
        verts = [(x0 + xoff, y0 - h / 2),
                 (x0 + xoff + w * 0.28, y0 - h / 2 * 0.55),
                 (x0 + xoff + w * 0.28, y0 + h / 2 * 0.55),
                 (x0 + xoff, y0 + h / 2)]
        return verts
    back = crescent(0)
    verts = [back[0],
             (x0 + w * 0.55, y0 - h / 2), (x0 + w * 0.9, y0 - h * 0.22), tip,
             (x0 + w * 0.9, y0 + h * 0.22), (x0 + w * 0.55, y0 + h / 2), back[3],
             back[2], back[1], back[0]]
    codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
             Path.CURVE4, Path.CURVE4, Path.CURVE4,
             Path.CURVE4, Path.CURVE4, Path.CURVE4]
    ax.add_patch(PathPatch(Path(verts, codes), fc=FILL, ec=INK, lw=1.4))
    if xor:
        v2 = crescent(-0.14)
        ax.add_patch(PathPatch(Path([v2[0], v2[1], v2[2], v2[3]],
                                    [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4]),
                               fc="none", ec=INK, lw=1.4))
    if bubble:
        ax.add_patch(Circle((n.x + n.w / 2 + 0.07, n.y), 0.07, fc=FILL, ec=INK, lw=1.2))


def _draw_tri(ax, n, bubble):
    ax.add_patch(Polygon([(n.x - n.w / 2, n.y - n.h / 2), (n.x - n.w / 2, n.y + n.h / 2),
                          (n.x + n.w / 2 * 0.9, n.y)], fc=FILL, ec=INK, lw=1.4))
    if bubble:
        ax.add_patch(Circle((n.x + n.w / 2 * 0.9 + 0.07, n.y), 0.07, fc=FILL, ec=INK, lw=1.2))


def _draw_mux(ax, n):
    ax.add_patch(Polygon([(n.x - n.w / 2, n.y - n.h / 2), (n.x - n.w / 2, n.y + n.h / 2),
                          (n.x + n.w / 2, n.y + n.h * 0.22), (n.x + n.w / 2, n.y - n.h * 0.22)],
                         fc=FILL, ec=INK, lw=1.4))
    ax.text(n.x, n.y, "MUX", ha="center", va="center", fontsize=5, color=INK)


def _draw_dff(ax, n):
    ax.add_patch(Rectangle((n.x - n.w / 2, n.y - n.h / 2), n.w, n.h,
                           fc=FILL, ec=INK, lw=1.4))
    ip, op = _pin_pos(n)
    for p, (px, py) in ip.items():
        short = {"RESET_B": "R", "SET_B": "S", "CLK": "", "D": "D"}.get(p, p)
        if p == "CLK":
            ax.add_patch(Polygon([(px, py - 0.09), (px, py + 0.09), (px + 0.16, py)],
                                 fc="none", ec=INK, lw=1.1))
        else:
            ax.text(px + 0.12, py, short, ha="left", va="center", fontsize=5.5, color=INK)
    for p, (px, py) in op.items():
        ax.text(px - 0.12, py, "Q", ha="right", va="center", fontsize=5.5, color=INK)
    ax.text(n.x, n.y - n.h / 2 - 0.14, n.sub, ha="center", va="top",
            fontsize=4, color="#777")


def _draw_box(ax, n):
    ax.add_patch(Rectangle((n.x - n.w / 2, n.y - n.h / 2), n.w, n.h,
                           fc=FILL, ec=INK, lw=1.4))
    ax.text(n.x, n.y + n.h / 2 - 0.02, n.label, ha="center", va="top",
            fontsize=5, color=INK)
    ip, op = _pin_pos(n)
    for p, (px, py) in ip.items():
        ax.text(px + 0.10, py, p.replace("_N", ""), ha="left", va="center",
                fontsize=4.5, color=INK)
    for p, (px, py) in op.items():
        ax.text(px - 0.10, py, p, ha="right", va="center", fontsize=4.5, color=INK)


def _draw_tie(ax, n):
    ax.add_patch(Rectangle((n.x - n.w / 2, n.y - n.h / 2), n.w, n.h,
                           fc=FILL, ec=INK, lw=1.4))
    used = ",".join("1" if p == "HI" else "0" for p, _ in n.outs)
    ax.text(n.x, n.y, used, ha="center", va="center", fontsize=6, color=INK)


def _draw_port(ax, n, is_out):
    if is_out:
        ax.annotate(n.label, (n.x + n.w / 2, n.y), ha="left", va="center",
                    fontsize=7, color=INK, fontweight="bold")
        ax.plot([n.x - n.w / 2, n.x + n.w / 2 - 0.05], [n.y, n.y], color=INK, lw=1.2)
    else:
        ax.annotate(n.label, (n.x - n.w / 2, n.y), ha="right", va="center",
                    fontsize=7, color=INK, fontweight="bold")
        ax.plot([n.x - n.w / 2 + 0.05, n.x + n.w / 2], [n.y, n.y], color=INK, lw=1.2)


_DRAW = {"AND": lambda ax, n: _draw_and(ax, n, n.label.startswith("nand")),
         "OR": lambda ax, n: _draw_or(ax, n, False, n.label.startswith("nor")),
         "XOR": lambda ax, n: _draw_or(ax, n, True, n.label.startswith("xnor")),
         "INV": lambda ax, n: _draw_tri(ax, n, True),
         "BUF": lambda ax, n: _draw_tri(ax, n, False),
         "MUX": lambda ax, n: _draw_mux(ax, n),
         "DFF": lambda ax, n: _draw_dff(ax, n),
         "BOX": lambda ax, n: _draw_box(ax, n),
         "TIE": lambda ax, n: _draw_tie(ax, n),
         "PIN": lambda ax, n: _draw_port(ax, n, False),
         "POUT": lambda ax, n: _draw_port(ax, n, True)}


def draw(nl, path, cone=None, fanout_wire_limit=8, title=None):
    """Render the netlist to `path` (svg/png by extension). Returns stats."""
    nodes, wires, labelled, netname, driver = build_nodes(nl, cone, fanout_wire_limit)
    ncols = _levelise(nodes, wires)
    _place(nodes, wires, ncols)

    xs = [n.x for n in nodes] or [0]
    ys = [n.y for n in nodes] or [0]
    x_lo, x_hi = min(xs) - 2, max(xs) + 2.5
    y_lo, y_hi = min(ys) - 2.5, max(ys) + 1.5
    fw = max(6.0, (x_hi - x_lo) * 0.42)
    fh = max(4.0, (y_hi - y_lo) * 0.42)
    scale = min(1.0, 260.0 / max(fw, fh))
    fig, ax = plt.subplots(figsize=(fw * scale, fh * scale))
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_aspect("equal")
    ax.axis("off")
    if title:
        ax.set_title(title, color=INK, fontsize=11)

    # wires first, glyphs on top
    lane_of = collections.defaultdict(lambda: len(lane_of))
    fb_lane = collections.defaultdict(lambda: len(fb_lane))
    for src, spin, dst, dpin, net in wires:
        _si, so = _pin_pos(src)
        di, _do = _pin_pos(dst)
        (x1, y1), (x2, y2) = so[spin], di[dpin]
        bub = next((inv for p, _n, inv in dst.ins if p == dpin), False)
        if bub:
            ax.add_patch(Circle((x2 - 0.07, y2), 0.06, fc=FILL, ec=INK, lw=1.0, zorder=3))
            x2 -= 0.13
        if x2 > x1:
            xm = x1 + 0.35 + (lane_of[net] % 24) * 0.075
            xm = min(xm, x2 - 0.1)
            ax.plot([x1, xm, xm, x2], [y1, y1, y2, y2], color=WIRE, lw=0.7, alpha=0.85,
                    solid_joinstyle="miter", zorder=1)
        else:  # feedback: route below everything
            rail = y_lo + 0.6 + (fb_lane[net] % 30) * 0.12
            x1s = x1 + 0.3
            x2s = x2 - 0.3
            ax.plot([x1, x1s, x1s, x2s, x2s, x2], [y1, y1, rail, rail, y2, y2],
                    color=FEEDBACK, lw=0.6, alpha=0.8, zorder=0)

    # fanout dots at sources feeding >1 sink
    fan = collections.Counter((id(src), spin) for src, spin, *_ in wires)
    for src, spin, *_rest in wires:
        if fan[(id(src), spin)] > 1:
            _si, so = _pin_pos(src)
            x1, y1 = so[spin]
            ax.add_patch(Circle((x1 + 0.3, y1), 0.035, fc=WIRE, ec="none", zorder=2))

    n_lbl = 0
    for node in nodes:
        _DRAW[node.kind](ax, node)
        ip, _op = _pin_pos(node)
        for p, net, _inv in node.ins:
            if net in labelled and node.kind != "POUT":
                px, py = ip[p]
                ax.text(px - 0.08, py, netname.get(net, str(net)), ha="right", va="center",
                        fontsize=4, color=LABEL, style="italic")
                n_lbl += 1

    fig.savefig(path, bbox_inches="tight",
                dpi=min(160, int(9000 / max(fw * scale, fh * scale, 1))))
    plt.close(fig)
    gates = sum(1 for n in nodes if n.kind not in ("PIN", "POUT"))
    return {"nodes": gates, "wires": len(wires), "labelled_pins": n_lbl,
            "columns": ncols + 1}
