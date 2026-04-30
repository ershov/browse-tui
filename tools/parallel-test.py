#!/usr/bin/env python3
"""Run unittest modules in parallel; aggregate results.

Splits the ``test/`` tree into one subprocess per ``test_*.py`` module
and dispatches them to a process pool. Each subprocess invokes
``python3 -m unittest <module>``, captures combined stdout+stderr, and
reports pass/fail. The aggregate exit code is non-zero if any module
failed.

Why per-module rather than per-test? Module-level fan-out keeps the
parallel boundary aligned with the natural test isolation: each module
already imports its own state, the tmux-fixture sockets are unique
per fixture instance, and test counts per module are well balanced.

Usage:

    python3 tools/parallel-test.py              # all modules, J=4
    python3 tools/parallel-test.py --jobs 8     # 8 workers
    python3 tools/parallel-test.py test.unit.test_item   # explicit modules

The shell wrapper ``run-tests-parallel.sh`` simply ``exec``s this script.
"""

import argparse
import concurrent.futures as cf
import glob
import os
import subprocess
import sys
import time


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def discover_modules() -> list[str]:
    """Find every ``test/*/test_*.py`` and return as dotted module paths.

    Sorted alphabetically — deterministic order makes failure logs and
    timing comparisons easier to diff across runs.
    """
    root = _repo_root()
    out = []
    for path in sorted(glob.glob(os.path.join(root, 'test', '*', 'test_*.py'))):
        rel = os.path.relpath(path, root)              # test/unit/test_item.py
        mod = rel[:-len('.py')].replace(os.sep, '.')   # test.unit.test_item
        out.append(mod)
    return out


def run_module(mod: str) -> tuple[str, int, str, float]:
    """Run a single unittest module in a subprocess; return diagnostics."""
    start = time.time()
    proc = subprocess.run(
        [sys.executable, '-m', 'unittest', mod],
        capture_output=True, text=True,
        cwd=_repo_root(),
    )
    elapsed = time.time() - start
    return mod, proc.returncode, proc.stdout + proc.stderr, elapsed


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n', 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--jobs', '-j', type=int,
                    default=int(os.environ.get('JOBS', '4')),
                    help='Number of parallel workers (default 4 or $JOBS).')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='Print per-module output even on success.')
    ap.add_argument('modules', nargs='*',
                    help='Specific modules to run (default: discover all).')
    args = ap.parse_args()

    modules = args.modules or discover_modules()
    if not modules:
        print('no test modules found', file=sys.stderr)
        return 1

    failures: list[tuple[str, str]] = []
    timings: list[tuple[str, float]] = []
    overall_start = time.time()

    # ProcessPoolExecutor — robust subprocess fan-out, easy to extend
    # (e.g. retry, sharding, JSON output) without reaching for shell.
    with cf.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        # Submit all up front so the pool can keep workers saturated.
        futures = {ex.submit(run_module, m): m for m in modules}
        for fut in cf.as_completed(futures):
            mod, rc, out, elapsed = fut.result()
            timings.append((mod, elapsed))
            tag = 'PASS' if rc == 0 else 'FAIL'
            print(f'[{tag}] {mod} ({elapsed:.2f}s)')
            if args.verbose or rc != 0:
                # Indent the captured output for legibility under the tag line.
                indent = '    '
                print(indent + out.replace('\n', '\n' + indent).rstrip())
            if rc != 0:
                failures.append((mod, out))

    overall = time.time() - overall_start
    n = len(modules)
    print()
    print(f'Ran {n} module{"" if n == 1 else "s"} in {overall:.2f}s '
          f'with {args.jobs} worker{"" if args.jobs == 1 else "s"}.')

    if failures:
        print(f'\n{len(failures)} module(s) failed:')
        for mod, out in failures:
            print(f'\n=== {mod} ===')
            print(out)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
