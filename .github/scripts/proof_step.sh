#!/bin/sh
# One timed step of the bootstrap proof (.github/workflows/bootstrap.yml):
#
#   sh .github/scripts/proof_step.sh NAME COMMAND [ARGUMENT...]
#
# Runs COMMAND with its output in $PROOF_LOGS/NAME.log, prints that log, adds
# the row "| NAME | seconds | exit status |" to the job summary, and exits
# with COMMAND's status. The workflow's checks read the log afterwards.
set -eu

name=$1
shift
mkdir -p "$PROOF_LOGS"
log="$PROOF_LOGS/$name.log"
start=$(date +%s)
status=0
"$@" > "$log" 2>&1 || status=$?
finish=$(date +%s)
cat "$log"
echo "| $name | $((finish - start)) s | $status |" >> "$GITHUB_STEP_SUMMARY"
exit "$status"
