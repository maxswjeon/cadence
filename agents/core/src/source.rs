//! Capture-source abstraction and reference implementations.
//!
//! A [`CaptureSource`] turns platform signals into [`EventEnvelope`]s. **Real OS capture
//! (Android NotificationListener, Windows UI Automation, …) is the platform agents' job**
//! and is deliberately out of scope for this portable core. The reference sources here
//! exist to drive and test the WAL/transport machinery without any OS dependency.
//!
//! ## Non-destructive invariant
//! A `CaptureSource` is **read-only** by contract: `poll` observes and normalizes; it must
//! never mark-read, mute, or otherwise mutate source state. Platform bindings that wrap a
//! real OS source must uphold this (documented and asserted at those bindings).

use crate::envelope::EventEnvelope;

/// A pull-based source of events. `poll` returns any events available *now* (possibly
/// empty). It must not block indefinitely and must never mutate source state.
pub trait CaptureSource {
    /// Provider name for observability (e.g. `"timer"`, `"android.notification"`).
    fn name(&self) -> &str;

    /// Return newly captured events, normalized to envelopes. Empty when nothing is ready.
    fn poll(&mut self) -> Vec<EventEnvelope>;
}

/// A deterministic reference source that emits one synthetic event per [`Self::poll`],
/// numbered from a monotonically increasing counter. Handy for exercising the pipeline.
pub struct TimerSource {
    account_ref: String,
    kind: String,
    counter: u64,
    limit: Option<u64>,
}

impl TimerSource {
    /// Create a timer source tagged with `account_ref`, emitting events of `kind`.
    pub fn new(account_ref: impl Into<String>, kind: impl Into<String>) -> Self {
        TimerSource {
            account_ref: account_ref.into(),
            kind: kind.into(),
            counter: 0,
            limit: None,
        }
    }

    /// Stop emitting after `n` total events (subsequent polls return empty).
    pub fn with_limit(mut self, n: u64) -> Self {
        self.limit = Some(n);
        self
    }
}

impl CaptureSource for TimerSource {
    fn name(&self) -> &str {
        "timer"
    }

    fn poll(&mut self) -> Vec<EventEnvelope> {
        if let Some(limit) = self.limit {
            if self.counter >= limit {
                return Vec::new();
            }
        }
        let n = self.counter;
        self.counter += 1;
        let env = EventEnvelope::builder(
            format!("timer-{n}"),
            "timer",
            self.account_ref.clone(),
            self.kind.clone(),
        )
        .summary(format!("synthetic tick {n}"))
        .structured_field("tick", serde_json::json!(n))
        .build();
        vec![env]
    }
}

/// A source that replays a fixed, pre-built list of envelopes — one drained per `poll`,
/// in order — so tests control the exact event stream (including intentional duplicates).
pub struct FakeSource {
    name: String,
    queue: std::collections::VecDeque<EventEnvelope>,
}

impl FakeSource {
    /// Build a fake source that will emit `events` (one per poll, front to back).
    pub fn new(name: impl Into<String>, events: impl IntoIterator<Item = EventEnvelope>) -> Self {
        FakeSource {
            name: name.into(),
            queue: events.into_iter().collect(),
        }
    }

    /// Events still queued.
    pub fn remaining(&self) -> usize {
        self.queue.len()
    }
}

impl CaptureSource for FakeSource {
    fn name(&self) -> &str {
        &self.name
    }

    fn poll(&mut self) -> Vec<EventEnvelope> {
        self.queue.pop_front().into_iter().collect()
    }
}
