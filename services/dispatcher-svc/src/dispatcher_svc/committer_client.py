"""dispatcher-svc's one synchronous call-out to committer-svc (ADR 0009)
— the second asymmetry in this codebase's otherwise Pub/Sub-only
topology (the first being ingest-svc's direct forward to resolver-svc/
dispatcher-svc, services/ingest-svc/src/ingest_svc/main.py). committer-
svc stays the only service that ever calls the Calendar write API;
dispatcher-svc just needs a synchronous result (the real event id) to
persist into latents.placeholder_event_id, which a fire-and-forget
Pub/Sub publish can't give it without inventing a second round trip.

Auth: same google.oauth2.id_token.fetch_id_token + Bearer pattern
ingest-svc's own direct call already uses.
"""

import os
from uuid import UUID

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.id_token import fetch_id_token


def _committer_url() -> str:
    return os.environ["COMMITTER_SVC_URL"]


def upsert_placeholder(
    item_id: UUID,
    user_id: UUID,
    title: str,
    start,
    effort_minutes: int,
    existing_event_id: str | None,
) -> str:
    """Returns the real Calendar event id — always, whether it's the same
    id as before (moved in place) or a fresh one (created, or a
    fallback-create after the old event vanished)."""
    url = f"{_committer_url()}/latents/{item_id}/placeholder"
    id_token = fetch_id_token(GoogleAuthRequest(), url)
    response = requests.put(
        url,
        json={
            "user_id": str(user_id),
            "title": title,
            "start": start.isoformat(),
            "effort_minutes": effort_minutes,
            "existing_event_id": existing_event_id,
        },
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["event_id"]


def delete_placeholder(item_id: UUID, user_id: UUID, event_id: str) -> None:
    url = f"{_committer_url()}/latents/{item_id}/placeholder"
    id_token = fetch_id_token(GoogleAuthRequest(), url)
    response = requests.delete(
        url,
        params={"user_id": str(user_id), "event_id": event_id},
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=30,
    )
    response.raise_for_status()
