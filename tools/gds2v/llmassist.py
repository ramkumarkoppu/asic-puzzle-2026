"""LLM-assisted cell-function proposals for unknown standard-cell libraries.

FALLBACK MECHANISM, off by default (``--llm-assist`` on the CLI).  When a GDS uses
a library the naming grammar cannot decode, the extractor normally marks those
cells blackbox and disables simulation/behavioural/lift with a reason.  This
module can instead ask a user-supplied Claude model to propose each unknown
cell's function from its NAME and OBSERVED PINS - foundry naming conventions are
exactly the kind of diffuse knowledge a large model holds.

The design principle is **propose, then verify** - the LLM is a hypothesis
generator, never a source of truth.  A proposal enters the netlist only after
passing deterministic gates:

    * its inputs+outputs must EXACTLY equal the pins observed in the GDS
    * exactly one output, disjoint from the inputs (combinational cells only;
      anything sequential or uncertain must be answered ``known: false``)
    * the boolean expression must parse (~ & | ^ over the declared inputs only)

Verified proposals register as cell models (cells.register_model), so the whole
downstream flow - simulator, behavioural RTL, lift - runs on them unchanged.
Everything they produce is labelled a HYPOTHESIS in the capability report:
pin-set agreement is proof of interface, not of function.

The API call defaults to Anthropic's ``claude-opus-5`` and resolves credentials
the standard way (explicit --llm-api-key, else ANTHROPIC_API_KEY / an `ant auth
login` profile).  Tests inject a fake ``transport`` so no network is involved.
"""
import json
import re


# --------------------------------------------------------------------------- #
#  Boolean-expression parsing: ~ (not) > & (and) > ^ (xor) > | (or), parens.  #
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"\s*(?:(?P<id>[A-Za-z_]\w*)|(?P<op>[~&|^()]))")


def parse_expr(text):
    """Boolean expression -> AST of nested tuples; raises ValueError on junk."""
    tokens = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError(f"bad character at {text[pos:]!r}")
            break
        tokens.append(m.group("id") or m.group("op"))
        pos = m.end()
    if not tokens:
        raise ValueError("empty expression")
    out, k = _parse_or(tokens, 0)
    if k != len(tokens):
        raise ValueError(f"trailing tokens: {tokens[k:]}")
    return out


def _parse_or(t, k):
    node, k = _parse_xor(t, k)
    while k < len(t) and t[k] == "|":
        rhs, k = _parse_xor(t, k + 1)
        node = ("or", node, rhs)
    return node, k


def _parse_xor(t, k):
    node, k = _parse_and(t, k)
    while k < len(t) and t[k] == "^":
        rhs, k = _parse_and(t, k + 1)
        node = ("xor", node, rhs)
    return node, k


def _parse_and(t, k):
    node, k = _parse_unary(t, k)
    while k < len(t) and t[k] == "&":
        rhs, k = _parse_unary(t, k + 1)
        node = ("and", node, rhs)
    return node, k


def _parse_unary(t, k):
    if k >= len(t):
        raise ValueError("unexpected end of expression")
    if t[k] == "~":
        node, k = _parse_unary(t, k + 1)
        return ("not", node), k
    if t[k] == "(":
        node, k = _parse_or(t, k + 1)
        if k >= len(t) or t[k] != ")":
            raise ValueError("unbalanced parentheses")
        return node, k + 1
    if t[k] in "&|^)":
        raise ValueError(f"unexpected {t[k]!r}")
    return ("var", t[k]), k + 1


def expr_vars(ast):
    if ast[0] == "var":
        return {ast[1]}
    return set().union(*(expr_vars(a) for a in ast[1:]))


def expr_to_verilog(ast, name=None):
    name = name or (lambda p: p)
    op, args = ast[0], ast[1:]
    if op == "var":
        return name(args[0])
    if op == "not":
        return f"~({expr_to_verilog(args[0], name)})"
    sym = {"and": "&", "or": "|", "xor": "^"}[op]
    return f"({expr_to_verilog(args[0], name)} {sym} {expr_to_verilog(args[1], name)})"


class ExprModel:
    """A cell model built from a verified expression - same interface as CellModel."""

    kind = "PROPOSED"

    def __init__(self, cell, inputs, output, ast):
        self.cell = cell
        self.base = cell
        self.inputs = list(inputs)
        self.outputs = [output]
        self.ast = ast
        self.detail = {"source": "llm-assist (verified pin set; function is a hypothesis)"}

    def eval(self, vals, ops=None):
        def ev(a):
            if a[0] == "var":
                return vals[a[1]]
            if a[0] == "not":
                return ops.inv(ev(a[1]))
            if a[0] == "and":
                return ev(a[1]) & ev(a[2])
            if a[0] == "or":
                return ev(a[1]) | ev(a[2])
            return ev(a[1]) ^ ev(a[2])
        return {self.outputs[0]: ev(self.ast)}

    def expr(self, name=None):
        return expr_to_verilog(self.ast, name)

    def verilog(self):
        supplies = ["VGND", "VNB", "VPB", "VPWR"]
        ports = self.outputs + self.inputs + supplies
        return (f"module {self.cell} ({', '.join(ports)});\n"
                f"  // llm-assist hypothesis: pin set verified against the GDS,\n"
                f"  // function proposed from the cell name - not proven\n"
                f"  output {', '.join(self.outputs)};\n"
                f"  input {', '.join(self.inputs + supplies)};\n"
                f"  assign {self.outputs[0]} = {self.expr()};\n"
                f"endmodule\n")

    def __repr__(self):
        return f"<{self.cell} PROPOSED in={self.inputs} out={self.outputs}>"


# --------------------------------------------------------------------------- #
#  The propose -> verify -> register pipeline.                                #
# --------------------------------------------------------------------------- #

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cells"],
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "known", "inputs", "outputs", "function"],
                "properties": {
                    "name": {"type": "string"},
                    "known": {
                        "type": "boolean",
                        "description": "false when unsure - the cell stays blackbox",
                    },
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "function": {
                        "type": "string",
                        "description": "boolean expression for the single output "
                                       "using only ~ & | ^ ( ) and input pin names; "
                                       "empty string when known is false",
                    },
                },
            },
        }
    },
}


def unknown_types(instances):
    """{cell name: sorted signal-pin list} for types the grammar cannot decode."""
    from . import cells
    out = {}
    for d in instances:
        name = d["cell"]
        if name in out:
            continue
        try:
            m = cells.parse_cell(name)
            if m.kind != "BLACKBOX":
                continue
        except ValueError:
            pass
        pins = sorted(p for p in d.get("pins", {}) if p not in cells.SUPPLY_PINS)
        out[name] = pins
    return out


def build_prompt(unknown):
    lines = [
        "You are helping reverse-engineer a chip layout.  The standard-cell library",
        "is not one we recognise.  For each cell below you are given its NAME and the",
        "signal PIN NAMES observed in the GDS (ground truth - do not invent pins).",
        "",
        "Infer the library's naming convention and propose each cell's function.",
        "Rules:",
        "  - only simple COMBINATIONAL cells with exactly ONE output pin",
        "  - inputs + outputs must exactly partition the observed pin list",
        "  - function: a boolean expression for the output over the input pins,",
        "    using only ~ (not), & (and), | (or), ^ (xor) and parentheses",
        "  - if a cell looks sequential (flop/latch), complex, or you are not",
        "    confident, answer known=false with empty inputs/outputs/function.",
        "    A wrong guess is far worse than no guess: unverified cells are",
        "    handled safely downstream, wrong functions are not.",
        "",
        "Cells:",
    ]
    for name, pins in sorted(unknown.items()):
        lines.append(f"  {name}: pins {pins}")
    return "\n".join(lines)


def anthropic_transport(model="claude-opus-5", api_key=None, base_url=None):
    """Returns transport(prompt) -> parsed JSON dict, via the Anthropic API.

    Credentials resolve the standard way when --llm-api-key is not given
    (ANTHROPIC_API_KEY, or an `ant auth login` profile).
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "the anthropic SDK is not installed - run "
            "`pip install -e .[llm]` (or `pip install anthropic`)") from e

    kw = {}
    if api_key:
        kw["api_key"] = api_key
    if base_url:
        kw["base_url"] = base_url
    client = anthropic.Anthropic(**kw)

    def transport(prompt):
        params = dict(
            model=model,
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        # On Claude Opus 5 / Fable 5, opt into the server-side refusal fallback
        # (recommended default); harmless to retry without it on other models.
        if model.startswith(("claude-opus-5", "claude-fable-5", "claude-mythos-5")):
            try:
                response = client.beta.messages.create(
                    betas=["server-side-fallback-2026-06-01"],
                    fallbacks=[{"model": "claude-opus-4-8"}], **params)
            except anthropic.BadRequestError:
                response = client.messages.create(**params)
        else:
            response = client.messages.create(**params)
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined the request (stop_reason=refusal)")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    return transport


def verify(proposals, unknown):
    """Gate each proposal deterministically -> (verified {name: ExprModel}, rejects)."""
    verified, rejects = {}, []

    def reject(name, why):
        rejects.append((name, why))

    for cell in proposals.get("cells", []):
        name = cell.get("name")
        if name not in unknown:
            reject(name, "not an unknown cell we asked about")
            continue
        if not cell.get("known"):
            continue                                   # honest "unsure" - not a reject
        ins, outs = list(cell.get("inputs", [])), list(cell.get("outputs", []))
        if len(outs) != 1:
            reject(name, f"{len(outs)} outputs (exactly 1 required)")
            continue
        if set(ins) & set(outs):
            reject(name, "inputs and outputs overlap")
            continue
        if sorted(ins + outs) != unknown[name]:
            reject(name, f"pins {sorted(ins + outs)} != observed {unknown[name]}")
            continue
        try:
            ast = parse_expr(cell.get("function", ""))
        except ValueError as e:
            reject(name, f"function does not parse: {e}")
            continue
        extra = expr_vars(ast) - set(ins)
        if extra:
            reject(name, f"function references non-input pins {sorted(extra)}")
            continue
        verified[name] = ExprModel(name, ins, outs[0], ast)
    return verified, rejects


def assist(instances, transport, say=print):
    """Full pipeline over extraction `instances`.  Returns a result dict.

    Every verified model is registered (cells.register_model) so the rest of the
    flow picks it up; unverified cells stay blackbox.
    """
    from . import cells
    unknown = unknown_types(instances)
    if not unknown:
        return {"unknown": 0, "proposed": 0, "verified": 0,
                "rejected": [], "remaining": []}

    say(f"  llm-assist: asking about {len(unknown)} unknown cell type(s)")
    proposals = transport(build_prompt(unknown))
    verified, rejects = verify(proposals, unknown)
    for m in verified.values():
        cells.register_model(m)

    remaining = sorted(set(unknown) - set(verified))
    say(f"  llm-assist: {len(proposals.get('cells', []))} proposals, "
        f"{len(verified)} verified against observed pin sets, "
        f"{len(rejects)} rejected, {len(remaining)} still blackbox")
    for name, why in rejects:
        say(f"    rejected {name}: {why}")
    return {"unknown": len(unknown), "proposed": len(proposals.get("cells", [])),
            "verified": len(verified), "rejected": rejects, "remaining": remaining}


def report_note(result, model):
    """Capability-report addendum recording provenance honestly."""
    return ("\n== llm-assist (fallback for unknown libraries) ==\n"
            f"  model           : {model}\n"
            f"  unknown types   : {result['unknown']}\n"
            f"  verified        : {result['verified']} "
            "(pin sets match the GDS exactly; functions are HYPOTHESES)\n"
            f"  rejected        : {len(result['rejected'])}\n"
            f"  still blackbox  : {len(result['remaining'])}\n"
            "  note: downstream simulation/RTL built on these models is a\n"
            "  hypothesis - validate against a reference before trusting it.\n")
