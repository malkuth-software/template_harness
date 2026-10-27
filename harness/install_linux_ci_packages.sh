#!/bin/sh
# Install the Ubuntu 24.04 packages of one CI profile, reusing downloaded
# packages kept in an Actions cache.
#
#   sh harness/install_linux_ci_packages.sh <profile> <archive-directory>
#
# profile: a [linux_packages.<profile>] table of the project file (GUI.Forms:
# toolkit for the native and sanitizer jobs, programs for release.yml);
# harness/project.py prints its packages, so python3 (3.11 or later, for
# tomllib; Ubuntu 24.04 has 3.12) and HARNESS_PROJECT or harness.toml must
# be in place.
# archive-directory: where apt keeps the downloaded .deb files. The workflow
# restores it from an Actions cache before this runs and, on main, saves it
# afterwards; the cache key must hash this file and the project file, so a
# changed package set starts a new cache.
#
# Why: the runners' Ubuntu mirror (azure.archive.ubuntu.com) sometimes serves
# at 40-260 kB/s, and the 30 MB of Ubuntu packages then took 7-11 minutes
# (run 39806663239). apt.llvm.org stays fast, and its 140 MB of LLVM packages
# are left out of the kept archives so the cache stays small. apt still reads
# the current package lists: a package whose version moved on is downloaded
# again, and everything installed is exactly what apt would install without
# the cache.
set -eu

profile=$1
archives=$2

# The package set lives in the project file; an unknown profile exits 2.
packages=$(python3 "$(dirname "$0")/project.py" linux-packages "$profile")

# Ubuntu 24.04's default Clang 18 rejects the existing in-class friend
# exception specifications. Use the LLVM 22 release series.
curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key -o "${RUNNER_TEMP:-/tmp}/llvm-key.asc"
sudo install -m 0644 "${RUNNER_TEMP:-/tmp}/llvm-key.asc" /usr/share/keyrings/apt-llvm.asc
echo 'deb [signed-by=/usr/share/keyrings/apt-llvm.asc] https://apt.llvm.org/noble/ llvm-toolchain-noble-22 main' \
  | sudo tee /etc/apt/sources.list.d/llvm-22.list
sudo apt-get update

mkdir -p "$archives/partial"
kept_before=$(find "$archives" -maxdepth 1 -name '*.deb' | wc -l)
started=$(date +%s)
# shellcheck disable=SC2086 # the package list is split into words on purpose
sudo apt-get install -y -o Dir::Cache::Archives="$archives" $packages
finished=$(date +%s)
echo "installed the $profile packages in $((finished - started)) s; $kept_before kept packages were available"

# apt.llvm.org versions carry "~++" (snapshot builds); those downloads are
# fast and large, so they are not kept. The rest are made readable by the
# runner user, which saves the cache.
sudo find "$archives" -maxdepth 1 -name '*~++*.deb' -delete
sudo rm -f "$archives/lock"
sudo chown -R "$(id -u):$(id -g)" "$archives"
echo "$(find "$archives" -maxdepth 1 -name '*.deb' | wc -l) packages kept for the next run"

echo /usr/lib/llvm-22/bin >> "$GITHUB_PATH"
