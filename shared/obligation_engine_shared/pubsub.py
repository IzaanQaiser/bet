"""Pub/Sub publish + push-envelope decode helpers. Every message on every
topic is one of the Pydantic models in schemas.py — publishers and
subscribers both import the same model, so the contract can't drift between
services (docs/engineering/conventions.md)."""

import base64
import json
import os

from google.cloud import pubsub_v1
from pydantic import BaseModel

_publisher: pubsub_v1.PublisherClient | None = None


def _client() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def topic_path(topic_name: str) -> str:
    project_id = os.environ["GCP_PROJECT_ID"]
    return _client().topic_path(project_id, topic_name)


def publish(topic_name: str, message: BaseModel) -> str:
    """Publish a validated Pydantic message as JSON. Returns the Pub/Sub message ID."""
    data = message.model_dump_json().encode("utf-8")
    future = _client().publish(topic_path(topic_name), data)
    return future.result()


def decode_push_envelope[T: BaseModel](envelope: dict, model: type[T]) -> T:
    """Decode a Pub/Sub push subscription's HTTP request body into a validated message."""
    data = base64.b64decode(envelope["message"]["data"])
    return model.model_validate_json(data)


def decode_dead_letter_envelope(envelope: dict) -> tuple[dict, int]:
    """Decode a push envelope forwarded by a dead-letter policy (step 13,
    state-machine.md §3) — the payload is one of RawItemMessage /
    ExtractedItemMessage / ConfirmedItemMessage depending on which topic it
    dead-lettered from, so it's returned as a raw dict rather than validated
    against one fixed model. Returns (payload, retry_count).

    Verified empirically against real Pub/Sub (not the emulator — confirmed
    separately that it doesn't implement push redelivery/dead-lettering at
    all): a dead-lettered message's `data` is the original payload bytes
    unchanged, and Pub/Sub attaches
    `CloudPubSubDeadLetterSourceDeliveryCount` as a string attribute — the
    real number of delivery attempts the source subscription made."""
    message = envelope["message"]
    payload = json.loads(base64.b64decode(message["data"]))
    attributes = message.get("attributes", {})
    retry_count = int(attributes.get("CloudPubSubDeadLetterSourceDeliveryCount", 0))
    return payload, retry_count
