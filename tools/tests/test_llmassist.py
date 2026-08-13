"""LLM-assist fallback: the propose -> verify -> register pipeline, no network.

  python tests\\test_llmassist.py

The LLM is a hypothesis generator behind a deterministic gate, so the gate is
what needs testing - a fake transport stands in for the API and returns a mix
of good, honest-unsure, and adversarially wrong proposals.  The gate must admit
exactly the verifiable ones and reject the rest with reasons.
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
from testutil import Checks

from gds2v import cells, llmassist
from gds2v.cells import ScalarOps
from gds2v.llmassist import parse_expr, expr_vars


def fake_instances():
    """Extraction-shaped instances for an unrecognised library."""
    mk = lambda cell, pins: {"cell": cell,
                             "pins": {p: i for i, p in enumerate(pins)} |
                                     {"VPWR": 90, "VGND": 91}}
    return [
        mk("FOO_ND2X1", ["A", "B", "Y"]),      # nand - good proposal
        mk("FOO_AO21X1", ["A", "B", "C", "X"]),  # and-or - good proposal
        mk("FOO_DFFX1", ["CK", "D", "Q"]),     # flop - model must say known=false
        mk("FOO_BADPIN", ["A", "B", "Y"]),     # proposal invents a pin -> reject
        mk("FOO_BADEXPR", ["A", "Y"]),         # proposal expr doesn't parse -> reject
        mk("FOO_TWOOUT", ["A", "Y", "Z"]),     # proposal claims 2 outputs -> reject
    ]


FAKE_PROPOSALS = {"cells": [
    {"name": "FOO_ND2X1", "known": True,
     "inputs": ["A", "B"], "outputs": ["Y"], "function": "~(A & B)"},
    {"name": "FOO_AO21X1", "known": True,
     "inputs": ["A", "B", "C"], "outputs": ["X"], "function": "(A & B) | C"},
    {"name": "FOO_DFFX1", "known": False,
     "inputs": [], "outputs": [], "function": ""},
    {"name": "FOO_BADPIN", "known": True,
     "inputs": ["A", "Q"], "outputs": ["Y"], "function": "A & Q"},
    {"name": "FOO_BADEXPR", "known": True,
     "inputs": ["A"], "outputs": ["Y"], "function": "A + 1"},
    {"name": "FOO_TWOOUT", "known": True,
     "inputs": ["A"], "outputs": ["Y", "Z"], "function": "A"},
]}


def main():
    c = Checks()

    # ------------------------------------------------ expression parser
    ast = parse_expr("~A & B | C ^ D")
    c.check("parser: precedence ~ > & > ^ > |",
            ast[0] == "or" and ast[1][0] == "and" and ast[2][0] == "xor")
    c.check("parser: variables collected", expr_vars(ast) == {"A", "B", "C", "D"})
    for bad in ("A + B", "A &", "(A", "", "A B"):
        try:
            parse_expr(bad)
            ok = False
        except ValueError:
            ok = True
        c.check(f"parser: rejects {bad!r}", ok)

    # ------------------------------------------------ the gate
    instances = fake_instances()
    unknown = llmassist.unknown_types(instances)
    c.check("all 6 fake cells are unknown to the grammar", len(unknown) == 6)
    prompt = llmassist.build_prompt(unknown)
    c.check("prompt carries the observed pins",
            "FOO_ND2X1" in prompt and "'A', 'B', 'Y'" in prompt)

    result = llmassist.assist(instances, lambda p: FAKE_PROPOSALS, say=lambda *a: None)
    c.check("2 proposals verified, 3 rejected, flop honestly unsure",
            result["verified"] == 2 and len(result["rejected"]) == 3
            and "FOO_DFFX1" in result["remaining"])
    reasons = dict(result["rejected"])
    c.check("pin-mismatch proposal rejected for its pins",
            "observed" in reasons.get("FOO_BADPIN", ""))
    c.check("unparseable function rejected",
            "parse" in reasons.get("FOO_BADEXPR", ""))
    c.check("two-output proposal rejected",
            "1 required" in reasons.get("FOO_TWOOUT", ""))

    # ------------------------------------------------ registered models behave
    m = cells.parse_cell("FOO_ND2X1")
    c.check("verified model registered, kind PROPOSED", m.kind == "PROPOSED")
    truth = [m.eval({"A": a, "B": b}, ScalarOps)["Y"]
             for a in (0, 1) for b in (0, 1)]
    c.check("registered NAND evaluates as NAND", truth == [1, 1, 1, 0])
    m2 = cells.parse_cell("FOO_AO21X1")
    truth2 = [m2.eval({"A": a, "B": b, "C": cc}, ScalarOps)["X"]
              for a in (0, 1) for b in (0, 1) for cc in (0, 1)]
    c.check("registered AO21 evaluates as (A&B)|C",
            truth2 == [(a & b) | cc for a in (0, 1) for b in (0, 1) for cc in (0, 1)])
    v = m.verilog()
    c.check("model emits a Verilog module with the hypothesis label",
            "module FOO_ND2X1" in v and "assign Y" in v and "hypothesis" in v)
    c.check("unverified cell still blackbox after assist",
            len(llmassist.unknown_types(instances)) == 4)

    cells._REGISTERED.clear()
    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
