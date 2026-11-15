#!/bin/sh
# One command to a harnessed GUI.Forms app (harness/new_app.py explains the steps):
#
#   sh new_app.sh --name APP --workspace DIR [--title TITLE] [--dry-run]
#                 [--create-repo] [--remote-base URL] [--no-restore] [--smoke]
#
# Finds a Python of 3.11 or later (tomllib): $HARNESS_PYTHON when set, else
# python3, python or py -3 on PATH.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
new_app="$here/harness/new_app.py"

usable() {
    "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -n "${HARNESS_PYTHON:-}" ]; then
    exec "$HARNESS_PYTHON" -B "$new_app" "$@"
elif usable python3; then
    exec python3 -B "$new_app" "$@"
elif usable python; then
    exec python -B "$new_app" "$@"
elif usable py -3; then
    exec py -3 -B "$new_app" "$@"
fi
echo "new_app: no Python 3.11 or later found; set HARNESS_PYTHON to one" >&2
exit 1
