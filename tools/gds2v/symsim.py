"""Symbolic simulation of an extracted netlist with z3.

The point of this module is that it adds NO new model of the circuit.  ``GateSim``
already evaluates the netlist through ``CellModel.eval`` parameterised by an ops object
(scalar ints, numpy lanes).  Here the same code path runs a third value type: ``Sym``, a
boolean that is either a concrete 0/1 or a z3 expression.  Whatever the VCD-validated
simulator computes, the symbolic unroll computes identically - so a SAT proof over the
unrolled formula is a proof about the same circuit the simulator was validated on.

``Sym`` constant-folds aggressively: while inputs are concrete (reset cycles, tied
ports) whole cones stay concrete ints, and only logic that actually depends on a
symbolic input grows a z3 term.  An unrolled 121-cycle puzzle run stays small enough
for z3 to answer in seconds.
"""
import z3


class Sym:
    """A boolean: concrete int 0/1, or a z3 BoolRef.  Supports & | ^ like the other ops."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    @property
    def concrete(self):
        return isinstance(self.v, int)

    def z3(self):
        return z3.BoolVal(bool(self.v)) if self.concrete else self.v

    def __and__(self, o):
        if self.concrete:
            return o if self.v else ZERO
        if o.concrete:
            return self if o.v else ZERO
        return Sym(z3.And(self.v, o.v))

    def __or__(self, o):
        if self.concrete:
            return ONE if self.v else o
        if o.concrete:
            return ONE if o.v else self
        return Sym(z3.Or(self.v, o.v))

    def __xor__(self, o):
        if self.concrete and o.concrete:
            return ONE if self.v ^ o.v else ZERO
        if self.concrete:
            return _inv(o) if self.v else o
        if o.concrete:
            return _inv(self) if o.v else self
        return Sym(z3.Xor(self.v, o.v))

    def __repr__(self):
        return f"Sym({self.v})"


ONE, ZERO = Sym(1), Sym(0)


def _inv(s):
    if s.concrete:
        return ZERO if s.v else ONE
    if z3.is_not(s.v):                       # fold double negation
        return Sym(s.v.arg(0))
    return Sym(z3.Not(s.v))


class SymOps:
    """Ops object for CellModel.eval / GateSim._step, mirroring ScalarOps/ArrayOps."""
    ONE, ZERO = ONE, ZERO
    inv = staticmethod(_inv)


def symbolic_run(sim, stimulus, outputs, undriven=ZERO, init=None):
    """Unroll `sim` (a GateSim) over `stimulus`, symbolically.

    stimulus: iterable of {input_port: Sym} dicts, one per clock cycle.
    outputs:  port names to sample after each edge.
    undriven: the Sym driven onto floating nets each cycle (a constant or a free Bool).
    init:     {flop instance name: Sym} overriding that flop's power-up value - e.g.
              free Bools for flops with no reset pin, whose silicon power-up state is
              genuinely unknown.
    -> list of {port: Sym} dicts, one per cycle.

    Uses GateSim._step directly, so cycle semantics (settle, capture with async
    set/reset, settle again) are exactly those of the concrete simulator.
    """
    ops = SymOps
    netv = [ZERO] * sim.n_nets
    state = sim._reset_state(ops)
    if init:
        state.update(init)
    out = []
    for inp in stimulus:
        state = sim._step(netv, state, ops, inp, undriven)
        out.append({o: netv[sim.port[o]] for o in outputs})
    return out


def model_bit(model, sym):
    """Value of a Sym under a z3 model, as int 0/1 (free vars complete to a value)."""
    if sym.concrete:
        return sym.v
    return 1 if z3.is_true(model.eval(sym.v, model_completion=True)) else 0
