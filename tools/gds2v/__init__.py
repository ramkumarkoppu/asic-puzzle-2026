"""gds2v - reverse-engineer a GDSII layout back to Verilog.

Pipeline (each stage consumes the previous one's output):

    Extraction   GDS geometry + pin labels  ->  cells, nets, capability report
    emit         netlist naming             ->  JSON / structural / behavioural Verilog
    cells        cell-name grammar          ->  boolean & sequential models
    sim          netlist JSON               ->  cycle-accurate simulation (numpy-batched)
    lift         netlist JSON               ->  registers + proven word-level RTL
    schematic    netlist JSON               ->  gate-symbol SVG diagram
    validate     DEF + reference netlist    ->  equivalence-up-to-renaming proof
    techprofile  layer map                  ->  built-in sky130 or auto-detected stack

Run ``python -m gds2v <file.gds> -o <outdir>`` for the full flow; see SOLUTION_WRITEUP.md.
"""
from .extract import Extraction
from .techprofile import TechProfile, SKY130, auto_detect, choose_profile
from . import cells, emit, validate

__all__ = ["Extraction", "TechProfile", "SKY130", "auto_detect", "choose_profile",
           "cells", "emit", "validate"]
__version__ = "0.2.0"
