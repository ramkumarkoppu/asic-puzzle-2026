"""GDS -> gate-level netlist extraction for standard-cell layouts.

Standard cells are treated as black boxes.  When a GDS carries pin *labels* inside its
cell definitions (as sky130 does: text on the li1/met1 pin layers), each pin has a probe
point and no PDK is needed.  The flow is:

  1. pick the top cell (handling multiple / nested hierarchy / arrays)
  2. choose a technology profile (built-in sky130, a named one, or auto-detected)
  3. record every leaf standard-cell instance and its accumulated transform
  4. collect each leaf cell's pin labels in cell-local coordinates
  5. flatten the layout so vias and cell interiors become top-level geometry
  6. let KLayout's LayoutToNetlist resolve the conductor stack through the via cuts
  7. transform each pin label into top coordinates and probe_net() it
  8. name nets from the top-level port labels

Everything technology-specific lives in a TechProfile (see techprofile.py); this module
holds no hardcoded layer numbers.  It degrades honestly: a file with geometry but no pin
labels yields connectivity it cannot name, and that is reported rather than hidden.

Gotcha worth remembering: LayoutToNetlist.connect(a, b) declares *inter*-layer
connectivity only.  connect(a) -- single argument -- declares *intra*-layer
connectivity.  Without the latter, touching polygons on the same layer stay separate
nets and the extraction silently fragments.
"""
import os
import time

import klayout.db as db
import networkx as nx

from .techprofile import choose_profile

# sky130 output pin names.  Used for the driver-count smoke test and port-direction
# inference.  Kept to the sky130 set exactly: other pin names (e.g. mux2's select S)
# must NOT appear here or inputs get miscounted as drivers.  For other libraries the
# authoritative outputs come from the cell model, not this set.
OUTPUT_PINS = {"X", "Y", "Q", "Q_N", "COUT", "COUT_N", "SUM", "HI", "LO"}
# Supply pin / net names.  A superset over common libraries is safe here: it only ever
# excludes a net from the signal list, and real designs never name a signal VGND.
SUPPLY_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VNW", "VPW",
               "VDDPE", "VDDCE", "VSSE", "vpwr", "vgnd", "vdd", "vss"}
POWER_NETS = SUPPLY_PINS

# name substrings marking a cell as physical (no logic function)
_FILL_HINT = ("decap", "tapvpwrvgnd", "fill", "tap", "filler", "antenna", "diode",
              "endcap", "well_tap", "ptap", "ntap")
# subset that --prune-fill removes.  Antenna diodes are physical but carry a real net
# connection (a DIODE pin) and appear in reference netlists, so they are NOT pruned.
_PRUNE_HINT = ("decap", "tapvpwrvgnd", "fill", "tap", "filler", "endcap",
               "well_tap", "ptap", "ntap")

_DEF_ORIENT = {(0, False): "N", (180, False): "S", (90, False): "W", (270, False): "E",
               (0, True): "FS", (180, True): "FN", (90, True): "FW", (270, True): "FE"}


class Extraction:
    """Extract cells and nets from ``gds_path``.

    Attributes:
        instances  list of dicts: id, cell, x, y, orient, pins {pin: net_id}
        nets       list of dicts: id, name (or None), terminals [(inst_id, pin)]
        misses     pins that could not be resolved to a net (should be empty)
        profile    the TechProfile used
        report     capability report (see capability_report())

    ``profile``: a built-in name ("sky130"), "auto" to infer from geometry, or None to
    match a built-in and fall back to auto.

    ``prune_physical`` drops decap/tap/fill leaf cells before extraction.  They carry no
    signal pins, and on fill-dominated dies (e.g. Caravel user areas, ~500k placements
    of which <1% is logic) extraction is infeasible without this.
    """

    def __init__(self, gds_path, verbose=True, prune_physical=False,
                 profile=None, top=None):
        self.path = gds_path
        self.verbose = verbose
        self.prune_physical = prune_physical
        self.requested_profile = profile
        self.requested_top = top
        self.log = []
        self.warnings = []
        self._run()

    def _say(self, msg):
        self.log.append(msg)
        if self.verbose:
            print(msg)

    def _warn(self, msg):
        self.warnings.append(msg)
        self._say("  !! " + msg)

    # ------------------------------------------------------------------
    def _run(self):
        ly = db.Layout()
        ly.read(self.path)
        self.dbu = ly.dbu

        top = self._pick_top(ly)
        self.top_name = top.name
        self._say(f"read {self.path}: top={top.name} dbu={ly.dbu} cells={ly.cells()}")

        self.profile, prof_report = choose_profile(ly, self.requested_profile, self.verbose)
        if self.profile is None:
            self._warn("no geometry / no technology could be chosen; nothing to extract")
            self.instances, self.nets, self.misses = [], [], []
            self._finish_report(top, prof_report, hier="empty")
            return
        self._say(f"  profile: {self.profile.name}"
                  f"{' (auto-detected)' if self.profile.detected else ''} "
                  f"conductors={[c[1] for c in self.profile.conductors]}")

        # profile-derived probe order and preferred region per label layer
        self._PROBE_ORDER = [n for n, _l in self.profile.conductors]
        self._PREFER = {lnum: name for name, lnum in self.profile.conductors}
        for (l, _dt) in self.profile.pin_label_layers + self.profile.port_label_layers:
            self._PREFER.setdefault(l, self.profile.prefer_region_of(l))

        t = time.time()
        classes = self._classify_cells(ly)
        self.cell_classes = classes
        self.instances, hier = self._collect_instances(ly, top, classes)
        nlog = len(self.instances)
        self._say(f"  leaf standard-cell instances: {nlog}"
                  f"  (hierarchy: {hier}, {time.time() - t:.1f}s)")
        if not self.instances:
            self._warn("no labelled standard-cell instances found; "
                       "cannot recover a pin-level netlist from this file")

        cell_pins = self._collect_cell_pins(ly, classes)
        self.port_labels = self._collect_port_labels(ly, top)
        self._say(f"  distinct top-level port labels: "
                  f"{len({p[0] for p in self.port_labels})}")

        # With prune_physical, also drop the pruned cells' GEOMETRY before the
        # flatten: their polygons touch only the power rails (that is the premise of
        # pruning them), yet on fill-dominated dies they are the bulk of what the
        # netlist extractor would otherwise chew through.  Signal nets are unaffected;
        # supply rails may fragment where fill straps sat, which pruning already
        # accepts.
        if self.prune_physical and self._pruned_cellnames:
            cleared = 0
            for c in ly.each_cell():
                if c.name in self._pruned_cellnames:
                    c.clear()
                    cleared += 1
            self._say(f"  cleared geometry of {cleared} pruned cell definitions")

        # The explicit flatten is load-bearing: probing transformed cell-pin label
        # points against the un-flattened hierarchy resolves a handful of pins
        # differently (verified: the puzzle partition changes without it).
        t = time.time()
        top.flatten(-1, True)
        self._say(f"  flatten: {time.time() - t:.1f}s")
        self.layout, self.topcell = ly, top
        t = time.time()
        self.l2n, self.regions = self._build_l2n(ly, top)
        self._say(f"  connectivity extraction: {time.time() - t:.1f}s"
                  f" ({self._l2n_threads} threads)")

        t = time.time()
        if self.instances:
            self._probe(cell_pins)
            self._say(f"  pin probing: {time.time() - t:.1f}s")
        else:
            self.nets, self.misses = [], []
        self._finish_report(top, prof_report, hier)

    # ------------------------------------------------------------------
    def _pick_top(self, ly):
        tops = ly.top_cells()
        if not tops:
            raise ValueError("GDS has no cells")
        if self.requested_top is not None:
            for c in ly.each_cell():
                if c.name == self.requested_top:
                    return c
            raise ValueError(f"requested top {self.requested_top!r} not found")
        if len(tops) == 1:
            return tops[0]
        # multiple tops: pick the one covering the most instances, warn about the rest
        best = max(tops, key=lambda c: sum(1 for _ in c.each_inst()))
        self._warn(f"{len(tops)} top cells {[c.name for c in tops]}; "
                   f"using {best.name!r} (most instances). "
                   f"Pass top=<name> to choose another.")
        return best

    def _classify_cells(self, ly):
        """leaf cell name -> ('logic' | 'physical' | 'unlabeled').

        A leaf cell (no child instances) is the granularity of a standard cell.  It is
        'logic' if it carries at least one non-supply pin label, 'physical' if it has
        only supply labels or a fill-like name, 'unlabeled' otherwise (e.g. via cells).
        """
        label_layers = self.profile.pin_label_layers
        out = {}
        for c in ly.each_cell():
            if not c.is_leaf():
                continue
            texts = []
            for (l, dt) in label_layers:
                li = ly.layer(l, dt)
                if li < 0:
                    continue
                for s in c.shapes(li).each(db.Shapes.STexts):
                    texts.append(s.text.string)
            signal = [t for t in texts if t not in SUPPLY_PINS]
            fill_like = any(h in c.name.lower() for h in _FILL_HINT)
            if signal and not fill_like:
                out[c.name] = "logic"
            elif texts or fill_like:
                out[c.name] = "physical"
            else:
                out[c.name] = "unlabeled"
        return out

    @staticmethod
    def _iter_element_trans(inst):
        """All placement transforms of an instance, expanding AREF arrays.

        KLayout exposes an array as base transform + counts na/nb along vectors a/b;
        element (ia, ib) is the base shifted by ia*a + ib*b.
        """
        base = inst.cplx_trans
        if inst.is_regular_array() and inst.size() > 1:
            a, b = inst.a, inst.b
            for ia in range(inst.na):
                for ib in range(inst.nb):
                    off = db.Vector(ia * a.x + ib * b.x, ia * a.y + ib * b.y)
                    yield db.ICplxTrans(off) * base
        else:
            yield base

    def _collect_instances(self, ly, top, classes):
        """Recursively gather logic + physical leaf placements (arrays expanded).

        With prune_physical, decap/tap/fill placeholders are dropped, but logic cells
        and connection-bearing physical cells (antenna diodes) are kept.
        """
        insts = []
        pruned = 0
        # per-cell-name decision, computed once: 'keep' / 'skip' / 'prune' / 'walk'.
        # On fill-dominated dies (Caravel: 487k placements) the walk visits every
        # placement, so per-instance name matching would dominate the runtime.
        decision = {}

        def decide(child):
            name = child.name
            d = decision.get(name)
            if d is None:
                if not child.is_leaf():
                    d = "walk"
                else:
                    cls = classes.get(name, "unlabeled")
                    if cls == "unlabeled":
                        d = "skip"
                    elif (self.prune_physical and cls == "physical"
                            and any(h in name.lower() for h in _PRUNE_HINT)):
                        d = "prune"
                    else:
                        d = "keep"
                decision[name] = d
            return d

        def walk(cell, trans):
            nonlocal pruned
            for inst in cell.each_inst():
                child = inst.cell
                d = decide(child)
                if d == "skip":
                    continue
                if d == "prune":
                    pruned += inst.size() if inst.is_regular_array() else 1
                    continue
                for et in self._iter_element_trans(inst):
                    gt = trans * et
                    if d == "keep":
                        insts.append((child.name, gt))
                    else:
                        walk(child, gt)

        walk(top, db.ICplxTrans())
        self._pruned_cellnames = {n for n, d in decision.items() if d == "prune"}
        if self.prune_physical and pruned:
            self._say(f"  pruned {pruned} decap/tap/fill placements")

        recs = []
        for cname, t in insts:
            ang = int(round(t.angle)) % 360
            mir = bool(t.is_mirror())
            recs.append({"cell": cname, "trans": t,
                         "x": t.disp.x * ly.dbu, "y": t.disp.y * ly.dbu,
                         "angle": ang, "mirror": mir,
                         "orient": _DEF_ORIENT.get((ang, mir), f"R{ang}{'M' if mir else ''}")})
        recs.sort(key=lambda d: (round(d["y"], 4), round(d["x"], 4), d["cell"]))
        for i, d in enumerate(recs):
            d["id"] = i
        depth = self._hierarchy_depth(top)
        hier = "flat" if depth <= 1 else f"nested (depth {depth})"
        return recs, hier

    @staticmethod
    def _hierarchy_depth(cell, seen=None):
        seen = seen or {}
        if cell.name in seen:
            return seen[cell.name]
        d = 0
        for inst in cell.each_inst():
            if not inst.cell.is_leaf():
                d = max(d, Extraction._hierarchy_depth(inst.cell, seen))
        seen[cell.name] = d + 1
        return d + 1

    def _collect_cell_pins(self, ly, classes):
        """labelled (logic or physical) cell name -> [(pin_name, local db.Point, layer)].

        Physical cells are included because some carry a real connection worth probing
        (e.g. an antenna diode's DIODE pin); their supply-only labels are harmless.
        """
        label_layers = self.profile.pin_label_layers
        out = {}
        for c in ly.each_cell():
            if classes.get(c.name) not in ("logic", "physical"):
                continue
            pts = []
            for (lnum, dt) in label_layers:
                li = ly.layer(lnum, dt)
                if li < 0:
                    continue
                for s in c.shapes(li).each(db.Shapes.STexts):
                    t = s.text
                    pts.append((t.string, db.Point(t.x, t.y), lnum))
            out[c.name] = pts
        return out

    def _collect_port_labels(self, ly, top):
        labels = []
        for (lnum, dt) in self.profile.port_label_layers:
            li = ly.layer(lnum, dt)
            if li < 0:
                continue
            for s in top.shapes(li).each(db.Shapes.STexts):
                t = s.text
                labels.append((t.string, db.Point(t.x, t.y), lnum))
        return labels

    def _build_l2n(self, ly, top):
        l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(ly, top, []))
        # netlist extraction parallelises well; cap at 8 to stay polite on big boxes
        self._l2n_threads = 1
        if hasattr(l2n, "threads"):
            l2n.threads = self._l2n_threads = min(os.cpu_count() or 1, 8)
        regions = {}
        for name, lnum in self.profile.conductors:
            parts = []
            for dt in tuple(self.profile.draw_dt) + tuple(self.profile.pin_dt):
                li = ly.layer(lnum, dt)
                if li >= 0:
                    parts.append(l2n.make_polygon_layer(li, f"{name}_{dt}"))
            if not parts:
                # declare an empty layer so probes on this conductor return None cleanly
                parts = [l2n.make_polygon_layer(ly.layer(lnum, self.profile.draw_dt[0]),
                                                f"{name}_empty")]
            for p in parts:
                l2n.connect(p)
            for p in parts[1:]:
                l2n.connect(parts[0], p)
            regions[name] = parts[0]
        name_of = {lnum: n for n, lnum in self.profile.conductors}
        for cname, lnum, lo, up in self.profile.cuts:
            made = False
            for dt in self.profile.cut_dt:
                li = ly.layer(lnum, dt)
                if li < 0:
                    continue
                cut = l2n.make_polygon_layer(li, f"{cname}_{dt}")
                l2n.connect(cut)
                l2n.connect(regions[lo], cut)
                l2n.connect(cut, regions[up])
                made = True
            if not made:
                # no cut geometry on the expected layer: connect where conductors
                # overlap - a guess, so say so (it can invent connectivity on a
                # malformed GDS; it never fires when the cut layer exists)
                self._warn(f"cut layer {cname} ({lnum}) has no geometry; "
                           f"connecting {lo}/{up} wherever they overlap")
                l2n.connect(regions[lo], regions[up])
        l2n.extract_netlist()
        return l2n, regions

    # ------------------------------------------------------------------
    def _probe_point(self, pt, prefer=None):
        order = ([prefer] if prefer else []) + \
                [p for p in self._PROBE_ORDER if p != prefer]
        for rn in order:
            n = self.l2n.probe_net(self.regions[rn], pt)
            if n is not None:
                return n
        # Fallback for sloppy layouts whose label anchor sits just off the pin shape
        # (seen in some PDK conversions).  Only reached when the exact point missed,
        # so exact layouts are bit-for-bit unaffected.
        for d in (5, 25):       # dbu steps: 5 nm, 25 nm at 1 nm dbu
            for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d)):
                jpt = db.Point(pt.x + dx, pt.y + dy)
                for rn in order:
                    n = self.l2n.probe_net(self.regions[rn], jpt)
                    if n is not None:
                        self._fallback_hits += 1
                        return n
        return None

    @staticmethod
    def _key(n):
        return n.expanded_name() + "@" + n.circuit().name

    def _probe(self, cell_pins):
        """Probe every pin label, then merge the nets that a single pin touches.

        A cell pin is one electrical node by definition.  sky130 cells label a pin
        several times (dfrtp_2 carries four Q labels, nand2_2 three Y labels) and
        those labels can sit on *separate* li1 islands, joined only through the
        cell's internal poly/diff/licon - layers this black-box extraction
        deliberately does not model.  Probing just the first label therefore leaves
        the other islands orphaned as driverless net fragments.  Probing all of them
        and unioning the results repairs that without modelling cell internals.
        """
        self.misses = []
        self._fallback_hits = 0
        pin_keys, key_obj = {}, {}

        for d in self.instances:
            t = d["trans"]
            touched = {}
            for pinname, lp, lnum in cell_pins.get(d["cell"], []):
                n = self._probe_point(t.trans(lp), self._PREFER.get(lnum))
                if n is None:
                    continue
                k = self._key(n)
                key_obj[k] = n
                touched.setdefault(pinname, set()).add(k)
            for pinname in {p for p, _, _ in cell_pins.get(d["cell"], [])}:
                if pinname not in touched and pinname not in SUPPLY_PINS:
                    self.misses.append((d["id"], d["cell"], pinname))
            for pinname, ks in touched.items():
                pin_keys[(d["id"], pinname)] = ks

        port_key = []
        for s, pt, lnum in self.port_labels:
            n = self._probe_point(pt, self._PREFER.get(lnum))
            if n is None:
                self._say(f"  !! port label {s!r} probed to nothing")
                continue
            k = self._key(n)
            key_obj[k] = n
            port_key.append((s, k))

        alias = nx.Graph()
        alias.add_nodes_from(key_obj)
        multi = 0
        for ks in pin_keys.values():
            if len(ks) > 1:
                multi += 1
                nx.add_path(alias, sorted(ks))

        comp_of, self.nets = {}, []
        for cid, comp in enumerate(nx.connected_components(alias)):
            self.nets.append({"id": cid, "keys": sorted(comp), "name": None, "terminals": []})
            for k in comp:
                comp_of[k] = cid

        for d in self.instances:
            d["pins"] = {}
        for (iid, pin), ks in sorted(pin_keys.items()):
            cid = comp_of[next(iter(ks))]
            self.nets[cid]["terminals"].append((iid, pin))
            self.instances[iid]["pins"][pin] = cid

        named = 0
        for s, k in port_key:
            rec = self.nets[comp_of[k]]
            if rec["name"] and rec["name"] != s:
                self._say(f"  !! net already named {rec['name']!r}, also labelled {s!r}")
            rec["name"] = s
            named += 1

        self._say(f"  pin labels landing on >1 net fragment (merged): {multi}")
        if self._fallback_hits:
            self._say(f"  labels resolved only by near-point fallback: "
                      f"{self._fallback_hits}")
        self._say(f"  nets: {len(self.nets)}; unresolved pins: {len(self.misses)}")
        self._say(f"  named nets from labels: {named}")

    # ------------------------------------------------------------------
    def signal_nets(self):
        return [n for n in self.nets if n["name"] not in POWER_NETS]

    def logic_instances(self):
        return [d for d in self.instances
                if self.cell_classes.get(d["cell"], "logic") == "logic"]

    def _finish_report(self, top, prof_report, hier):
        from . import cells as _cells
        recognised = blackbox = 0
        for name in {d["cell"] for d in getattr(self, "instances", [])}:
            if self.cell_classes.get(name) != "logic":
                continue
            try:
                m = _cells.parse_cell(name)
                recognised += 1 if m.kind != "BLACKBOX" else 0
                blackbox += 1 if m.kind == "BLACKBOX" else 0
            except Exception:
                blackbox += 1
        n_logic = len(self.logic_instances()) if getattr(self, "instances", []) else 0
        named = sum(1 for n in getattr(self, "nets", []) if n["name"])
        # which downstream stages are usable
        stages = {}
        stages["structural_verilog"] = (bool(getattr(self, "nets", [])),
                                        "" if self.nets else "no nets extracted")
        can_sim = self.nets and blackbox == 0 and not self.misses
        stages["simulation"] = (bool(can_sim),
                                "" if can_sim else
                                ("unresolved pins" if self.misses else
                                 "blackbox cells (unknown function)" if blackbox else
                                 "no nets"))
        stages["behavioral_verilog"] = stages["simulation"]
        stages["lift_rtl"] = stages["simulation"]
        stages["schematic"] = (bool(getattr(self, "instances", [])),
                               "" if self.instances else "no instances")
        self.report = {
            "file": self.path,
            "top": self.top_name,
            "profile": self.profile.name if self.profile else None,
            "profile_detected": bool(self.profile and self.profile.detected),
            "profile_detail": prof_report,
            "hierarchy": hier,
            "instances_logic": n_logic,
            "instances_total": len(getattr(self, "instances", [])),
            "cell_types_recognised": recognised,
            "cell_types_blackbox": blackbox,
            "nets": len(getattr(self, "nets", [])),
            "nets_named": named,
            "unresolved_pins": len(getattr(self, "misses", [])),
            "stages": stages,
            "warnings": list(self.warnings),
        }

    def capability_report(self):
        r = self.report
        L = [f"== gds2v capability report: {r['file']} ==",
             f"  top cell        : {r['top']}",
             f"  technology      : {r['profile']}"
             f"{' (AUTO-DETECTED - verify)' if r['profile_detected'] else ''}",
             f"  hierarchy       : {r['hierarchy']}",
             f"  logic instances : {r['instances_logic']} "
             f"({r['cell_types_recognised']} known types, "
             f"{r['cell_types_blackbox']} blackbox)",
             f"  nets            : {r['nets']} ({r['nets_named']} named from labels)",
             f"  unresolved pins : {r['unresolved_pins']}"]
        L.append("  downstream stages:")
        for stage, (ok, why) in r["stages"].items():
            L.append(f"    {'[available]' if ok else '[disabled  ]'} {stage}"
                     + (f"  ({why})" if not ok else ""))
        for w in r["warnings"]:
            L.append(f"  warning: {w}")
        return "\n".join(L)

    def undriven_nets(self):
        """Nets without exactly one driving (output) pin - a correctness smoke test."""
        bad = []
        for n in self.nets:
            if n["name"] in POWER_NETS:
                continue
            drv = [t for t in n["terminals"] if t[1] in OUTPUT_PINS]
            if len(drv) != 1 and not (n["name"] and not drv):
                bad.append((n["id"], n["name"], len(drv), len(n["terminals"])))
        return bad
