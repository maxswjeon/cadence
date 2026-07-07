using System.Text.Json.Serialization;

namespace Cadence.WindowsAgent.Models;

/// <summary>
/// The on-the-wire event envelope this agent hands to <c>CoreInterop</c> (which owns WAL,
/// dedupe, and mTLS transport — see ../Interop/CoreInterop.cs).
///
/// Reconciled against the real <c>contract/event-envelope.schema.json</c> (W1, landed after
/// this file was first written) and the brain's <c>cadence.adapters.base.Event</c> (pydantic
/// model, cadence/adapters/base.py). The schema sets
/// <c>additionalProperties: false</c> (mirroring <c>Event.model_config["extra"] = "forbid"</c>),
/// so an unrecognized property is a hard `422` envelope-shape rejection
/// (<c>contract/protocol.md</c> §2) — every <c>[JsonPropertyName]</c> below is the exact,
/// case-sensitive wire name; there is no reliance on a global naming-policy guess.
///
/// Fixed on reconciliation (flagged by m2-3's cross-review, see team-lead):
/// 1. This type previously had a serialized <c>SchemaVersion</c> wire property — the schema
///    has no such per-envelope field (only a top-level <c>$comment</c>-adjacent
///    <c>schema_version</c> on the schema document itself), so it would 422. It is now the
///    non-serialized <see cref="SchemaVersion"/> `const` below (a compile-time constant is
///    never emitted by the JSON serializer), mirroring the Android sibling's
///    <c>EventEnvelope.Companion.SCHEMA_VERSION</c> (Kotlin) and the Rust core's
///    <c>SCHEMA_VERSION</c> (<c>agents/core/src/envelope.rs</c>).
/// 2. <c>ingested_at</c> is one field the schema types as bare <c>"string"</c> — NOT
///    <c>["string","null"]</c> like every other optional field here — so sending a literal
///    JSON `null` for it is itself a schema-type violation, not just noise;
///    <c>contract/protocol.md</c> §5 spells this out: "omit the field entirely instead of
///    sending null, since the underlying type is non-nullable." <see cref="IngestedAt"/> is
///    therefore nullable, defaults to `null` (§5: devices SHOULD omit it and let the brain
///    stamp receipt time), and carries <c>JsonIgnoreCondition.WhenWritingNull</c> so a null
///    value is dropped from the JSON rather than written out as `"ingested_at": null`.
///
/// Raw-boundary invariant (Decision F, consensus plan): this type must NEVER carry verbatim
/// raw content (no window screenshots, no OCR text bodies, no raw audio). Only
/// <see cref="RawEvidenceRef"/> (an opaque NAS blob id/hash) and a non-verbatim
/// <see cref="Summary"/> may stand in for raw evidence.
/// </summary>
public sealed record EventEnvelope
{
    /// <summary>
    /// Matches <c>contract/event-envelope.schema.json</c>'s top-level <c>schema_version</c>
    /// ("1.0.0"). Deliberately NOT an instance property — the schema has no per-envelope
    /// `schema_version` field (`additionalProperties: false` would 422 it); this exists only
    /// for this agent's own internal bookkeeping/logging.
    /// </summary>
    public const string SchemaVersion = "1.0.0";

    /// <summary>Stable id of the source event (opaque, agent-assigned).</summary>
    [JsonPropertyName("event_id")]
    public required string EventId { get; init; }

    /// <summary>Provider name. Per-signal, e.g. "windows_active_window" — see EventMapper's
    /// Source* constants (the schema's own examples name "android_notification"/
    /// "windows_active_window" as the intended per-signal shape, not one generic per-agent name).</summary>
    [JsonPropertyName("source")]
    public required string Source { get; init; }

    /// <summary>Opaque per-account/per-device reference — never a credential.</summary>
    [JsonPropertyName("account_ref")]
    public required string AccountRef { get; init; }

    [JsonPropertyName("acquisition_tier")]
    public required AcquisitionTier AcquisitionTier { get; init; }

    /// <summary>Event kind, e.g. "device.active_window", "device.app_usage",
    /// "device.meeting_detected", "device.screen_context" — the schema's own examples use a
    /// platform-agnostic "device.*" shape for kind (platform lives in Source instead).</summary>
    [JsonPropertyName("kind")]
    public required string Kind { get; init; }

    [JsonPropertyName("occurred_at")]
    public DateTimeOffset? OccurredAt { get; init; }

    /// <summary>SHOULD stay null/omitted (contract/protocol.md §5) — the brain stamps its own
    /// receipt time. See the class doc's fixed-bug note #2: this field's schema type is bare
    /// "string" (not nullable), so `null` must never actually hit the wire; the
    /// WhenWritingNull condition drops it instead of emitting `"ingested_at": null`.</summary>
    [JsonPropertyName("ingested_at")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public DateTimeOffset? IngestedAt { get; init; }

    /// <summary>Originating device id (dedupe input) — a stable, non-PII machine identifier.</summary>
    [JsonPropertyName("device_id")]
    public string? DeviceId { get; init; }

    /// <summary>Cross-device idempotency key. Populated by <c>CoreInterop</c> / the Rust core
    /// (content hash), matching the brain's <c>Event.with_dedupe_id()</c> derivation
    /// (sha256 of source|account_ref|event_id) unless the core computes its own basis — see
    /// agents/core (W2, not yet landed at skeleton time).</summary>
    [JsonPropertyName("dedupe_id")]
    public string? DedupeId { get; init; }

    /// <summary>SHA-256 hex digest of the raw payload, if any raw evidence exists for this
    /// event. Schema pattern `^[0-9a-fA-F]{64}$` when set.</summary>
    [JsonPropertyName("payload_hash")]
    public string? PayloadHash { get; init; }

    /// <summary>NAS blob id/hash for raw evidence (e.g. a captured screen frame). Never the bytes themselves.</summary>
    [JsonPropertyName("raw_evidence_ref")]
    public string? RawEvidenceRef { get; init; }

    /// <summary>Short NON-verbatim summary (e.g. "foreground app changed" — never a window title's full text if that text could be sensitive; collectors decide field-by-field what belongs in Structured vs. Summary).</summary>
    [JsonPropertyName("summary")]
    public string? Summary { get; init; }

    [JsonPropertyName("confidence")]
    public double? Confidence { get; init; }

    /// <summary>Normalized structured fields destined for D1 rows. Must stay raw-boundary clean
    /// (Decision F): no verbatim window/document text, no raw OCR, no raw screenshot bytes.
    /// The schema's `propertyNames` pattern structurally rejects raw-content-shaped keys
    /// (raw/body/text/content/message/email/chat/transcript/audio/voice/screenshot/screen/ocr/
    /// snippet/verbatim/attachment/photo/image/account-or-secret-shaped words) — EventMapper's
    /// keys are checked against this by inspection since there is no local schema validator
    /// wired into this C# skeleton.</summary>
    [JsonPropertyName("structured")]
    public IReadOnlyDictionary<string, object?> Structured { get; init; } =
        new Dictionary<string, object?>();
}
