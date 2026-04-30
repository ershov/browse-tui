#!/bin/bash
# Parallel test runner — thin shim over tools/parallel-test.py.
#
# Splits the ``test/`` tree into per-module subprocesses and runs them
# through a process pool. Each TmuxFixture allocates a unique socket
# per instance so the UI layer is safe to fan out.
#
# Usage:
#   ./run-tests-parallel.sh              # default 4 workers
#   ./run-tests-parallel.sh -j 8         # 8 workers
#   JOBS=8 ./run-tests-parallel.sh       # via env var
#   ./run-tests-parallel.sh -v           # verbose (per-module output)
#   ./run-tests-parallel.sh test.unit.test_item   # specific modules
#
# The serial runner (./run-tests.sh) is unchanged for CI / simplicity.
set -ueo pipefail
cd "$(dirname "$(realpath "$0")")"
exec python3 tools/parallel-test.py "$@"
