"""Validate an Extraction against ground truth (a DEF plus a gate-level Verilog netlist).

Only the warmup design ships with ground truth, so this is what proves the flow
before it is pointed at puzzle.gds.
"""
import collections
import re

_ESC = re.compile(r"\\\S+[ \t]")
_PLAIN = re.compile(r"[A-Za-z_][\w$]*")
_CONST = re.compile(r"\d+'[bBdDhH][0-9a-fA-FxzXZ_]+")
# a net reference may carry a bus index: la_data_in[64] is one net, distinct from [65]
_NET = re.compile(r"[A-Za-z_]\w*(?:\[\d+\])?")


def _ident(s, i, net=False):
    """Return (identifier, next_index) or (None, i).

    Escaped Verilog identifiers (``\\sr_a/_16_ `` - the trailing space is part of
    the token) are normalised by dropping the leading backslash, so they compare
    equal to the same name as it appears in DEF.  With ``net=True`` a trailing bus
    index (``la_data_in[64]``) is kept, so bus bits stay distinct nets.
    """
    while i < len(s) and s[i] in " \t\n":
        i += 1
    for rx in (_ESC, _CONST, _NET if net else _PLAIN):
        m = rx.match(s, i)
        if m:
            return m.group(0).strip().lstrip("\\"), m.end()
    return None, i


def _balanced(s, i):
    """Index of the ')' matching the '(' at s[i]."""
    depth, j = 0, i
    while j < len(s):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(s) - 1


def parse_verilog_netlist(path):
    """-> ({instname: {'cell':..., 'conns': {pin: net}}}, module_name)"""
    src = open(path).read()
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)

    insts = {}
    for m in re.finditer(r"\b(sky130_fd_sc_hd__\w+)\b", src):
        cell = m.group(1)
        name, i = _ident(src, m.end())
        if name is None:
            continue
        while i < len(src) and src[i] in " \t\n":
            i += 1
        if i >= len(src) or src[i] != "(":
            continue
        body = src[i + 1:_balanced(src, i)]

        conns, k = {}, 0
        while True:
            d = body.find(".", k)
            if d < 0:
                break
            pm = _PLAIN.match(body, d + 1)
            if not pm:
                k = d + 1
                continue
            o = body.find("(", pm.end())
            if o < 0:
                break
            e = _balanced(body, o)
            conns[pm.group(0)], _ = _ident(body, o + 1, net=True)
            k = e + 1
        insts[name] = {"cell": cell, "conns": conns}

    mod = re.search(r"\bmodule\s+(\w+)", src)
    return insts, (mod.group(1) if mod else None)


def parse_def_components(path):
    """-> {instname: (cell, x_um, y_um, orient)}   (DEF integers are nanometres)"""
    out = {}
    txt = open(path).read()
    sec = re.search(r"^COMPONENTS\s+\d+\s*;(.*?)^END COMPONENTS", txt, re.S | re.M)
    if not sec:
        return out
    for m in re.finditer(r"-\s+(\S+)\s+(\S+)\s+(.*?);", sec.group(1), re.S):
        p = re.search(r"\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*(\w+)", m.group(3))
        if p:
            out[m.group(1)] = (m.group(2), int(p.group(1)) / 1000.0,
                               int(p.group(2)) / 1000.0, p.group(3))
    return out


def parse_def_net_count(path):
    m = re.search(r"^NETS\s+(\d+)\s*;", open(path).read(), re.M)
    return int(m.group(1)) if m else None


def map_instances(extraction, comps):
    """Map extracted instance id -> DEF instance name.

    A GDS SREF origin differs from the DEF placement point by a constant offset
    that depends only on (cell, orientation): (0,0) for N, (0,h) for FS, (w,0)
    for FN, (w,h) for S.  So group both sides by (cell, orientation), sort by
    (y, x) -- a constant translation preserves that order -- and pair by index.
    """
    gds_g, def_g = collections.defaultdict(list), collections.defaultdict(list)
    for d in extraction.instances:
        gds_g[(d["cell"], d["orient"])].append(d)
    for n, (c, x, y, o) in comps.items():
        def_g[(c, o)].append((n, x, y))

    idmap, problems = {}, []
    for k, gl in gds_g.items():
        dl = def_g.get(k, [])
        if len(gl) != len(dl):
            problems.append(f"group {k}: {len(gl)} in GDS vs {len(dl)} in DEF")
            continue
        gl_s = sorted(gl, key=lambda d: (round(d["y"], 3), round(d["x"], 3)))
        dl_s = sorted(dl, key=lambda t: (round(t[2], 3), round(t[1], 3)))
        offs = {(round(g["x"] - d[1], 3), round(g["y"] - d[2], 3))
                for g, d in zip(gl_s, dl_s)}
        if len(offs) != 1:
            problems.append(f"group {k}: inconsistent offsets {sorted(offs)[:4]}")
            continue
        for g, d in zip(gl_s, dl_s):
            idmap[g["id"]] = d[0]
    return idmap, problems


def validate(extraction, def_path, ref_v_path):
    """-> (report_lines, idmap, gold_instances)"""
    rep = []
    comps = parse_def_components(def_path)
    gold, gold_mod = parse_verilog_netlist(ref_v_path)
    rep.append(f"DEF components: {len(comps)}   DEF NETS: {parse_def_net_count(def_path)}")
    rep.append(f"reference netlist: module {gold_mod}, {len(gold)} instances")
    rep.append(f"extracted: {len(extraction.instances)} instances, "
               f"{len(extraction.signal_nets())} signal nets")

    idmap, problems = map_instances(extraction, comps)
    rep.append(f"instance match: {len(idmap)}/{len(extraction.instances)}"
               + ("  (all matched)" if not problems else f"  PROBLEMS: {problems[:4]}"))

    # Supply pins are excluded: power-aware references (02_netlist_with_power_rails.v,
    # OpenLane gate-level netlists) connect VPWR/VGND/VPB/VNB on every instance, which
    # would otherwise appear as two enormous extra nets on the gold side only.
    supply = {"VPWR", "VGND", "VPB", "VNB"}
    gold_net_of = {}
    for iname, rec in gold.items():
        for pin, net in rec["conns"].items():
            if pin not in supply:
                gold_net_of[(iname, pin)] = net

    mine, mismatches = {}, []
    for n in extraction.nets:
        terms = [t for t in ((idmap.get(i), p) for (i, p) in n["terminals"])
                 if t[0] is not None and t in gold_net_of]
        if terms:
            mine[n["id"]] = terms

    for nid, terms in mine.items():
        names = {gold_net_of[t] for t in terms}
        if len(names) != 1:
            mismatches.append(("split", nid, sorted(names)[:6], terms[:6]))

    gold_groups = collections.defaultdict(set)
    for t, nm in gold_net_of.items():
        gold_groups[nm].add(t)

    mine_groups = {nid: set(t) for nid, t in mine.items()}
    inv = {t: nid for nid, ts in mine_groups.items() for t in ts}
    for nm, ts in gold_groups.items():
        ids = {inv.get(t) for t in ts} - {None}
        if len(ids) > 1:
            mismatches.append(("merged-apart", nm, sorted(ids), sorted(ts)[:6]))

    rep.append(f"gold signal nets: {len(gold_groups)}   "
               f"extracted nets carrying gold pins: {len(mine)}")
    rep.append(f"MISMATCHES: {len(mismatches)}")
    for m in mismatches[:10]:
        rep.append(f"   {m}")

    gold_part = {frozenset(ts) for ts in gold_groups.values()}
    mine_part = {frozenset(ts) for ts in mine_groups.values()}
    rep.append(f"partition identical: {gold_part == mine_part}  "
               f"(only-in-gold={len(gold_part - mine_part)}, "
               f"only-in-extracted={len(mine_part - gold_part)})")
    for s in list(gold_part - mine_part)[:5]:
        rep.append(f"   only gold: {sorted(s)[:8]}")
    for s in list(mine_part - gold_part)[:5]:
        rep.append(f"   only extracted: {sorted(s)[:8]}")
    return rep, idmap, gold


def roundtrip(emitted_v_path, idmap, gold):
    """Re-parse emitted Verilog and confirm it carries the same signal connectivity.

    Supply pins are excluded on both sides: the emitted netlist omits them by default,
    and references like 02_netlist_with_power_rails.v carry VPWR/VGND/VPB/VNB
    connections that would otherwise appear as two extra (power) nets.
    """
    supply = {"VPWR", "VGND", "VPB", "VNB"}
    mine, _ = parse_verilog_netlist(emitted_v_path)

    gold_part = collections.defaultdict(set)
    for iname, rec in gold.items():
        for pin, net in rec["conns"].items():
            if pin not in supply:
                gold_part[net].add((iname, pin))
    gold_part = {k: v for k, v in gold_part.items() if v}
    allowed = {t for v in gold_part.values() for t in v}

    mine_part = collections.defaultdict(set)
    for iname, rec in mine.items():
        did = idmap[int(iname.split("_")[0][1:])]
        for pin, net in rec["conns"].items():
            if pin not in supply:
                mine_part[net].add((did, pin))

    gp = {frozenset(v) for v in gold_part.values()}
    mp = {frozenset(t for t in v if t in allowed) for v in mine_part.values()}
    mp.discard(frozenset())
    cells_ok = (collections.Counter(r["cell"] for r in gold.values())
                == collections.Counter(r["cell"] for r in mine.values()))
    return [f"re-parsed emitted netlist: {len(mine)} instances",
            f"gold nets: {len(gp)}   emitted nets: {len(mp)}",
            f"round-trip partition identical: {gp == mp}",
            f"cell-type census identical: {cells_ok}"]
