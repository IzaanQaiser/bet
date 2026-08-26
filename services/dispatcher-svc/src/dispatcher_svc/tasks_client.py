"""dispatcher-svc enqueueing a Cloud Task at itself — a new role for it
(previously only ever a Cloud Tasks *target*: committer-svc's reminder/
next-fit tasks, dashboard-svc's working-hours-change task). Reuses the
same `reminders` queue rather than standing up a new one — it's already
multi-purpose.

Fires POST /latents/{item_id}/fire at exactly `fire_at`, the instant
this item's next_fit_start currently claims. The endpoint itself is
responsible for checking staleness (a later write may have superseded
this exact task by the time it actually fires) — this module doesn't
try to cancel/replace anything, just enqueue.
"""

import json
import os
from datetime import UTC, datetime
from uuid import UUID

from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

TASKS_LOCATION = "us-central1"
TASKS_QUEUE = "reminders"


def enqueue_fire_task(item_id: UUID, fire_at: datetime) -> None:
    project_id = os.environ["GCP_PROJECT_ID"]
    dispatcher_url = os.environ["DISPATCHER_SVC_URL"]
    url = f"{dispatcher_url}/latents/{item_id}/fire"
    dispatcher_sa = f"sa-dispatcher@{project_id}.iam.gserviceaccount.com"
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project_id, TASKS_LOCATION, TASKS_QUEUE)
    schedule_time = timestamp_pb2.Timestamp()
    schedule_time.FromDatetime(fire_at.astimezone(UTC))
    client.create_task(
        parent=parent,
        task={
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"scheduled_for": fire_at.isoformat()}).encode(),
                "oidc_token": {"service_account_email": dispatcher_sa, "audience": url},
            },
            "schedule_time": schedule_time,
        },
    )
