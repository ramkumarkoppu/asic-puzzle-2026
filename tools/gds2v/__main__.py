"""gds2v CLI - GDS in, netlists/RTL/schematic out.

  python -m gds2v <input.gds> -o <outdir> [options]

Writes, into <outdir> (numbering follows the recovery pipeline):
  report.txt       capability report: what was recovered, which stages ran
  01_cells.json    placed standard cells (type, coordinates, orientation)
  02_nets.json     extracted nets and their (instance, pin) terminals
  03_netlist.json  combined machine-readable netlist
  03_netlist.v     structural Verilog (cell instances)
  04_behavioral.v  de-synthesised RTL (assign / always style)      [needs known cells]
  05_schematic.svg gate-symbol circuit diagram
  06_rtl_recovered.v lifted RTL (registers + word-level functions) [needs known cells]
  cells_sim.v      behavioural models for every cell type used     [needs known cells]
  extract.log      run log

The stages marked [needs known cells] require every cell's FUNCTION to be known
(recognised sky130 naming).  On an unknown library they are skipped with a reason -
the structural netlist and schematic are still produced from connectivity alone.
"""
import argparse
import json
import os
import sys

from . import emit
from .extract import Extraction


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="gds2v", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("-m", "--module", default=None, help="override module name")
    ap.add_argument("--def", dest="deffile", default=None,
                    help="DEF file to validate against (needs --ref too)")
    ap.add_argument("--ref", default=None,
                    help="reference gate netlist to validate against")
    ap.add_argument("--power", action="store_true",
                    help="include VPWR/VGND connections in the Verilog")
    ap.add_argument("--cone", default=None,
                    help="also draw 05_schematic_<name>.svg restricted to the logic "
                         "cone of this output port")
    ap.add_argument("--no-schematic", action="store_true")
    ap.add_argument("--prune-fill", action="store_true",
                    help="drop decap/tap/fill instances before extraction; required "
                         "for fill-dominated dies (e.g. Caravel user areas)")
    ap.add_argument("--profile", default=None,
                    help="technology profile: a built-in name (sky130), 'auto' to infer "
                         "from geometry, or omit to match a built-in then fall back to auto")
    ap.add_argument("--top", default=None,
                    help="top cell name (for GDS files with several top cells)")
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap.parse_args(argv)


def write_netlist_outputs(e, a, out):
    """Stage 1: connectivity products - JSON dumps + structural Verilog.

    These need only the extraction (cells, pins, nets); they work for ANY library,
    including blackbox cells of unknown function.
    """
    json.dump([{k: d[k] for k in ("id", "cell", "x", "y", "orient")}
               for d in e.instances], open(out("01_cells.json"), "w"), indent=1)
    json.dump([{"id": n["id"], "name": n["name"],
                "terminals": [[i, p] for (i, p) in n["terminals"]]}
               for n in e.nets], open(out("02_nets.json"), "w"), indent=1)
    print(f"\nwrote {out('01_cells.json')}   ({len(e.instances)} cells, "
          f"{len(e.logic_instances())} logic)")
    print(f"wrote {out('02_nets.json')}     ({len(e.nets)} nets)")

    nl = emit.build(e, a.module)
    nl_json = emit.to_json(nl)
    json.dump(nl_json, open(out("03_netlist.json"), "w"), indent=1)
    open(out("03_netlist.v"), "w").write(emit.to_verilog(nl, include_power=a.power))
    print(f"wrote {out('03_netlist.v')}     ({len(nl['ports'])} ports, "
          f"{len(nl['instances'])} instances)")
    return nl, nl_json


def write_function_outputs(e, nl, nl_json, out):
    """Stage 2: function products - behavioural RTL, cell models, lifted RTL.

    Only possible when every cell's boolean/sequential function is known.  The lift
    is verified before it is trusted: shift-register semantics are proven dynamically
    against the netlist on random stimulus, and rejected wholesale on any mismatch.
    Returns the set of undeclared identifiers from the lint (empty = clean).
    """
    from . import cells
    open(out("04_behavioral.v"), "w").write(emit.to_behavioral_verilog(nl))
    open(out("cells_sim.v"), "w").write(
        cells.to_verilog_models(d["cell"] for d in e.instances))
    print(f"wrote {out('04_behavioral.v')}  (de-synthesised RTL)")
    print(f"wrote {out('cells_sim.v')}     (behavioural models, "
          f"{len(set(d['cell'] for d in e.instances))} cell types)")

    from .lift import Lifter, lint_verilog, verify_shift_semantics
    lf = Lifter(nl_json).run()
    if lf.registers:
        import random
        from .sim import GateSim
        gsim = GateSim(nl_json)
        rnd = random.Random(0)
        inports = [p["name"] for p in nl_json["ports"]
                   if p["dir"] == "input" and p["name"] != "clk"]
        stim = [{p: 0 for p in inports} | {"clk": 0} for _ in range(3)] + \
               [{p: rnd.getrandbits(1) for p in inports} | {"clk": 0}
                for _ in range(500)]
        for s in stim[3:]:
            if "rst_n" in s and rnd.random() > 0.02:
                s["rst_n"] = 1
        bad = verify_shift_semantics(gsim, lf, stim)
        if bad:
            print(f"LIFT REJECTED: shift semantics mismatched on {bad} register-cycles")
            lf.registers, lf.templates = [], []
            lf.run()

    text = lf.to_verilog()
    undecl = lint_verilog(text)
    open(out("06_rtl_recovered.v"), "w").write(text)
    print(f"wrote {out('06_rtl_recovered.v')}  "
          f"({len(lf.registers)} shift registers, {len(lf.templates)} word-level "
          f"functions proven, {len(lf.leftover)} gates + "
          f"{len(lf.unlifted)} flops left flat)")
    if undecl:
        print(f"WARNING: recovered RTL references undeclared identifiers: "
              f"{sorted(undecl)[:8]}")
    return undecl


def write_schematics(nl, nl_json, a, out):
    """Stage 3: gate-symbol schematic (full and/or one output's logic cone)."""
    from . import schematic
    if not a.no_schematic:
        stats = schematic.draw(nl_json, out("05_schematic.svg"), title=nl["module"])
        print(f"wrote {out('05_schematic.svg')}  ({stats['nodes']} symbols, "
              f"{stats['wires']} wires, {stats['columns']} columns)")
    if a.cone:
        safe = a.cone.replace("[", "").replace("]", "")
        s2 = schematic.draw(nl_json, out(f"05_schematic_{safe}.svg"),
                            cone=[a.cone], title=f"{nl['module']} - cone of {a.cone}")
        print(f"wrote {out(f'05_schematic_{safe}.svg')}  ({s2['nodes']} symbols)")


def validate_against_ground_truth(e, a, out):
    """Stage 4 (optional): compare against a DEF + reference netlist answer key."""
    from . import validate as V
    rep, idmap, gold = V.validate(e, a.deffile, a.ref)
    print("\n--- validation vs ground truth ---")
    print("\n".join(rep))
    print("\n".join(V.roundtrip(out("03_netlist.v"), idmap, gold)))


def main(argv=None):
    a = parse_args(argv)
    os.makedirs(a.outdir, exist_ok=True)
    out = lambda f: os.path.join(a.outdir, f)

    e = Extraction(a.gds, verbose=not a.quiet, prune_physical=a.prune_fill,
                   profile=a.profile, top=a.top)
    open(out("extract.log"), "w").write("\n".join(e.log))
    open(out("report.txt"), "w").write(e.capability_report() + "\n")
    print()
    print(e.capability_report())

    if not e.instances:
        json.dump([], open(out("01_cells.json"), "w"))
        json.dump([], open(out("02_nets.json"), "w"))
        print("\nnothing further to emit (no standard-cell instances recovered).")
        return 2

    nl, nl_json = write_netlist_outputs(e, a, out)

    blackbox = e.report["cell_types_blackbox"] > 0
    undecl = set()
    if blackbox:
        print(f"\nfunction-level stages skipped: {e.report['cell_types_blackbox']} "
              f"cell type(s) have unknown function (blackbox). Structural netlist and "
              f"schematic are still emitted; behavioural RTL, models, simulation and "
              f"lifting need a recognised library or a Liberty model.")
    else:
        undecl = write_function_outputs(e, nl, nl_json, out)

    if not a.no_schematic or a.cone:
        write_schematics(nl, nl_json, a, out)

    rc = 1 if undecl else 0
    if e.misses:
        print(f"WARNING: {len(e.misses)} pins could not be resolved to a net")
        rc = 1
    if not blackbox:
        bad = e.undriven_nets()
        print(f"self-check: nets without exactly one driver: {len(bad)}")
        if bad:
            print(f"   {bad[:5]}")

    if a.deffile and a.ref:
        validate_against_ground_truth(e, a, out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
