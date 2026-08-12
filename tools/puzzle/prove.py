"""SAT proofs about the extracted puzzle netlist, using z3 over a symbolic unroll.

  python -m puzzle.prove

solve.py establishes the answer constructively (unique at the RULES level, verified on
the netlist, all 121 single-bit flips fail).  This module proves the stronger claims
directly on the CIRCUIT, with no reliance on the recovered Star Battle interpretation:

  * uniqueness   - the answer is the only 121-bit input that ever raises `success`
                   (constrain success, get the one model, block it, get UNSAT)
  * completeness - over all 2^121 inputs the chip emits exactly five messages
                   (enumerate output windows until UNSAT), for both polarities of the
                   genuinely floating net n293
  * exclusivity  - `BIG BANG` requires all 121 bits set and `EMPTY SKY` all clear
                   (each with one bit negated is UNSAT)
  * independence - `success` cannot depend on the floating net (miter of the two
                   polarities is UNSAT)

The unroll reuses GateSim._step and CellModel.eval verbatim via gds2v.symsim, so the
formula is built by the same code the VCD replay validated; every SAT witness is also
replayed through the concrete simulator and must match, cross-checking the machinery.
"""
import argparse
import json
import re
import sys
import time

import z3

from gds2v import paths
from gds2v.sim import GateSim, standard_stimulus, message_of
from gds2v.symsim import Sym, ONE, ZERO, symbolic_run, model_bit

N_BITS = 121
RESET_CYCLES = 3
TAIL = 24                       # post-input cycles: longest message is 15 chars
OUTS = ["success"] + [f"O[{b}]" for b in range(8)]
EXPECTED = {"(* TWO STARS *)", "BIG BANG", "EMPTY SKY", "TRY AGAIN"}
TOUCH_RE = re.compile(r"TWO.NOT TOUC.")
MAX_MESSAGES = 12               # enumeration bound; the proof expects 5


def unrolled(sim, ivars, undriven):
    """Symbolic run of the puzzle protocol: reset, 121 bits, TAIL idle cycles."""
    stim = [{"rst_n": ZERO, "enable": ZERO, "I": ZERO}] * RESET_CYCLES
    stim += [{"rst_n": ONE, "enable": ONE, "I": iv} for iv in ivars]
    stim += [{"rst_n": ONE, "enable": ZERO, "I": ZERO}] * TAIL
    return symbolic_run(sim, stim, OUTS, undriven=undriven)


def window_of(trace):
    """The output window: per-cycle [O[0]..O[7]] Syms for the TAIL cycles."""
    return [[cyc[f"O[{b}]"] for b in range(8)] for cyc in trace[-TAIL:]]


def concrete_window(netlist, bits, pol):
    """Replay `bits` through the concrete simulator; -> (window bytes, success, msg)."""
    sim = GateSim(netlist, undriven=pol)
    res = sim.run(standard_stimulus(bits, reset_cycles=RESET_CYCLES, tail=TAIL))
    tail = res[-TAIL:]
    return [o for o, _s in tail], max(s for _o, s in res), message_of(res)


def decode(window_bytes):
    return "".join(chr(v) for v in window_bytes if 32 <= v < 127)


def bits_of_model(model, ivars):
    return "".join("1" if model_bit(model, iv) else "0" for iv in ivars)


def window_equals(window, target_bytes):
    """z3 constraints: symbolic window == concrete byte list."""
    cons = []
    for cyc, byte in zip(window, target_bytes):
        for b in range(8):
            want = (byte >> b) & 1
            sym = cyc[b]
            if sym.concrete:
                if sym.v != want:
                    return [z3.BoolVal(False)]
            else:
                cons.append(sym.v if want else z3.Not(sym.v))
    return cons


def window_differs(window, target_bytes):
    """One z3 constraint: symbolic window != concrete byte list."""
    diffs = []
    for cyc, byte in zip(window, target_bytes):
        for b in range(8):
            want = (byte >> b) & 1
            sym = cyc[b]
            if sym.concrete:
                if sym.v != want:
                    return z3.BoolVal(True)
            else:
                diffs.append(z3.Not(sym.v) if want else sym.v)
    return z3.Or(diffs) if diffs else z3.BoolVal(False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netlist", default=str(paths.PUZZLE_OUT / "03_netlist.json"))
    ap.add_argument("--solution", default=str(paths.PUZZLE_OUT / "solution.json"))
    ap.add_argument("--out", default=str(paths.PUZZLE_OUT / "proofs.json"))
    a = ap.parse_args(argv)
    ok = True
    t0 = time.time()

    expected_bits = json.load(open(a.solution))["bits"]

    print("== symbolic unroll (both polarities of the floating net) ==")
    sim = GateSim(a.netlist)
    ivars = [Sym(z3.Bool(f"i{k}")) for k in range(N_BITS)]
    tr0 = unrolled(sim, ivars, undriven=ZERO)
    tr1 = unrolled(sim, ivars, undriven=ONE)
    n_sym = sum(1 for cyc in tr0 for s in cyc.values() if not s.concrete)
    print(f"   {len(tr0)} cycles, {n_sym} symbolic samples  ({time.time()-t0:.1f}s)")

    # ---------------------------------------------------- independence (miter)
    print("\n== success is independent of the floating net ==")
    s = z3.Solver()
    pairs = []
    for c0, c1 in zip(tr0, tr1):
        a0, a1 = c0["success"], c1["success"]
        if a0.concrete and a1.concrete:
            if a0.v != a1.v:
                pairs.append(z3.BoolVal(True))
        else:
            pairs.append(z3.Xor(a0.z3(), a1.z3()))
    s.add(z3.Or(pairs) if pairs else z3.BoolVal(False))
    indep = s.check() == z3.unsat
    ok &= indep
    print(f"   miter over every cycle: {'UNSAT' if indep else 'SAT (!!)'}")
    print(f"   success independent of floating net: {indep}")

    # ---------------------------------------------------------- uniqueness
    print("\n== circuit-level uniqueness of the success input ==")
    succ_any = z3.Or([c["success"].z3() for c in tr0 if not c["success"].concrete])
    s = z3.Solver()
    s.add(succ_any)
    r = s.check()
    assert r == z3.sat, "no input raises success?!"
    witness = bits_of_model(s.model(), ivars)
    match = witness == expected_bits
    ok &= match
    print(f"   SAT witness: {witness}")
    print(f"   witness matches solution.json: {match}")
    wb, wsucc, wmsg = concrete_window(a.netlist, witness, 0)
    print(f"   concrete replay: success={wsucc} message={wmsg!r}")
    ok &= wsucc == 1
    # block the witness: any input differing in at least one bit
    s.add(z3.Or([z3.Not(iv.v) if witness[k] == "1" else iv.v
                 for k, iv in enumerate(ivars)]))
    unique = s.check() == z3.unsat
    ok &= unique
    print(f"   blocking it: {'UNSAT - no second input exists' if unique else 'SAT (!!)'}")
    print(f"   unique: {unique}")

    # ------------------------------------------------- message-set completeness
    print("\n== every message the chip can emit (exhaustive over 2^121 inputs) ==")
    messages = {}
    complete = True
    for pol, tr in ((0, tr0), (1, tr1)):
        win = window_of(tr)
        s = z3.Solver()
        found = []
        while len(found) < MAX_MESSAGES:
            if s.check() == z3.unsat:
                break
            m = s.model()
            bits = bits_of_model(m, ivars)
            wbytes, _succ, _msg = concrete_window(a.netlist, bits, pol)
            # cross-check: the model's own window must equal the concrete replay
            got = [sum(model_bit(m, cyc[b]) << b for b in range(8)) for cyc in win]
            assert got == wbytes, "symbolic/concrete window mismatch - Sym bug"
            found.append((decode(wbytes), wbytes))
            s.add(window_differs(win, wbytes))
        else:
            complete = False
        msgs = sorted({t for t, _w in found})
        messages[pol] = msgs
        exhausted = len(found) < MAX_MESSAGES
        touch = [t for t in msgs if TOUCH_RE.fullmatch(t)]
        good = (exhausted and len(found) == 5 and len(touch) == 1
                and set(msgs) - set(touch) == EXPECTED)
        complete &= good
        print(f"   n293={pol}: {len(found)} windows -> {msgs} "
              f"{'(UNSAT: list is complete)' if exhausted else '(bound hit!)'}")
    ok &= complete
    print(f"   messages proven complete: {complete}")

    # -------------------------------------------------------- trigger exclusivity
    print("\n== BIG BANG / EMPTY SKY trigger exclusivity ==")
    win0 = window_of(tr0)
    exclusive = True
    for label, stim_bits, negate in (("BIG BANG", "1" * N_BITS, True),
                                     ("EMPTY SKY", "0" * N_BITS, False)):
        target, _succ, msg = concrete_window(a.netlist, stim_bits, 0)
        s = z3.Solver()
        s.add(window_equals(win0, target))
        # at least one bit off the trigger pattern
        s.add(z3.Or([z3.Not(iv.v) if negate else iv.v for iv in ivars]))
        u = s.check() == z3.unsat
        exclusive &= u
        print(f"   {msg!r} with any {'zero' if negate else 'one'} bit: "
              f"{'UNSAT - only the exact grid' if u else 'SAT (!!)'}")
    ok &= exclusive
    print(f"   triggers exclusive: {exclusive}")

    json.dump({"unique": unique, "witness": witness,
               "witness_matches_solution": match,
               "messages": {str(k): v for k, v in messages.items()},
               "messages_complete": complete,
               "triggers_exclusive": exclusive,
               "success_independent_of_floating_net": indep,
               "tail_cycles": TAIL},
              open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}  ({time.time()-t0:.1f}s total)")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
