#!/bin/sh
# Build the pinned BigQuery emulator from source into .emulator/bin (needs Go >= 1.24;
# the Go toolchain fetches 1.25 itself). First build takes a few minutes and several GB of RAM.
set -eu
VERSION="v0.8.1"
here="$(cd "$(dirname "$0")/.." && pwd)"
GOBIN="$here/.emulator/bin" go install "github.com/goccy/bigquery-emulator/cmd/bigquery-emulator@$VERSION"
echo "Installed: $here/.emulator/bin/bigquery-emulator"
echo "Use it with: export KUMO_BQ_EMULATOR_BIN=$here/.emulator/bin/bigquery-emulator"
