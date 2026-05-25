#!/bin/bash
set -ueo pipefail
cd "$(dirname "$(realpath "$0")")"
# Rebuild the concatenated browse-tui binary before testing — the UI
# integration tests in test/ui/ spawn the binary directly, so a stale
# build would test the wrong thing. The unit / async suites load each
# src-tui/*.py file separately and don't need the rebuild, but it's
# cheap enough to always run.
./build-tui.sh
exec python3 -m unittest discover -t . -s test/
