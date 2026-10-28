#!/bin/bash

set -ueo pipefail

cd "$(dirname "$(realpath "$0")")"
./build-tui.sh
./build-agents.sh
