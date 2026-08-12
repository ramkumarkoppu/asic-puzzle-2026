"""Render the layout and the recovered structure.

  python -m puzzle.visualize [--outdir out/figures]

This is the useful form of "look at the layout".  The placement genuinely encodes the
architecture - dataflow runs left to right across four vertical bands - but the figures
are drawn from the *exact* extracted netlist, not guessed from pixels.  Placement is a
lossy projection of connectivity; the netlist is the un-projected original.

Figures written:
  placement.png    every cell, coloured by the functional block it belongs to
  regions.png      the recovered 11x11 region map with the solution's stars
  logo.png         the decorative met2 spiral, as its raw bitmap
  communities.png  logic cells coloured by netlist community, to show the projection
"""
import argparse
import json
import os
import sys
from collections import Counter

import gdstk
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.patches import Rectangle

from gds2v import paths
from puzzle.analyze import Analysis, GRID

ROW_HEIGHT = 2.72
FILL_CELLS = ("tapvpwrvgnd", "decap", "diode", "fill")

BLOCK_COLOURS = {
    "bit/row counter": "#d1495b",
    "shift register": "#edae49",
    "column counter": "#66a182",
    "region counter": "#2e4057",
    "region ROM": "#8d6a9f",
    "popcount": "#00798c",
    "output generator": "#e07a5f",
    "other logic": "#c9c9c9",
    "fill / tap": "#f2f2f2",
}


def cell_widths(gds_path):
    lib = gdstk.read_gds(gds_path)
    out = {}
    for c in lib.cells:
        if c.name.startswith("sky130"):
            bb = c.bounding_box()
            out[c.name] = bb[1][0] - bb[0][0]
    return out


def cell_box(d, widths):
    """Abutment box in top coordinates, accounting for orientation."""
    w = widths.get(d["cell"], 1.0)
    x0, x1 = (d["x"] - w, d["x"]) if d["orient"] in ("S", "FN") else (d["x"], d["x"] + w)
    y0, y1 = (d["y"] - ROW_HEIGHT, d["y"]) if d["orient"] in ("S", "FS") \
        else (d["y"], d["y"] + ROW_HEIGHT)
    return x0, y0, x1 - x0, y1 - y0


def classify(an, rec):
    """instance name -> functional block label, from the netlist not from geometry."""
    g = an.flop_graph()
    sccs = sorted(nx.strongly_connected_components(g), key=len, reverse=True)
    label = {}
    for s in sccs:
        if len(s) == 9:
            for f in s:
                label[f] = "bit/row counter"
        elif len(s) in (8, 4):
            for f in s:
                label[f] = "output generator"
    for c, pair in rec["columns"].items():
        for f in pair:
            label[f] = "column counter"
    for ch, pair in rec["regions"].items():
        for f in pair:
            label[f] = "region counter"
    for f in an.shift_chain("I"):
        label.setdefault(f, "shift register")
    for f in an.flop_pins:
        label.setdefault(f, "popcount")

    # combinational cells inherit the block of the flop D-cone they feed
    for name, pins in an.flop_pins.items():
        blk = label[name]
        anc = nx.ancestors(an.comb, pins["D"]) if pins["D"] in an.comb else set()
        for _, _, d in an.comb.in_edges(anc, data=True):
            label.setdefault(d["inst"], blk)
    # the region decoder: gates feeding region counters that depend only on counters
    for ch, pair in rec["regions"].items():
        pins = an.flop_pins[pair[0]]
        anc = nx.ancestors(an.comb, pins["D"]) if pins["D"] in an.comb else set()
        for _, _, d in an.comb.in_edges(anc, data=True):
            label[d["inst"]] = "region ROM"
    for out in [p["name"] for p in an.nl["ports"] if p["dir"] == "output"]:
        net = an.port[out]
        anc = nx.ancestors(an.comb, net) if net in an.comb else set()
        for _, _, d in an.comb.in_edges(anc | {net}, data=True):
            label.setdefault(d["inst"], "output generator")
    return label


def fig_placement(cells_json, gds, an, rec, path):
    cells = json.load(open(cells_json))
    widths = cell_widths(gds)
    label = classify(an, rec)
    fig, ax = plt.subplots(figsize=(7.2, 10.4))
    counts = Counter()
    for d in cells:
        base = d["cell"].split("__")[1]
        if any(f in base for f in FILL_CELLS):
            blk = "fill / tap"
        else:
            blk = label.get(f"U{d['id']}_{base}", "other logic")
        counts[blk] += 1
        x, y, w, h = cell_box(d, widths)
        ax.add_patch(Rectangle((x, y), w, h, facecolor=BLOCK_COLOURS[blk],
                               edgecolor="none", linewidth=0))
    ax.add_patch(Rectangle((0, 0), 200, 300, fill=False, edgecolor="#444", linewidth=1.2))
    ax.set_xlim(-6, 206); ax.set_ylim(-6, 306); ax.set_aspect("equal")
    ax.set_xlabel("x (um)"); ax.set_ylabel("y (um)")
    ax.set_title("puzzle.gds placement, coloured by recovered function\n"
                 "dataflow runs left to right", fontsize=10)
    handles = [Rectangle((0, 0), 1, 1, facecolor=BLOCK_COLOURS[k])
               for k in BLOCK_COLOURS if counts[k]]
    ax.legend(handles, [f"{k} ({counts[k]})" for k in BLOCK_COLOURS if counts[k]],
              loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return counts


def fig_regions(rmap, bits, path):
    letters = sorted({c for row in rmap for c in row})
    cmap = plt.get_cmap("tab20")
    colour = {ch: cmap(i / max(1, len(letters) - 1) * 0.95) for i, ch in enumerate(letters)}
    fig, ax = plt.subplots(figsize=(6.2, 6.2))
    for r in range(GRID):
        for c in range(GRID):
            ax.add_patch(Rectangle((c, GRID - 1 - r), 1, 1,
                                   facecolor=colour[rmap[r][c]], edgecolor="white", lw=1.5))
            if bits[r * GRID + c] == "1":
                ax.plot(c + 0.5, GRID - 1 - r + 0.5, marker="*", markersize=22,
                        color="black", markeredgecolor="white", markeredgewidth=0.8)
    ax.set_xlim(0, GRID); ax.set_ylim(0, GRID); ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("recovered region map with the unique solution\n"
                 "2 stars per row, column and region; none touching", fontsize=10)
    fig.tight_layout(); fig.savefig(path, dpi=170); plt.close(fig)


def fig_logo(gds, path, pitch=0.3, tol=0.02):
    """The decorative spiral: small identical met2 squares on a fixed grid."""
    lib = gdstk.read_gds(gds)
    top = next(c for c in lib.cells if not any(
        c.name in [r.cell.name for r in o.references if r.cell] for o in lib.cells if o is not c))
    squares = []
    for poly in top.polygons:
        if poly.layer != 69 or poly.datatype != 20:
            continue
        bb = poly.bounding_box()
        w, h = bb[1][0] - bb[0][0], bb[1][1] - bb[0][1]
        if abs(w - pitch) < tol and abs(h - pitch) < tol:
            squares.append(((bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2))
    if not squares:
        return None
    xs = sorted({round(x, 3) for x, _ in squares})
    ys = sorted({round(y, 3) for _, y in squares})
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    grid = [[0] * len(xs) for _ in ys]
    for x, y in squares:
        grid[yi[round(y, 3)]][xi[round(x, 3)]] = 1
    fig, ax = plt.subplots(figsize=(5.4, 5.4))
    ax.imshow(grid, origin="lower", cmap="Blues", interpolation="nearest")
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"decorative met2 logo: {len(squares)} squares, "
                 f"{len(xs)}x{len(ys)} bitmap\n"
                 f"electrically isolated - no vias, no cells", fontsize=9)
    fig.tight_layout(); fig.savefig(path, dpi=170); plt.close(fig)
    return {"squares": len(squares), "shape": (len(ys), len(xs)),
            "bbox": (min(x for x, _ in squares), min(y for _, y in squares),
                     max(x for x, _ in squares), max(y for _, y in squares))}


def fig_communities(cells_json, gds, an, path, max_terms=20):
    """Colour logic cells by netlist community - placement is a lossy projection of this."""
    cells = {d["id"]: d for d in json.load(open(cells_json))}
    widths = cell_widths(gds)
    g = nx.Graph()
    for net in an.nl["nets"]:
        terms = [t for t in net["terminals"]]
        if len(terms) > max_terms:
            continue
        for i in range(len(terms)):
            for j in range(i + 1, len(terms)):
                g.add_edge(terms[i][0], terms[j][0])
    comms = list(nx.community.asyn_lpa_communities(g, seed=1))
    comms.sort(key=len, reverse=True)
    cmap = plt.get_cmap("tab20")
    colour = {}
    for i, com in enumerate(comms):
        for n in com:
            colour[n] = cmap((i % 20) / 19.0)
    intra = sum(1 for a, b in g.edges() if colour.get(a) == colour.get(b))
    fig, ax = plt.subplots(figsize=(7.2, 10.4))
    for iid, col in colour.items():
        d = cells.get(iid)
        if d is None or any(f in d["cell"] for f in FILL_CELLS):
            continue
        x, y, w, h = cell_box(d, widths)
        ax.add_patch(Rectangle((x, y), w, h, facecolor=col, edgecolor="none"))
    ax.add_patch(Rectangle((0, 0), 200, 300, fill=False, edgecolor="#444", lw=1.2))
    ax.set_xlim(-6, 206); ax.set_ylim(-6, 306); ax.set_aspect("equal")
    ax.set_xlabel("x (um)"); ax.set_ylabel("y (um)")
    ax.set_title(f"logic cells coloured by netlist community\n"
                 f"{len(comms)} communities, {intra}/{g.number_of_edges()} edges intra-community",
                 fontsize=10)
    fig.tight_layout(); fig.savefig(path, dpi=170, bbox_inches="tight"); plt.close(fig)
    return {"communities": len(comms), "edges": g.number_of_edges(), "intra": intra}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gds", default=str(paths.PUZZLE_GDS))
    ap.add_argument("--netlist", default=str(paths.PUZZLE_OUT / "03_netlist.json"))
    ap.add_argument("--cells", default=str(paths.PUZZLE_OUT / "01_cells.json"))
    ap.add_argument("--solution", default=str(paths.PUZZLE_OUT / "solution.json"))
    ap.add_argument("--outdir", default=str(paths.FIGURES_OUT))
    a = ap.parse_args(argv)
    os.makedirs(a.outdir, exist_ok=True)

    an = Analysis(a.netlist, quiet=True)
    rec = an.recover_regions()

    counts = fig_placement(a.cells, a.gds, an, rec, os.path.join(a.outdir, "placement.png"))
    print(f"placement.png   {dict(counts)}")

    if os.path.exists(a.solution):
        sol = json.load(open(a.solution))
        fig_regions(rec["map"], sol["bits"], os.path.join(a.outdir, "regions.png"))
        print("regions.png     region map + solution")
    else:
        print("regions.png     skipped (run solve.py first)")

    info = fig_logo(a.gds, os.path.join(a.outdir, "logo.png"))
    print(f"logo.png        {info}")

    ci = fig_communities(a.cells, a.gds, an, os.path.join(a.outdir, "communities.png"))
    print(f"communities.png {ci}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
