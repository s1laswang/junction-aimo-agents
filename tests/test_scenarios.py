import sys
sys.path.insert(0, "scenarios")
import run_all
from _common import table


def test_table():
    out, fails = table([(True, "a", ""), (False, "b", "boom")])
    assert fails == 1 and "FAIL  b" in out and "1/2 passed" in out
    assert callable(run_all.run_all)
