#!/usr/bin/env bash
#
# Installer. Everything it does lives in ./hermes; this file exists because
# `install.sh` is where people look first, and one implementation with two names
# is better than two implementations that drift.
#
#   ./install.sh
#
# It is safe to run again: nothing is overwritten without being asked.

set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/hermes" install "$@"
