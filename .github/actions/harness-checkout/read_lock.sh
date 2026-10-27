#!/bin/sh
# Print a harness.lock's repository and commit as two lines, key=value, for
# GITHUB_OUTPUT; exit 1 with a message when either is missing, repeated or
# malformed. harness/lock.py is the full reader; this one runs before the
# harness is checked out, so it reads only the two lines it needs.
#
#   sh read_lock.sh <product checkout>/harness.lock
set -eu

lock=$1
if [ ! -f "$lock" ]; then
    echo "harness-checkout: no $lock" >&2
    exit 1
fi
repositories=$(sed -n 's/^repository[[:space:]]*=[[:space:]]*"\([A-Za-z0-9_.-]*\/[A-Za-z0-9_.-]*\)".*$/\1/p' "$lock")
commits=$(sed -n 's/^commit[[:space:]]*=[[:space:]]*"\([0-9a-f]\{40\}\)".*$/\1/p' "$lock")
if [ -z "$repositories" ] || [ "$(printf '%s\n' "$repositories" | wc -l)" -ne 1 ]; then
    echo "harness-checkout: $lock needs exactly one line repository = \"owner/name\"" >&2
    exit 1
fi
if [ -z "$commits" ] || [ "$(printf '%s\n' "$commits" | wc -l)" -ne 1 ]; then
    echo "harness-checkout: $lock needs exactly one line commit = \"<40 lowercase hexadecimal characters>\"" >&2
    exit 1
fi
echo "repository=$repositories"
echo "commit=$commits"
