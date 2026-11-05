"""Aggregated MirrorHold suite: every test module in a fixed order, one summary.

    python tests/run_all.py

Each module also runs alone (``python tests/test_<unit>.py``). The engine-contract
module needs the pinned engine in ``vendor/``; without it one of its tests fails by
name and the others skip, so a missing engine is never a green run. Exit status 0
only when every module passes.

A failure's text may hold a character the console's code page cannot show (a name in
another script, written to a redirected output): it is written escaped rather than
ending the run.
"""
import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import support  # noqa: E402,F401  (puts the project on the import path)

MODULES = [
    "test_endpoints",
    "test_storage",
    "test_model",
    "test_store",
    "test_exchange",
    "test_survey",
    "test_wording",
    "test_validation",
    "test_engine_adapter",
    "test_fetch_engine",
    "test_release_build",
    "test_cli",
    "test_plan",
    "test_drive_turns",
    "test_drive_marks",
    "test_engine_contract",
    "test_remotes",
    "test_remote_runs",
    "test_remote_maintenance",
    "test_remote_window",
    "test_run",
    "test_live_sources",
    "test_maintenance",
    "test_repair",
    "test_power",
    "test_tray",
    "test_automation",
    "test_installation",
    "test_gui",
]


def run_all() -> bool:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    print("#" * 72)
    print("# MirrorHold aggregated suite")
    print("#" * 72)
    results = []
    for name in MODULES:
        print(f"\n>>> {name} <<<", flush=True)
        started = time.monotonic()
        suite = unittest.defaultTestLoader.loadTestsFromName(name)
        outcome = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
        results.append((name, outcome, time.monotonic() - started))
    print("\n" + "#" * 72)
    print("# SUMMARY")
    print("#" * 72)
    total = failed = skipped = 0
    for name, outcome, seconds in results:
        bad = len(outcome.failures) + len(outcome.errors)
        total += outcome.testsRun
        failed += bad
        skipped += len(outcome.skipped)
        tag = "PASS" if outcome.wasSuccessful() else "FAIL"
        print(f"  [{tag}] {name}: {outcome.testsRun} run, {bad} failed, {len(outcome.skipped)} skipped "
              f"({seconds:.1f} s)")
        for test, reason in outcome.skipped:
            print(f"         skipped {test.id().split('.', 1)[-1]}: {reason}")
    print(f"\n{total} tests, {failed} failed, {skipped} skipped")
    print("#" * 72)
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
