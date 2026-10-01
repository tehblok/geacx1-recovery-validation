#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Нужен Python 3. В Ubuntu: sudo apt update && sudo apt install python3'
    exit 1
fi
exec python3 ./assemble.py "$@"
