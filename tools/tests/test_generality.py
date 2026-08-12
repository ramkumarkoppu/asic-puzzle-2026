"""Verify gds2v handles arbitrary valid GDS files - not just sky130.

  python test_generality.py

Generates synthetic GDS files (make_test_gds.py) that each stress one structural case a
sky130-only extractor mishandles, and asserts the tool now either extracts sensibly or
DEGRADES HONESTLY (never crashes, and never claims more than it proved):

  alien_layers      unknown PDK layer numbers -> auto-detect + blackbox function, but
                    connectivity + structural netlist + schematic still produced
  nested_hierarchy  cells inside sub-modules -> descended, not just top's children
  aref_array        AREF array placement -> expanded to individual instances
  multi_top         several top cells -> one chosen, others warned, no crash
  no_labels         geometry but no pin labels -> reported as unrecoverable, no crash
  empty             no geometry -> reported, no crash

Also re-checks that the sky130 and auto profiles agree on the sky130 warmup, proving the
generalisation did not change the built-in path.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for sibling test modules
import make_test_gds
from gds2v import paths
from gds2v.extract import Extraction
from testutil import Checks

GEN = str(paths.GEN_OUT)         # where the synthetic GDS fixtures are written


def extract(name, **kw):
    return Extraction(os.path.join(GEN, name + ".gds"), verbose=False, **kw)


def main(argv=None):
    c = Checks()
    make_test_gds.main([GEN])
    print("generated synthetic GDS files\n")

    # alien PDK: auto-detect, connectivity recovered, function correctly unknown
    e = extract("alien_layers", profile="auto")
    r = e.report
    c.check("alien_layers: auto-detected a profile", r["profile_detected"])
    c.check("alien_layers: 2 leaf cells found", r["instances_logic"] == 2)
    c.check("alien_layers: wire connectivity extracted (>=1 multi-terminal net)",
          any(len(n["terminals"]) >= 2 for n in e.nets))
    c.check("alien_layers: function stages disabled (blackbox), structural enabled",
          not r["stages"]["simulation"][0] and r["stages"]["structural_verilog"][0]
          and r["cell_types_blackbox"] == 2)
    # the and->inv wire is one net with two terminals (and.Y drives inv.A)
    wire_net = [n for n in e.nets if len(n["terminals"]) == 2 and not n["name"]]
    c.check("alien_layers: and.Y -> inv.A internal net recovered",
          any(sorted(p for _i, p in n["terminals"]) == ["A", "Y"] for n in wire_net))
    c.check("alien_layers: 3 top ports named from labels (a, b, out)",
          sorted(n["name"] for n in e.nets if n["name"]) == ["a", "b", "out"])

    # nested hierarchy: descend into sub-modules
    e = extract("nested_hierarchy", profile="auto")
    c.check("nested_hierarchy: hierarchy reported as nested",
          "nested" in e.report["hierarchy"])
    c.check("nested_hierarchy: 6 leaf placements collected (2 rows x 3)",
          e.report["instances_logic"] == 6, str(e.report["instances_logic"]))

    # AREF array: expanded
    e = extract("aref_array", profile="auto")
    xs = sorted(round(d["x"], 2) for d in e.instances)
    c.check("aref_array: 4 array elements expanded at x=0,1.5,3,4.5",
          xs == [0.0, 1.5, 3.0, 4.5], str(xs))

    # multiple tops: no crash, clear warning
    e = extract("multi_top", profile="auto")
    c.check("multi_top: did not crash, warned about multiple tops",
          any("top cells" in w for w in e.warnings))

    # no labels: honest 'cannot recover' report, no crash
    e = extract("no_labels", profile="auto")
    c.check("no_labels: reports it cannot recover a pin-level netlist",
          e.report["instances_logic"] == 0
          and any("cannot recover" in w for w in e.warnings))

    # empty: no crash
    e = extract("empty", profile="auto")
    c.check("empty: handled without crashing", e.report["nets"] == 0)

    # the built-in path is unchanged: sky130 warmup identical under 'sky130' and default
    a = Extraction(str(paths.WARMUP_GDS), verbose=False, profile="sky130")
    b = Extraction(str(paths.WARMUP_GDS), verbose=False)
    c.check("sky130 warmup: 230 instances, 84 signal nets, profile=sky130",
          len(a.instances) == 230 and len(a.signal_nets()) == 84
          and a.profile.name == "sky130")
    c.check("sky130 warmup: default profile selection also picks sky130",
          b.profile.name == "sky130" and len(b.signal_nets()) == 84)

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
