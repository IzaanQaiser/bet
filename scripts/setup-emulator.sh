#!/usr/bin/env bash
# Creates the Pub/Sub topics against a running emulator, mirroring
# infra/pubsub.tf's topic list. Plain script, not Terraform-against-the-
# emulator, since the emulator is ephemeral per dev session
# (docs/architecture/infrastructure.md §7).
#
# Usage: PUBSUB_EMULATOR_HOST=localhost:8085 GCP_PROJECT_ID=obligation-engine-hack ./scripts/setup-emulator.sh
set -euo pipefail

: "${PUBSUB_EMULATOR_HOST:?Set PUBSUB_EMULATOR_HOST first, e.g. localhost:8085}"
: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID first}"

for topic in items-raw items-extracted items-confirmed items-raw-dlq items-extracted-dlq items-confirmed-dlq; do
    curl -s -X PUT "http://${PUBSUB_EMULATOR_HOST}/v1/projects/${GCP_PROJECT_ID}/topics/${topic}" > /dev/null
    echo "created topic: $topic"
done
echo "Emulator topics ready."
