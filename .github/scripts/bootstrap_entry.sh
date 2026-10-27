#!/bin/sh
# The bootstrap entry a person uses on this host, for the bootstrap proof:
# bootstrap.ps1 through Windows PowerShell on Windows, bootstrap.sh elsewhere.
#
#   sh .github/scripts/bootstrap_entry.sh [BOOTSTRAP ARGUMENT...]
set -eu

harness=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${RUNNER_OS:-}" = Windows ]; then
    exec powershell.exe -NoProfile -NonInteractive -File "$(cygpath -w "$harness/bootstrap.ps1")" "$@"
fi
exec sh "$harness/bootstrap.sh" "$@"
