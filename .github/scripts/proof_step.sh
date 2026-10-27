#!/bin/sh
# One timed step of the bootstrap proof (.github/workflows/bootstrap.yml):
#
#   sh .github/scripts/proof_step.sh NAME COMMAND [ARGUMENT...]
#
# Runs COMMAND with its output shown as it comes (so the job log's
# timestamps say where a slow step spent its time) and kept in
# $PROOF_LOGS/NAME.log, adds the row "| NAME | seconds | exit status |" to the
# job summary, and exits with COMMAND's status. The workflow's checks read
# the log afterwards.
set -eu

name=$1
shift
mkdir -p "$PROOF_LOGS"
log="$PROOF_LOGS/$name.log"
status_file="$PROOF_LOGS/$name.status"
start=$(date +%s)
# The group runs in the pipeline's subshell; its status reaches this shell
# through the status file, since POSIX sh has no pipefail.
{
    status=0
    "$@" 2>&1 || status=$?
    echo "$status" > "$status_file"
} | tee "$log"
finish=$(date +%s)
status=$(cat "$status_file")
echo "| $name | $((finish - start)) s | $status |" >> "$GITHUB_STEP_SUMMARY"
exit "$status"
