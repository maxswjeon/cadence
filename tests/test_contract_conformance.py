"""Conformance tests for the device↔brain event-envelope contract (`contract/`).

Three claims, each independently checked:

(a) a sample device-agent envelope validates against the versioned JSON Schema;
(b) that same envelope round-trips through the *real* brain (FastAPI ``TestClient`` +
    ``IngestPipeline`` + in-memory D1) — first POST is ``202`` (new), a re-POST is
    ``200`` (dedupe);
(c) the brain's own :class:`~cadence.adapters.base.Event` model serializes to
    something that conforms to the same schema — this is what actually keeps the
    contract honest: if the code and the schema diverge, this test catches it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.app import create_app
from cadence.ingest.pipeline import IngestPipeline

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contract" / "event-envelope.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def validator(schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _sample_envelope(event_id: str = "android-notif-9f2c1") -> dict:
    """A realistic device-agent envelope (Android NotificationListener capture)."""
    return {
        "event_id": event_id,
        "source": "android_notification",
        "account_ref": "device:pixel-8:com.google.android.gm",
        "acquisition_tier": "notification_wal",
        "kind": "device.notification",
        "occurred_at": "2026-07-06T14:32:00Z",
        "device_id": "pixel-8-a1b2",
        "dedupe_id": hashlib.sha256(
            f"android_notification|device:pixel-8:com.google.android.gm|{event_id}".encode()
        ).hexdigest(),
        "payload_hash": hashlib.sha256(b"fixture-raw-payload").hexdigest(),
        "raw_evidence_ref": f"nas://{event_id}",
        "summary": "notification from Gmail",
        "confidence": 0.95,
        "structured": {"app_package": "com.google.android.gm", "actionable": True},
    }


# --------------------------------------------------------------------------- #
# (a) sample envelope validates against the schema
# --------------------------------------------------------------------------- #


def test_schema_is_valid_draft_2020_12(schema: dict) -> None:
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["schema_version"]


def test_sample_envelope_validates(validator: Draft202012Validator) -> None:
    validator.validate(_sample_envelope())


def test_schema_examples_validate(schema: dict, validator: Draft202012Validator) -> None:
    for example in schema.get("examples", []):
        validator.validate(example)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(raw_body="verbatim raw text"),  # extra top-level field
        lambda e: e.update(structured={"message_body": "verbatim raw text"}),  # denylisted key
        lambda e: e.pop("kind"),  # missing required field
        lambda e: e.update(acquisition_tier="not_a_real_tier"),  # bad enum
        lambda e: e.update(payload_hash="not-hex"),  # bad hash shape
    ],
)
def test_boundary_violations_fail_schema_validation(
    validator: Draft202012Validator, mutate
) -> None:
    envelope = _sample_envelope()
    mutate(envelope)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(envelope)


# --------------------------------------------------------------------------- #
# (b) POST to the real brain: 202 new, 200 dedupe on re-POST
# --------------------------------------------------------------------------- #


def test_envelope_round_trips_through_real_brain_with_dedupe(store, settings) -> None:
    # settings fixture already sets require_mtls=False (see tests/conftest.py) — the
    # boundary respected: this exercises the same require_mtls stub as production,
    # just with the dev/test escape hatch that lets TestClient bypass proxy-terminated
    # mTLS, exactly as tests/test_app.py does.
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    envelope = _sample_envelope()

    with TestClient(app) as client:
        r1 = client.post("/ingest/event", json=envelope)
        assert r1.status_code == 202
        body1 = r1.json()
        assert body1["accepted"] is True
        assert body1["duplicate"] is False
        assert body1["fact_id"]
        assert body1["dedupe_id"] == envelope["dedupe_id"]

        r2 = client.post("/ingest/event", json=envelope)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["duplicate"] is True
        assert body2["dedupe_id"] == envelope["dedupe_id"]


def test_device_os_api_tier_envelope_validates_and_ingests(
    validator: Draft202012Validator, store, settings
) -> None:
    """``device_os_api`` covers direct OS/system-API reads (app-usage, active-window,
    telemetry, SMS provider, location) not otherwise categorized — distinct from
    ``notification_wal`` (notifications) and ``scrape_nonroot`` (messenger scrape)."""
    event_id = "windows-appusage-1a2b"
    envelope = _sample_envelope(event_id=event_id)
    envelope.update(
        source="windows_app_usage",
        account_ref="device:thinkpad-x1:desktop",
        acquisition_tier="device_os_api",
        kind="device.app_usage",
        dedupe_id=hashlib.sha256(
            f"windows_app_usage|device:thinkpad-x1:desktop|{event_id}".encode()
        ).hexdigest(),
        summary="active-window usage sample",
        structured={"app_name": "chrome.exe", "foreground_seconds": 42},
    )

    validator.validate(envelope)  # (a) schema-valid

    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    with TestClient(app) as client:
        r = client.post("/ingest/event", json=envelope)  # (b) real brain accepts it
        assert r.status_code == 202
        assert r.json()["accepted"] is True


# --------------------------------------------------------------------------- #
# (c) the brain's own Event model conforms to the contract schema
# --------------------------------------------------------------------------- #


def test_brain_event_model_conforms_to_schema(validator: Draft202012Validator) -> None:
    """Round-trip a fully-populated ``Event`` through the schema.

    This is the test that actually enforces brain↔agent agreement: if a field is
    renamed/added/removed on ``Event`` without updating the schema, this fails — and
    per the contract's ground rule, the fix is to update the schema, not this test.
    """
    event = Event(
        event_id="android-notif-9f2c1",
        source="android_notification",
        account_ref="device:pixel-8:com.google.android.gm",
        acquisition_tier=AcquisitionTier.NOTIFICATION_WAL,
        kind="device.notification",
        occurred_at=datetime(2026, 7, 6, 14, 32, tzinfo=UTC),
        device_id="pixel-8-a1b2",
        payload_hash=hashlib.sha256(b"fixture-raw-payload").hexdigest(),
        raw_evidence_ref="nas://android-notif-9f2c1",
        summary="notification from Gmail",
        confidence=0.95,
        structured={"app_package": "com.google.android.gm", "actionable": True},
    ).with_dedupe_id()

    payload = event.model_dump(mode="json")
    validator.validate(payload)


def test_brain_event_model_minimal_conforms_to_schema(validator: Draft202012Validator) -> None:
    """The minimal-required-fields case (everything else left at its ``Event`` default)."""
    event = Event(
        event_id="e1",
        source="github",
        account_ref="octocat",
        kind="github.issue",
    ).with_dedupe_id()

    payload = event.model_dump(mode="json")
    validator.validate(payload)


def test_brain_event_model_device_os_api_tier_conforms_to_schema(
    validator: Draft202012Validator,
) -> None:
    """The newer ``DEVICE_OS_API`` tier (direct OS/system-API reads) round-trips too."""
    event = Event(
        event_id="windows-appusage-1a2b",
        source="windows_app_usage",
        account_ref="device:thinkpad-x1:desktop",
        acquisition_tier=AcquisitionTier.DEVICE_OS_API,
        kind="device.app_usage",
        summary="active-window usage sample",
        structured={"app_name": "chrome.exe", "foreground_seconds": 42},
    ).with_dedupe_id()

    payload = event.model_dump(mode="json")
    validator.validate(payload)
