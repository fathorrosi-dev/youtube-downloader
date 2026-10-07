#!/usr/bin/env bash
set -euo pipefail
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$BASE/maintenance.py" setup "$@"
