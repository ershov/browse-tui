#!/bin/bash
set -ueo pipefail
cd "$(dirname "$(realpath "$0")")"
exec python3 -m unittest discover -t . -s test/
