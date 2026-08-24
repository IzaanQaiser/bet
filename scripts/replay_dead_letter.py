"""Manual dead-letter replay (step 13, state-machine.md §3: "Replay is
manual, not automatic"). Reads one dead_letters row, republishes its
stored payload byte-for-byte to the topic matching `stage` — re-entering
the pipeline at the stage it failed on, not from RECEIVED. Does not
delete or mark the dead_letters row; it stays as the audit record either
way, the operator judges success from what happens next.

Usage: DB_USER=<iam-user> GCP_PROJECT_ID=obligation-engine-hack \
       uv run --with google-cloud-pubsub python scripts/replay_dead_letter.py <dead_letter_id>

Needs a Cloud SQL Auth Proxy running locally (same DB_HOST/DB_PORT
convention as every other script/test in this repo) and application
default credentials with publish rights on the target topic.
"""

import json
import os
import sys

from google.cloud import pubsub_v1
from obligation_engine_shared.db import get_connection


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: replay_dead_letter.py <dead_letter_id>", file=sys.stderr)
        raise SystemExit(1)
    dead_letter_id = sys.argv[1]

    with get_connection() as conn:
        row = conn.execute(
            "SELECT item_id, stage, payload, error, retry_count FROM dead_letters WHERE id = %s",
            (dead_letter_id,),
        ).fetchone()
    if row is None:
        print(f"No dead_letters row with id={dead_letter_id}", file=sys.stderr)
        raise SystemExit(1)

    item_id, stage, payload, error, retry_count = row
    print(f"item_id={item_id} stage={stage} retry_count={retry_count}")
    print(f"original error: {error}")

    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(os.environ["GCP_PROJECT_ID"], stage)
    data = json.dumps(payload).encode("utf-8")
    message_id = publisher.publish(topic_path, data).result()

    print(f"republished to {stage}, message_id={message_id}")


if __name__ == "__main__":
    main()
