package com.cadence.agent.envelope

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * On-the-wire event envelope sent to the brain's `POST /ingest/event`
 * (`contract/event-envelope.schema.json`, `schema_version` "1.0.0"; `contract/protocol.md`).
 *
 * Field-for-field aligned with both the JSON Schema and the brain's pydantic `Event`
 * (`cadence/adapters/base.py`). The schema sets `additionalProperties: false`, so any
 * field NOT listed in `properties` (e.g. a schema-version marker on the envelope itself)
 * is a `422` envelope-shape violation on the wire (protocol.md §2, "Envelope-shape
 * violation") — [SCHEMA_VERSION] below is therefore a Kotlin-only constant, never
 * serialized, mirroring the Rust core's `SCHEMA_VERSION` (`agents/core/src/envelope.rs`),
 * NOT the Windows sibling's `EventEnvelope.SchemaVersion` wire field
 * (`agents/windows/Models/EventEnvelope.cs`), which — as written — would violate this
 * same rule if ever serialized as-is; flagged to team-lead, not fixed here (out of this
 * workstream's directory).
 *
 * `structured` MUST stay raw-boundary clean: the schema's `propertyNames` pattern
 * structurally rejects keys containing `raw`/`body`/`text`/`content`/`message`/`email`/
 * `chat`/`transcript`/`audio`/`voice`/`screenshot`/`screen`/`ocr`/`snippet`/`verbatim`/
 * `attachment`/`photo`/`image`/account-or-secret-shaped words, etc. Verbatim evidence
 * goes to NAS and is referenced only via [rawEvidenceRef] + [payloadHash].
 *
 * `ingestedAt` defaults to `null`/omitted: protocol.md §5 says devices SHOULD omit it and
 * let the brain stamp its own receipt time. (Note: the Rust core's own builder currently
 * always fills `ingested_at` when built — `EventEnvelopeBuilder::build` in
 * `agents/core/src/envelope.rs` — which the field's own doc-comment there doesn't flag as
 * a divergence from protocol.md; if this envelope round-trips through `CoreBridge` once
 * linked, the core's behavior wins over this default.)
 */
@Serializable
data class EventEnvelope(
    @SerialName("event_id") val eventId: String,
    val source: String,
    @SerialName("account_ref") val accountRef: String,
    @SerialName("acquisition_tier") val acquisitionTier: AcquisitionTier = AcquisitionTier.UNKNOWN,
    val kind: String,

    /** UTC ISO-8601. The source event's own time; null/omitted only when genuinely unknown. */
    @SerialName("occurred_at") val occurredAt: String? = null,

    /** UTC ISO-8601. SHOULD stay null/omitted in v1 — see class doc. */
    @SerialName("ingested_at") val ingestedAt: String? = null,

    @SerialName("device_id") val deviceId: String? = null,

    /**
     * Cross-device idempotency key. If set, the brain uses it verbatim
     * (`Event.with_dedupe_id()` only fills an unset id) — see
     * [com.cadence.agent.mapper.EventMapper]'s fallback computation.
     */
    @SerialName("dedupe_id") val dedupeId: String? = null,

    @SerialName("payload_hash") val payloadHash: String? = null,
    @SerialName("raw_evidence_ref") val rawEvidenceRef: String? = null,

    /** Short, NON-verbatim summary — never verbatim message/email/chat/transcript text. */
    val summary: String? = null,

    val confidence: Double? = null,

    val structured: JsonObject = JsonObject(emptyMap()),
) {
    companion object {
        /** Matches `contract/event-envelope.schema.json`'s `schema_version`. NOT a wire field. */
        const val SCHEMA_VERSION: String = "1.0.0"
    }
}
