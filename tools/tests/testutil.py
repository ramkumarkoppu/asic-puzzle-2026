"""Shared bookkeeping for the test suites.

Every test file reports through the same PASS/FAIL format so that
``test_regression.py`` can grep sub-suite results uniformly:

    [PASS] <name>  <detail>
    ...
    <passed>/<total> checks passed
       FAILED: <name>  <detail>

Do not change the output format: the regression harness matches on these strings.
"""


class Checks:
    """Collects named pass/fail results and prints them as they arrive."""

    def __init__(self):
        self.rows = []          # (name, ok, detail)

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
        return bool(ok)

    def summary(self):
        """Print the tally (with failure detail); return True when everything passed."""
        failures = [(n, d) for n, ok, d in self.rows if not ok]
        print(f"\n{len(self.rows) - len(failures)}/{len(self.rows)} checks passed")
        for n, d in failures:
            print(f"   FAILED: {n}" + (f"  {d}" if d else ""))
        return not failures
