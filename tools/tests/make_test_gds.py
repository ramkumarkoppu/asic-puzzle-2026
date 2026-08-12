"""Synthesise tiny GDS files that stress the extractor's generality.

  python make_test_gds.py [outdir]

Each file is a VALID GDSII exercising one structural case that a sky130-only extractor
tends to mishandle.  Used by test_generality.py.  gdstk only - no PDK.
"""
import os
import sys

import gdstk


def _wire(cell, layer, dt, x0, y0, x1, y1, w=0.14):
    cell.add(gdstk.rectangle((min(x0, x1) - w / 2, min(y0, y1) - w / 2),
                             (max(x0, x1) + w / 2, max(y0, y1) + w / 2),
                             layer=layer, datatype=dt))


def alien_layers(path):
    """A clean 2-gate design on a fictional PDK's layer numbers (not sky130).

    MYLIB_and (pins A,B,Y) feeds MYLIB_inv (pins A,Y); top ports a,b -> out.  Pins are
    isolated within each cell and joined only by explicit top-level wires, so the
    extracted netlist is electrically well-formed.
    Conductors: M1=100/0 (pin 100/2, label 100/10), M2=101/0; via cut 100/3.
    """
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    M1, M2, PINDT, LBLDT, CUTDT = 100, 101, 2, 10, 3

    def leaf(name, pins):
        c = lib.new_cell(name)
        c.add(gdstk.rectangle((0, 0), (1.0, 1.4), layer=200, datatype=0))  # outline
        for pn, (x, y) in pins.items():
            # pin GEOMETRY on the pin datatype, pin TEXT on the label datatype
            # (mirrors real GDS, where these are distinct datatypes)
            c.add(gdstk.rectangle((x - 0.1, y - 0.1), (x + 0.1, y + 0.1),
                                  layer=M1, datatype=PINDT))
            c.add(gdstk.Label(pn, (x, y), layer=M1, texttype=LBLDT))
        return c

    g_and = leaf("MYLIB_and", {"A": (0.15, 1.05), "B": (0.15, 0.35), "Y": (0.85, 0.7)})
    g_inv = leaf("MYLIB_inv", {"A": (0.15, 0.7), "Y": (0.85, 0.7)})

    top = lib.new_cell("top")
    top.add(gdstk.Reference(g_and, (0, 0)))
    top.add(gdstk.Reference(g_inv, (2, 0)))
    # g_and.Y (0.85,0.7) -> g_inv.A (2.15,0.7) on M1
    _wire(top, M1, 0, 0.85, 0.7, 2.15, 0.7)
    # top ports: a -> and.A, b -> and.B, out -> inv.Y
    for name, (x, y) in (("a", (0.15, 1.05)), ("b", (0.15, 0.35)), ("out", (2.85, 0.7))):
        top.add(gdstk.Label(name, (x, y), layer=M1, texttype=LBLDT))
    _wire(top, M1, 0, 2.85, 0.7, 2.95, 0.7)  # inv.Y stub to the 'out' label
    lib.write_gds(path)


def multi_top(path):
    """Two independent top cells - top_cell() raises in KLayout."""
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    for nm in ("blockA", "blockB"):
        c = lib.new_cell(nm)
        c.add(gdstk.rectangle((0, 0), (1, 1), layer=68, datatype=20))
        c.add(gdstk.Label("p", (0.5, 0.5), layer=68, texttype=5))
    lib.write_gds(path)


def nested_hierarchy(path):
    """A real 2-level hierarchy: top -> row -> leaf, sky130-ish layers.

    The leaf cells are the only ones with pins; the extractor must descend, not just
    read top's direct children.
    """
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    leaf = lib.new_cell("cellX")
    leaf.add(gdstk.rectangle((0, 0), (1, 1.2), layer=236, datatype=0))
    for nm, x in (("A", 0.15), ("Y", 0.85)):
        leaf.add(gdstk.rectangle((x - 0.1, 0.5), (x + 0.1, 0.7), layer=67, datatype=16))
        leaf.add(gdstk.Label(nm, (x, 0.6), layer=67, texttype=5))
    row = lib.new_cell("row")
    for i in range(3):
        row.add(gdstk.Reference(leaf, (i * 1.5, 0)))
        _wire(row, 68, 20, i * 1.5 + 0.85, 0.6, i * 1.5 + 1.65, 0.6)  # leaf Y -> next A
    top = lib.new_cell("chip")
    top.add(gdstk.Reference(row, (0, 0)))
    top.add(gdstk.Reference(row, (0, 3)))
    lib.write_gds(path)


def aref_array(path):
    """An AREF (array reference): 4x1 repetition of a leaf, sky130-ish."""
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    leaf = lib.new_cell("arr_cell")
    leaf.add(gdstk.rectangle((0, 0), (1, 1.2), layer=236, datatype=0))
    leaf.add(gdstk.rectangle((0.05, 0.5), (0.25, 0.7), layer=67, datatype=16))
    leaf.add(gdstk.Label("A", (0.15, 0.6), layer=67, texttype=5))
    top = lib.new_cell("top")
    top.add(gdstk.Reference(leaf, (0, 0), columns=4, rows=1, spacing=(1.5, 0)))
    lib.write_gds(path)


def no_labels(path):
    """Geometry but NO pin labels anywhere - connectivity is possible, pin ID is not."""
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    leaf = lib.new_cell("blank")
    leaf.add(gdstk.rectangle((0, 0), (1, 1.2), layer=236, datatype=0))
    leaf.add(gdstk.rectangle((0.05, 0.5), (0.25, 0.7), layer=67, datatype=20))
    top = lib.new_cell("top")
    top.add(gdstk.Reference(leaf, (0, 0)))
    top.add(gdstk.Reference(leaf, (2, 0)))
    _wire(top, 68, 20, 0.15, 0.6, 2.15, 0.6)
    lib.write_gds(path)


def empty(path):
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    lib.new_cell("empty_top")
    lib.write_gds(path)


CASES = {
    "alien_layers.gds": alien_layers,
    "multi_top.gds": multi_top,
    "nested_hierarchy.gds": nested_hierarchy,
    "aref_array.gds": aref_array,
    "no_labels.gds": no_labels,
    "empty.gds": empty,
}


def main(argv=None):
    from gds2v import paths
    outdir = (argv or [None])[0] or str(paths.GEN_OUT)
    os.makedirs(outdir, exist_ok=True)
    for name, fn in CASES.items():
        p = os.path.join(outdir, name)
        fn(p)
        print(f"wrote {p} ({os.path.getsize(p)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
