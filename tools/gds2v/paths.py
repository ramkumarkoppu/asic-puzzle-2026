"""One source of truth for every file-system path the tools use.

Paths are computed from this module's location, not from the current working
directory, so scripts and tests resolve the same files no matter where they run from
or how deeply they are nested.

Layout:
    <repo>/                     REPO_ROOT   (holds the puzzle input data)
      puzzle.gds                PUZZLE_GDS
      example_inputs.vcd        EXAMPLE_VCD
      warmup/                   WARMUP_DIR  (the ground-truth ladder)
      tools/                    TOOLS_DIR
        gds2v/                  (this package)
        puzzle/                 (the solver application)
        out/                    OUT_DIR     (all generated output; gitignored)
"""
from pathlib import Path

GDS2V_DIR = Path(__file__).resolve().parent
TOOLS_DIR = GDS2V_DIR.parent
REPO_ROOT = TOOLS_DIR.parent

# puzzle input data (tracked, upstream)
PUZZLE_GDS = REPO_ROOT / "puzzle.gds"
EXAMPLE_VCD = REPO_ROOT / "example_inputs.vcd"
WARMUP_DIR = REPO_ROOT / "warmup"
WARMUP_GDS = WARMUP_DIR / "04_final.gds"
WARMUP_DEF = WARMUP_DIR / "03_post_place_and_route.def"
WARMUP_NETLIST = WARMUP_DIR / "01_netlist.v"

# generated output (gitignored)
OUT_DIR = TOOLS_DIR / "out"
WARMUP_OUT = OUT_DIR / "warmup"
PUZZLE_OUT = OUT_DIR / "puzzle"
GEN_OUT = OUT_DIR / "gen"                       # synthetic test GDS
OPENSOURCE_OUT = OUT_DIR / "opensource" / "caravel"
FIGURES_OUT = OUT_DIR / "figures"
