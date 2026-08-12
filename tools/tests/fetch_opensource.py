"""Fetch an open-source sky130 ASIC with published ground truth, for test_opensource.py.

  python fetch_opensource.py

Downloads Efabless caravel_user_project's `user_proj_example` (GDS + DEF + gate-level
netlist + RTL) into out/opensource/caravel/.  ~160 MB total, one time.  These files are
NOT committed (out/ is gitignored); they are third-party test fixtures.
"""
import os
import sys
import urllib.request

from gds2v import paths
DEST = str(paths.OPENSOURCE_OUT)
BASE = "https://raw.githubusercontent.com/efabless/caravel_user_project/main"
FILES = [
    ("gds/user_proj_example.gds", "user_proj_example.gds"),
    ("def/user_proj_example.def", "user_proj_example.def"),
    ("verilog/gl/user_proj_example.v", "user_proj_example.gl.v"),
    ("verilog/rtl/user_proj_example.v", "user_proj_example.rtl.v"),
]


def main():
    os.makedirs(DEST, exist_ok=True)
    for rel, name in FILES:
        out = os.path.join(DEST, name)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            print(f"have {name} ({os.path.getsize(out):,} bytes)")
            continue
        url = f"{BASE}/{rel}"
        print(f"fetching {url} ...")
        urllib.request.urlretrieve(url, out)
        print(f"  -> {name} ({os.path.getsize(out):,} bytes)")
    print("\ndone. run:  python test_opensource.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
