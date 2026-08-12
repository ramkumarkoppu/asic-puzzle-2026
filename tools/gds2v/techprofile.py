"""Technology profiles: what layers mean, so extraction is not hardwired to sky130.

A profile names the conductor stack (bottom to top), the cut/via layer between each
adjacent pair, and the datatypes that carry drawing/pin/label shapes.  Extraction reads
everything else from the GDS itself.

Two ways to get a profile:
  * a built-in (``sky130``), identical to the tool's original behaviour, or
  * ``auto_detect(layout)``, which infers a plausible stack from geometry statistics
    when the PDK is unknown.  Auto-detection is best-effort and always reported as such.
"""
from dataclasses import dataclass, field

import klayout.db as db


@dataclass
class TechProfile:
    name: str
    # conductor name -> GDS layer number, ordered bottom to top
    conductors: list                      # list[(name, layer_number)]
    # cut name, layer_number, lower conductor name, upper conductor name
    cuts: list                            # list[(name, layer, lower, upper)]
    draw_dt: tuple = (20,)                # drawing datatypes to union into a conductor
    pin_dt: tuple = (16,)                 # pin-shape datatypes (also unioned)
    cut_dt: tuple = (44,)                # via cut datatypes
    # (layer, datatype) pairs carrying CELL pin-name text (inside leaf cells)
    pin_label_layers: tuple = ()
    # (layer, datatype) pairs carrying TOP-level PORT-name text
    port_label_layers: tuple = ()
    detected: bool = False               # True if produced by auto_detect
    notes: str = ""

    def conductor_layers(self):
        return {lnum for _n, lnum in self.conductors}

    # region preference for a pin-label layer: its own conductor, else the bottom one
    def prefer_region_of(self, layer):
        for name, lnum in self.conductors:
            if lnum == layer:
                return name
        return self.conductors[0][0] if self.conductors else None


SKY130 = TechProfile(
    name="sky130",
    conductors=[("li1", 67), ("met1", 68), ("met2", 69),
                ("met3", 70), ("met4", 71), ("met5", 72)],
    cuts=[("mcon", 67, "li1", "met1"), ("via", 68, "met1", "met2"),
          ("via2", 69, "met2", "met3"), ("via3", 70, "met3", "met4"),
          ("via4", 71, "met4", "met5")],
    draw_dt=(20,), pin_dt=(16,), cut_dt=(44,),
    # cell pins: li1 & met1 (5 and 59) plus nwell (VPB/VNB)
    pin_label_layers=((67, 5), (67, 59), (68, 5), (68, 59), (64, 5), (64, 59)),
    # top ports: met3/met4/met5 (outer) plus met1/met2
    port_label_layers=((70, 5), (71, 5), (72, 5), (68, 5), (69, 5)),
    notes="built-in; matches the tool's original hardcoded layer map",
)

BUILTINS = {"sky130": SKY130}


def _layer_stats(layout):
    """Per (layer, datatype): shape count, total area, and text count, over all cells."""
    stats = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        key = (info.layer, info.datatype)
        npoly = ntext = 0
        area = 0
        for c in layout.each_cell():
            shp = c.shapes(li)
            npoly += shp.size()
            for s in shp.each(db.Shapes.SPolygons | db.Shapes.SBoxes):
                area += s.polygon.area() if s.is_polygon() else s.box.area()
            for _t in shp.each(db.Shapes.STexts):
                ntext += 1
        if npoly or ntext:
            stats[key] = {"shapes": npoly, "area": area * layout.dbu * layout.dbu,
                          "texts": ntext}
    return stats


def auto_detect(layout, verbose=False):
    """Infer a TechProfile from geometry when the technology is unknown.

    Heuristics, deliberately conservative:
      * conductors are the drawing layers (datatype 0 or 20) carrying real area, ordered
        by layer number (GDS convention: metals ascend with layer number); boundary /
        outline layers are excluded (they carry no pin/label text);
      * the cut/via datatype is assumed to be 44 (the sky130/OpenLane convention); cuts
        are NOT inferred from shape size, and when no 44 geometry exists extraction
        connects conductors where they physically overlap;
      * pin datatypes are the remaining non-label datatypes on conductor layers;
      * label datatypes are those holding TEXT.
    Returns (profile, report_dict). Never raises on a valid layout.
    """
    stats = _layer_stats(layout)
    if not stats:
        return None, {"reason": "no geometry"}

    # datatypes that hold text -> label datatypes
    label_dts = sorted({dt for (_l, dt), s in stats.items() if s["texts"]})
    text_layers = sorted({(l, dt) for (l, dt), s in stats.items() if s["texts"]})

    # candidate drawing datatypes: the ones with the most total area
    dt_area = {}
    for (l, dt), s in stats.items():
        dt_area.setdefault(dt, 0.0)
        dt_area[dt] += s["area"]
    draw_dt_candidates = [dt for dt in sorted(dt_area, key=lambda d: -dt_area[d])
                          if dt in (0, 20) and dt_area[dt] > 0]
    draw_dt = tuple(draw_dt_candidates[:1]) or (0,)

    # conductor layers: layers carrying real drawing area on a draw datatype
    cond_area, cond_shapes = {}, {}
    for (l, dt), s in stats.items():
        if dt in draw_dt:
            cond_area[l] = cond_area.get(l, 0.0) + s["area"]
            cond_shapes[l] = cond_shapes.get(l, 0) + s["shapes"]
    conductors = [l for l in cond_area if cond_area[l] > 0]
    # exclude boundary/outline layers: routing layers carry pin/label text on the same
    # layer number; an outline is a few big, label-less boxes.  Keep text-bearing draw
    # layers when any exist, else drop layers that are few-and-large.
    text_layer_nums = {l for (l, dt), s in stats.items() if s["texts"]}
    with_text = [l for l in conductors if l in text_layer_nums]
    if with_text:
        conductors = with_text
    else:
        conductors = [l for l in conductors
                      if cond_shapes[l] >= 4 or cond_area[l] / max(cond_shapes[l], 1) < 1.0]
    conductors = sorted(conductors)

    # cut/via datatype: 44 is the near-universal sky130/OpenLane convention.  We do NOT
    # guess cuts from shape size (pin shapes can be just as small); when no 44 geometry
    # exists, _build_l2n connects conductors where they physically overlap instead.
    cut_dt = (44,)

    cond_names = {l: f"cond{l}" for l in conductors}
    prof_conductors = [(cond_names[l], l) for l in conductors]
    cuts = []
    for i in range(len(conductors) - 1):
        lo, up = conductors[i], conductors[i + 1]
        # a cut is conventionally on the lower conductor's layer number in sky130; here
        # we just declare a cut named for the gap and let extraction connect on cut_dt
        cuts.append((f"cut{lo}_{up}", lo, cond_names[lo], cond_names[up]))

    # union every datatype found on a conductor layer that is not a label or cut
    # datatype - we do not know which one is "pin", so include them all.  This lets
    # pin shapes on an unusual datatype still merge into their conductor.
    cond_set = set(conductors)
    pin_dt = tuple(sorted({dt for (l, dt) in stats
                           if l in cond_set and dt not in draw_dt
                           and dt not in label_dts and dt not in cut_dt})) or (16,)

    # without a PDK we cannot tell cell-pin labels from port labels, so use every text
    # layer for both; over-inclusion is safe here because leaf cells and the flattened
    # top are scanned separately
    prof = TechProfile(
        name="auto",
        conductors=prof_conductors,
        cuts=cuts,
        draw_dt=draw_dt,
        pin_dt=pin_dt,
        cut_dt=cut_dt,
        pin_label_layers=tuple(text_layers),
        port_label_layers=tuple(text_layers),
        detected=True,
        notes="auto-detected from geometry; verify against the PDK if available",
    )
    report = {
        "conductors": prof_conductors,
        "cut_datatypes": list(cut_dt),
        "draw_datatypes": list(draw_dt),
        "label_datatypes": list(label_dts),
        "text_layers": text_layers,
    }
    if verbose:
        print("auto-detect:", report)
    return prof, report


def looks_like(layout, profile):
    """How many of this profile's conductor layers (on a drawing datatype) are present?"""
    present = {(layout.get_info(li).layer, layout.get_info(li).datatype)
               for li in layout.layer_indexes()}
    hits = 0
    for _n, lnum in profile.conductors:
        for dt in profile.draw_dt:
            if (lnum, dt) in present:
                hits += 1
                break
    return hits


def choose_profile(layout, requested=None, verbose=False):
    """Pick a profile for `layout`.

    requested: profile name (built-in), 'auto', or None (try built-ins, else auto).
    Returns (profile, report_dict).
    """
    if requested and requested in BUILTINS:
        return BUILTINS[requested], {"source": "requested builtin"}
    if requested == "auto":
        p, r = auto_detect(layout, verbose)
        return p, {"source": "auto", **(r or {})}
    if requested is None:
        # pick the built-in with the most conductor layers present, else auto
        best, score = None, 0
        for prof in BUILTINS.values():
            s = looks_like(layout, prof)
            if s > score:
                best, score = prof, s
        if best is not None and score >= max(2, len(best.conductors) // 2):
            return best, {"source": f"matched builtin {best.name}",
                          "conductor_layers_present": score}
        p, r = auto_detect(layout, verbose)
        return p, {"source": "auto (no builtin matched)", **(r or {})}
    raise ValueError(f"unknown profile {requested!r}; "
                     f"builtins: {sorted(BUILTINS)} or 'auto'")
