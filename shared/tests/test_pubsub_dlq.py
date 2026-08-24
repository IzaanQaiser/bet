"""docs/engineering/test-plan.md step 13 — decode_dead_letter_envelope,
the shape confirmed empirically against real Pub/Sub (not the emulator,
which doesn't implement dead-letter forwarding at all — see
docs/product/status.md's step 13 notes for the probe results)."""

import base64
import json

from obligation_engine_shared.pubsub import decode_dead_letter_envelope


def _envelope(payload: dict, delivery_count: str | None = "5") -> dict:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    attributes = {}
    if delivery_count is not None:
        attributes["CloudPubSubDeadLetterSourceDeliveryCount"] = delivery_count
    return {"message": {"data": data, "attributes": attributes, "messageId": "1"}}


def test_decodes_payload_and_retry_count():
    payload = {"item_id": "abc-123", "text": "hi"}
    decoded_payload, retry_count = decode_dead_letter_envelope(_envelope(payload))
    assert decoded_payload == payload
    assert retry_count == 5


def test_missing_delivery_count_attribute_defaults_to_zero():
    payload = {"item_id": "abc-123"}
    envelope = _envelope(payload, delivery_count=None)
    _decoded_payload, retry_count = decode_dead_letter_envelope(envelope)
    assert retry_count == 0
