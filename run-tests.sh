#!/bin/bash
set -ueo pipefail
cd "$(dirname "$(realpath "$0")")"
# Rebuild browse-tui and the browse-agents-* recipes first: the UI suites
# spawn the binary and the recipe suites import the recipes.
./build-all.sh
exec python3 -m unittest discover -t . -s test/
