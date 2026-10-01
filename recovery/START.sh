#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Нужен Python 3. В Ubuntu: sudo apt update && sudo apt install python3'
    exit 1
fi
case "${1:-}" in
    --demo|--check|--plan|--help|-h) exec python3 ./wizard.py "$@" ;;
esac
if [[ "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Прошивка запускается только на Ubuntu x86_64. Демо: bash START.sh --demo'
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    printf '%s\n' 'Мастеру нужны права sudo для подготовки ARM rootfs и доступа к USB.'
    exec sudo -- python3 "$PWD/wizard.py" "$@"
fi
exec python3 ./wizard.py "$@"
