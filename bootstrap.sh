#!/bin/sh
# Set up a workspace for one product (harness/bootstrap.py explains the steps):
#
#   sh bootstrap.sh --workspace DIR --project NAME [--in-house] [--dry-run]
#                   [--platform P] [--no-restore] [--no-smoke]
#
# Finds a Python of 3.11 or later (tomllib): $HARNESS_PYTHON when set, else
# python3, python or py -3 on PATH. The Windows toolchain restore needs 3.14.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
bootstrap="$here/harness/bootstrap.py"

usable() {
    "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -n "${HARNESS_PYTHON:-}" ]; then
    exec "$HARNESS_PYTHON" -B "$bootstrap" "$@"
elif usable python3; then
    exec python3 -B "$bootstrap" "$@"
elif usable python; then
    exec python -B "$bootstrap" "$@"
elif usable py -3; then
    exec py -3 -B "$bootstrap" "$@"
fi
echo "bootstrap: no Python 3.11 or later found; set HARNESS_PYTHON to one" >&2
exit 1
