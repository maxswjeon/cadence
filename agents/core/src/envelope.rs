//! The on-the-wire event envelope.
//!
//! This mirrors the brain's `Event` model (`cadence/adapters/base.py`) **field for
//! field**, because the brain deserializes with `extra="forbid"` — any unknown key on
//! the wire is a `422`. The envelope therefore carries **structured fields, provenance
//! pointers, and a non-verbatim summary only**; verbatim raw bytes never leave the
//! device (they live on the NAS and are referenced by [`EventEnvelope::raw_evidence_ref`]
//! + [`EventEnvelope::payload_hash`]).
//!
//! The device sets [`EventEnvelope::dedupe_id`] to a **content hash of stable fields**
//! so the brain can dedupe on it (`202` new / `200` duplicate). Computing it on-device
//! is what makes retries after a crash idempotent at the brain.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Contract version this envelope shape targets. It is **not** serialized onto the wire
/// (the brain's `Event` forbids extra fields); it exists so agents and the shared
/// `contract/event-envelope.schema.json` can assert they were built against the same
/// revision. See `README.md` for the reconciliation note with workstream W1.
pub const SCHEMA_VERSION: &str = "1.0.0";

/// How a source's data was acquired — drives trust/compliance handling downstream.
///
/// The wire values match the brain's `AcquisitionTier` `StrEnum` exactly.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub enum AcquisitionTier {
    /// First-party API with an OAuth/token grant.
    #[serde(rename = "official_api")]
    OfficialApi,
    /// OAuth-scoped read (e.g. Google Calendar).
    #[serde(rename = "oauth")]
    OAuth,
    /// User-supplied token/session (e.g. Discord).
    #[serde(rename = "user_token")]
    UserToken,
    /// Imported export/log file.
    #[serde(rename = "file_import")]
    FileImport,
    /// Device notification WAL capture.
    #[serde(rename = "notification_wal")]
    NotificationWal,
    /// First-party device OS API (e.g. Android UsageStats, Windows UI Automation).
    #[serde(rename = "device_os_api")]
    DeviceOsApi,
    /// Non-root on-device scrape.
    #[serde(rename = "scrape_nonroot")]
    ScrapeNonroot,
    /// Manually entered.
    #[serde(rename = "manual")]
    Manual,
    /// Unknown / not yet classified.
    #[serde(rename = "unknown")]
    #[default]
    Unknown,
}

/// A normalized, provenance-tagged source event — the single currency flowing from a
/// device agent to the brain over the mTLS ingest stream.
///
/// Serialization intentionally omits `None` optionals so the payload matches the fields
/// a Python `Event` would emit. `structured` is always present (possibly empty).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventEnvelope {
    /// Stable id of the source event (opaque to the brain).
    pub event_id: String,
    /// Provider name, e.g. `"kakaotalk"`, `"android.notification"`.
    pub source: String,
    /// Opaque per-account reference (**never** a credential).
    pub account_ref: String,
    /// How this event's data was acquired.
    pub acquisition_tier: AcquisitionTier,
    /// Event kind, e.g. `"notification.posted"`, `"sms.received"`.
    pub kind: String,

    /// When the event happened at the source (if known).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub occurred_at: Option<DateTime<Utc>>,
    /// When the agent captured/normalized it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingested_at: Option<DateTime<Utc>>,
    /// Originating device (a dedupe input; opaque).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub device_id: Option<String>,
    /// Cross-device idempotency key — a content hash of stable fields (see
    /// [`EventEnvelope::compute_dedupe_id`]). The brain dedupes on this.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dedupe_id: Option<String>,

    /// SHA-256 of the raw payload that stayed on-device (provenance pointer).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload_hash: Option<String>,
    /// NAS blob id/hash for the verbatim raw (provenance pointer; never the raw itself).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_evidence_ref: Option<String>,
    /// Short **non-verbatim** summary.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    /// Optional confidence in `[0, 1]`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,

    /// Normalized structured fields destined for the brain's D1 rows. Must be
    /// raw-boundary clean (enforced again at the brain's D1 write).
    #[serde(default)]
    pub structured: serde_json::Map<String, serde_json::Value>,
}

impl EventEnvelope {
    /// Start building an envelope with the required identity/kind fields.
    pub fn builder(
        event_id: impl Into<String>,
        source: impl Into<String>,
        account_ref: impl Into<String>,
        kind: impl Into<String>,
    ) -> EventEnvelopeBuilder {
        EventEnvelopeBuilder {
            inner: EventEnvelope {
                event_id: event_id.into(),
                source: source.into(),
                account_ref: account_ref.into(),
                acquisition_tier: AcquisitionTier::Unknown,
                kind: kind.into(),
                occurred_at: None,
                ingested_at: None,
                device_id: None,
                dedupe_id: None,
                payload_hash: None,
                raw_evidence_ref: None,
                summary: None,
                confidence: None,
                structured: serde_json::Map::new(),
            },
        }
    }

    /// Deterministic content hash of the **stable** identity/provenance fields.
    ///
    /// Two envelopes describing the same source event hash identically regardless of
    /// capture time or transient fields, which is what makes at-least-once delivery
    /// collapse to exactly-once at the brain (it dedupes on this value). `ingested_at`,
    /// `summary`, `confidence`, and `structured` are deliberately excluded so a re-capture
    /// of the same event is recognised as a duplicate.
    pub fn compute_dedupe_id(&self) -> String {
        // A unit-separator join avoids ambiguity between adjacent fields.
        const US: char = '\u{1f}';
        let occurred = self.occurred_at.map(|t| t.to_rfc3339()).unwrap_or_default();
        let payload_hash = self.payload_hash.clone().unwrap_or_default();
        let device = self.device_id.clone().unwrap_or_default();
        let basis = format!(
            "{}{US}{}{US}{}{US}{}{US}{}{US}{}{US}{}",
            self.source, self.account_ref, self.event_id, self.kind, occurred, payload_hash, device,
        );
        let mut hasher = Sha256::new();
        hasher.update(basis.as_bytes());
        hex(&hasher.finalize())
    }

    /// Fill [`EventEnvelope::dedupe_id`] from [`EventEnvelope::compute_dedupe_id`] if unset,
    /// returning `self` for chaining. Idempotent: an already-set id is preserved.
    pub fn ensure_dedupe_id(mut self) -> Self {
        if self.dedupe_id.is_none() {
            self.dedupe_id = Some(self.compute_dedupe_id());
        }
        self
    }

    /// The dedupe id, computing (but not storing) it if unset.
    pub fn dedupe_key(&self) -> String {
        self.dedupe_id
            .clone()
            .unwrap_or_else(|| self.compute_dedupe_id())
    }
}

/// Lowercase hex encoding without pulling in an extra dependency.
fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(char::from_digit((b >> 4) as u32, 16).unwrap());
        s.push(char::from_digit((b & 0x0f) as u32, 16).unwrap());
    }
    s
}

/// Ergonomic builder for [`EventEnvelope`].
pub struct EventEnvelopeBuilder {
    inner: EventEnvelope,
}

impl EventEnvelopeBuilder {
    /// Set the acquisition tier.
    pub fn acquisition_tier(mut self, tier: AcquisitionTier) -> Self {
        self.inner.acquisition_tier = tier;
        self
    }
    /// Set when the event occurred at the source.
    pub fn occurred_at(mut self, ts: DateTime<Utc>) -> Self {
        self.inner.occurred_at = Some(ts);
        self
    }
    /// Set when the agent captured the event (defaults to now in [`Self::build`]).
    pub fn ingested_at(mut self, ts: DateTime<Utc>) -> Self {
        self.inner.ingested_at = Some(ts);
        self
    }
    /// Set the originating device id.
    pub fn device_id(mut self, id: impl Into<String>) -> Self {
        self.inner.device_id = Some(id.into());
        self
    }
    /// Set the raw payload hash (provenance pointer).
    pub fn payload_hash(mut self, h: impl Into<String>) -> Self {
        self.inner.payload_hash = Some(h.into());
        self
    }
    /// Set the NAS raw-evidence reference (provenance pointer).
    pub fn raw_evidence_ref(mut self, r: impl Into<String>) -> Self {
        self.inner.raw_evidence_ref = Some(r.into());
        self
    }
    /// Set the short non-verbatim summary.
    pub fn summary(mut self, s: impl Into<String>) -> Self {
        self.inner.summary = Some(s.into());
        self
    }
    /// Set the confidence value.
    pub fn confidence(mut self, c: f64) -> Self {
        self.inner.confidence = Some(c);
        self
    }
    /// Insert one normalized structured field.
    pub fn structured_field(mut self, key: impl Into<String>, value: serde_json::Value) -> Self {
        self.inner.structured.insert(key.into(), value);
        self
    }

    /// Finish building: default `ingested_at` to now and fill the dedupe id.
    pub fn build(mut self) -> EventEnvelope {
        if self.inner.ingested_at.is_none() {
            self.inner.ingested_at = Some(Utc::now());
        }
        self.inner.ensure_dedupe_id()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tier_serializes_to_brain_string() {
        let j = serde_json::to_string(&AcquisitionTier::NotificationWal).unwrap();
        assert_eq!(j, "\"notification_wal\"");
    }

    #[test]
    fn device_os_api_tier_round_trips() {
        // Serializes to the exact wire string shared with the brain enum + contract schema.
        let j = serde_json::to_string(&AcquisitionTier::DeviceOsApi).unwrap();
        assert_eq!(j, "\"device_os_api\"");
        // And deserializes back to the same variant.
        let back: AcquisitionTier = serde_json::from_str("\"device_os_api\"").unwrap();
        assert_eq!(back, AcquisitionTier::DeviceOsApi);
    }

    #[test]
    fn dedupe_id_is_stable_and_content_addressed() {
        let a = EventEnvelope::builder("e1", "kakaotalk", "acct-1", "message.posted")
            .payload_hash("abc")
            .build();
        // Same identity fields but different transient fields → same dedupe id.
        let b = EventEnvelope::builder("e1", "kakaotalk", "acct-1", "message.posted")
            .payload_hash("abc")
            .summary("a later re-capture")
            .confidence(0.5)
            .build();
        assert_eq!(a.dedupe_id, b.dedupe_id);

        // Different event → different dedupe id.
        let c = EventEnvelope::builder("e2", "kakaotalk", "acct-1", "message.posted")
            .payload_hash("abc")
            .build();
        assert_ne!(a.dedupe_id, c.dedupe_id);
    }

    #[test]
    fn none_optionals_are_omitted_on_the_wire() {
        let env = EventEnvelope::builder("e1", "s", "a", "k").build();
        let v: serde_json::Value = serde_json::to_value(&env).unwrap();
        let obj = v.as_object().unwrap();
        // Optionals we never set must be absent (brain forbids unknown/None-typed extras
        // only for unknown keys, but omitting keeps the payload minimal and unambiguous).
        assert!(!obj.contains_key("payload_hash"));
        assert!(!obj.contains_key("summary"));
        // Required + defaulted fields are present.
        assert!(obj.contains_key("event_id"));
        assert!(obj.contains_key("acquisition_tier"));
        assert!(obj.contains_key("structured"));
        assert!(obj.contains_key("dedupe_id"));
    }
}
