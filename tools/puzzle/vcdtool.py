"""Read and write the puzzle's VCD traces.

Reading uses the `vcdvcd` package.  Sampling is done on the posedge of `clk`; the
testbench changes every stimulus on the negedge, so the value present at a posedge is
the one the design captures.

  python -m puzzle.vcdtool ../example_inputs.vcd

splits the trace into trials by the `enable` window and decodes `I` as fixed-width
frames.  The framing that makes `example_inputs.vcd` readable is 11 bits per character:
8 data bits LSB-first followed by 3 idle zeros.  Bit 7 is always 0 because the payload
is 7-bit ASCII, so "7 data + 4 idle" fits equally well - the trace alone cannot tell them
apart.  Note this framing is a property of the *example stimulus*, not of the design:
`puzzle.gds` reads the same 121 bits as an 11x11 grid, one cell per cycle.
"""
import argparse
import sys

from vcdvcd import VCDVCD

CLOCK_PERIOD_PS = 10_000
POSEDGE_OFFSET_PS = 5_000


def parse_vcd(path):
    """-> (sym2name {symbol: name}, events [(time, symbol, value)]).

    Kept for compatibility with the rest of the toolchain; backed by vcdvcd.
    """
    vcd = VCDVCD(path, store_tvs=True)
    sym2name, events = {}, []
    for sym, sig in vcd.data.items():
        name = sig.references[0] if sig.references else sym
        name = name.split(".")[-1]
        name = name.split("[")[0] if name.endswith("]") and "[" in name else name
        sym2name[sym] = name
        for t, v in sig.tv:
            events.append((t, sym, v))
    events.sort(key=lambda e: e[0])
    return sym2name, events


def sample_posedges(sym2name, events, clk="clk"):
    """-> [(time, values_before_edge, values_after_edge)] for each posedge of clk."""
    by_t = {}
    for (t, sym, v) in events:
        by_t.setdefault(t, {})[sym] = v
    clk_sym = next(s for s, n in sym2name.items() if n == clk)
    cur, rows = {s: "x" for s in sym2name}, []
    for t in sorted(by_t):
        before = dict(cur)
        cur.update(by_t[t])
        if before.get(clk_sym) != "1" and cur.get(clk_sym) == "1":
            rows.append((t, before, dict(cur)))
    return rows


def trials(rows, sym2name, gate="enable"):
    """Index ranges of posedges where `gate` is high at the sampling edge."""
    sym = next(s for s, n in sym2name.items() if n == gate)
    out, run = [], None
    for i, (t, b, a) in enumerate(rows):
        if b.get(sym) == "1":
            run = [i, i] if run is None else [run[0], i]
        elif run is not None:
            out.append(tuple(run))
            run = None
    if run:
        out.append(tuple(run))
    return out


def decode(bits, frame=11, data=8, lsb_first=True):
    """Split `bits` into frames and decode each frame's data field as a character."""
    chars, tails = [], []
    for i in range(0, len(bits) - frame + 1, frame):
        f = bits[i:i + frame]
        payload = f[:data]
        chars.append(chr(int(payload[::-1] if lsb_first else payload, 2)))
        tails.append(f[data:])
    return "".join(chars), tails


def stimulus_from_vcd(path, serial="I"):
    """-> [(rst_n, enable, I)] sampled at every posedge, ready for GateSim.run()."""
    sym2name, events = parse_vcd(path)
    names = {n: s for s, n in sym2name.items()}
    rows = sample_posedges(sym2name, events)
    return [(int(r[1][names["rst_n"]]), int(r[1][names["enable"]]), int(r[1][names[serial]]))
            for r in rows]


def expected_from_vcd(path):
    """-> [(O_byte or None, success or None)] recorded at every posedge."""
    sym2name, events = parse_vcd(path)
    names = {n: s for s, n in sym2name.items()}
    rows = sample_posedges(sym2name, events)
    out = []
    for r in rows:
        o = r[2].get(names["O"], "x")
        s = r[2].get(names["success"], "x")
        out.append((int(o, 2) if set(o) <= set("01") else None,
                    int(s) if s in "01" else None))
    return out


# ---------------------------------------------------------------- writing
def write_vcd(path, bits, results=None, reset_cycles=3, tail=40, comment=""):
    """Write a stimulus VCD in exactly the format of example_inputs.vcd.

    1 ps timescale, 10 ns clock (posedge at 5 + 10k ns), every stimulus changing on the
    negedge, rst_n released at 30 ns and enable asserted at 40 ns for len(bits) cycles.
    `results` is the matching GateSim.run() output; when given, O[7:0] and success are
    written too so the file shows the design's response.
    """
    n = len(bits)
    total = reset_cycles + n + tail
    syms = {"clk": "!", "rst_n": '"', "enable": "#", "I": "$", "O": "%", "success": "&"}

    def stim_at(cyc):
        if cyc < reset_cycles:
            return 0, 0, 0
        k = cyc - reset_cycles
        return (1, 1, int(bits[k])) if k < n else (1, 0, 0)

    L = ["$date", "  gds2v vcdtool", "$end",
         "$version", f"\t{comment or 'generated stimulus'}", "$end",
         "$timescale", "\t1ps", "$end"]
    for name, sym in syms.items():
        width = 8 if name == "O" else 1
        kind = "wire" if name in ("O", "success") else "reg"
        L += ["$scope module puzzle $end",
              f"$var {kind} {width} {sym} {name}{' [7:0]' if name == 'O' else ''} $end",
              "$upscope $end"]
    L += ["$enddefinitions $end", "#0", "$dumpvars",
          f"x{syms['success']}", f"bx {syms['O']}",
          f"0{syms['I']}", f"0{syms['enable']}", f"0{syms['rst_n']}", f"0{syms['clk']}",
          "$end"]

    prev = {"rst_n": 0, "enable": 0, "I": 0, "O": None, "success": None}
    for cyc in range(total):
        rst_n, en, i_bit = stim_at(cyc)
        # negedge of the previous cycle is where stimulus changes; cycle 0 is special
        t_neg = cyc * CLOCK_PERIOD_PS
        t_pos = t_neg + POSEDGE_OFFSET_PS
        chunk = []
        if cyc:
            chunk.append(f"0{syms['clk']}")
        for name, val in (("rst_n", rst_n), ("enable", en), ("I", i_bit)):
            if prev[name] != val:
                chunk.append(f"{val}{syms[name]}")
                prev[name] = val
        if chunk or cyc:
            L.append(f"#{t_neg}")
            L += chunk
        L.append(f"#{t_pos}")
        L.append(f"1{syms['clk']}")
        if results is not None and cyc < len(results):
            o, s = results[cyc]
            if prev["O"] != o:
                L.append(f"b{o:b} {syms['O']}")
                prev["O"] = o
            if prev["success"] != s:
                L.append(f"{s}{syms['success']}")
                prev["success"] = s
    L.append(f"#{total * CLOCK_PERIOD_PS}")
    L.append(f"0{syms['clk']}")
    open(path, "w").write("\n".join(L) + "\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vcd")
    ap.add_argument("--frame", type=int, default=11)
    ap.add_argument("--data", type=int, default=8)
    ap.add_argument("--msb-first", action="store_true")
    ap.add_argument("--serial", default="I")
    a = ap.parse_args(argv)

    sym2name, events = parse_vcd(a.vcd)
    names = {n: s for s, n in sym2name.items()}
    rows = sample_posedges(sym2name, events)
    print(f"{a.vcd}: {len(events)} events, {len(rows)} posedges, "
          f"signals={sorted(sym2name.values())}")

    out_sym = names.get("O")
    for k, (i0, i1) in enumerate(trials(rows, sym2name)):
        bits = "".join(rows[i][1][names[a.serial]] for i in range(i0, i1 + 1))
        print(f"\n--- trial {k}: posedges {i0}..{i1} ({len(bits)} cycles, "
              f"t={rows[i0][0] / 1000:.1f}..{rows[i1][0] / 1000:.1f} ns)")
        print(f"  bits  : {bits}")
        if len(bits) % a.frame:
            print(f"  !! {len(bits)} bits is not a multiple of frame={a.frame}")
        text, tails = decode(bits, a.frame, a.data, not a.msb_first)
        idle_ok = all(set(t) <= {"0"} for t in tails if t)
        print(f"  frames: {len(tails)} x {a.frame}b ({a.data} data "
              f"{'MSB' if a.msb_first else 'LSB'}-first + {a.frame - a.data} idle); "
              f"idle bits all zero: {idle_ok}")
        print(f"  ascii : {text!r}")
        print(f"  grid  : {bits.count('1')} ones "
              f"(as 11x11: rows={[bits[r*11:(r+1)*11].count('1') for r in range(11)]})")
        if out_sym:
            msg = []
            for i in range(i1 + 1, min(i1 + 40, len(rows))):
                v = rows[i][2].get(out_sym, "")
                if set(v) <= set("01") and v:
                    c = int(v, 2)
                    if c:
                        msg.append(chr(c))
            if msg:
                print(f"  O     : {''.join(msg)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
