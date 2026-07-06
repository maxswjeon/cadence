//! The agent orchestrator: WAL + transport + retry/backoff, wired for exactly-once.
//!
//! Lifecycle of one event:
//! 1. [`AgentCore::capture`] durably appends it to the WAL (`fsync`) — or returns
//!    [`AgentError::QueueFull`] when the bounded queue is full (backpressure to the source).
//! 2. [`AgentCore::drain`] sends pending events oldest-first. On confirmed delivery
//!    (`202`/`200`) it acks + drops. On `503`/network it retries with capped exponential
//!    backoff, and if the wall persists it stops the pass leaving events safely in the WAL.
//! 3. A crash before the ack leaves the event pending; the next process replays the WAL and
//!    redelivers. The brain dedupes on `dedupe_id`, so redelivery is a no-op → exactly-once.

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use crate::envelope::EventEnvelope;
use crate::error::Result;
use crate::source::CaptureSource;
use crate::transport::{Outcome, Transport};
use crate::wal::Wal;

/// Capped exponential backoff with an injectable sleeper (no-op in tests).
#[derive(Clone)]
pub struct RetryPolicy {
    base: Duration,
    max: Duration,
    /// Max attempts for a single event against a persistent `503`/network wall before the
    /// drain pass yields (the event stays pending and is retried on the next drain).
    max_attempts: u32,
    sleep: Arc<dyn Fn(Duration) + Send + Sync>,
}

impl RetryPolicy {
    /// Production defaults: 200 ms base, 30 s cap, 6 attempts, real sleeps.
    pub fn new(base: Duration, max: Duration, max_attempts: u32) -> Self {
        RetryPolicy {
            base,
            max,
            max_attempts: max_attempts.max(1),
            sleep: Arc::new(std::thread::sleep),
        }
    }

    /// A policy whose sleeps are no-ops — deterministic and instant, for tests.
    pub fn no_sleep(max_attempts: u32) -> Self {
        RetryPolicy {
            base: Duration::from_millis(1),
            max: Duration::from_millis(1),
            max_attempts: max_attempts.max(1),
            sleep: Arc::new(|_| {}),
        }
    }

    fn backoff_for(&self, attempt: u32) -> Duration {
        // attempt is 1-based; delay = base * 2^(attempt-1), capped.
        let shift = attempt.saturating_sub(1).min(31);
        let scaled = self.base.saturating_mul(1u32 << shift);
        scaled.min(self.max)
    }
}

impl Default for RetryPolicy {
    fn default() -> Self {
        RetryPolicy::new(Duration::from_millis(200), Duration::from_secs(30), 6)
    }
}

/// Why a [`AgentCore::drain`] pass stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DrainStop {
    /// Every pending event was delivered.
    #[default]
    Drained,
    /// The brain kept returning `503`; remaining events stay buffered for the next pass.
    Backpressured,
    /// Persistent network failure; remaining events stay buffered for the next pass.
    NetworkStalled,
}

/// Metrics for a single drain pass.
#[derive(Debug, Clone, Default)]
pub struct DrainReport {
    /// Events confirmed delivered (`202` or `200`).
    pub delivered: usize,
    /// Events dead-lettered on a permanent `422` rejection.
    pub dead_lettered: usize,
    /// Count of `503` responses observed.
    pub backpressure_hits: usize,
    /// Count of network errors observed.
    pub network_errors: usize,
    /// Events still pending after the pass.
    pub pending_after: usize,
    /// Why the pass ended.
    pub stop: DrainStop,
}

/// The portable agent core: owns the WAL and a transport, and turns captured events into
/// exactly-once deliveries.
pub struct AgentCore<T: Transport> {
    wal: Wal,
    transport: T,
    policy: RetryPolicy,
    dead_letter: Vec<EventEnvelope>,
}

impl<T: Transport> AgentCore<T> {
    /// Open the WAL at `path` (replaying any un-acked events), bound the pending queue at
    /// `capacity`, and use `transport` + `policy` for delivery.
    pub fn open(
        path: impl AsRef<Path>,
        capacity: usize,
        transport: T,
        policy: RetryPolicy,
    ) -> Result<Self> {
        Ok(AgentCore {
            wal: Wal::open(path, capacity)?,
            transport,
            policy,
            dead_letter: Vec::new(),
        })
    }

    /// Durably capture one event. Returns [`AgentError::QueueFull`] when the bounded WAL is
    /// full — the caller must stop pulling from its [`CaptureSource`] until a drain frees
    /// space (this is what bounds device memory under a brain outage).
    pub fn capture(&mut self, env: EventEnvelope) -> Result<String> {
        self.wal.append(env)
    }

    /// Poll a capture source once and durably capture everything it yields, stopping early
    /// with [`AgentError::QueueFull`] if the WAL fills mid-batch (backpressure). Returns the
    /// number of events captured.
    pub fn capture_from(&mut self, source: &mut dyn CaptureSource) -> Result<usize> {
        let mut n = 0;
        for env in source.poll() {
            self.wal.append(env)?;
            n += 1;
        }
        Ok(n)
    }

    /// Number of un-acked events buffered.
    pub fn pending_len(&self) -> usize {
        self.wal.pending_len()
    }

    /// `true` when the bounded queue is full and capture must pause.
    pub fn is_full(&self) -> bool {
        self.wal.is_full()
    }

    /// Events permanently rejected (`422`) this process — surfaced for logging/inspection.
    pub fn dead_letter(&self) -> &[EventEnvelope] {
        &self.dead_letter
    }

    /// Borrow the underlying transport (for metrics/inspection in tests and callers).
    pub fn transport_ref(&self) -> &T {
        &self.transport
    }

    /// Attempt to deliver all pending events, oldest first. See [`DrainReport`].
    pub fn drain(&mut self) -> Result<DrainReport> {
        let mut report = DrainReport::default();

        loop {
            // Snapshot the current head of the pending queue.
            let Some(env) = self.wal.pending_in_order().into_iter().next() else {
                report.stop = DrainStop::Drained;
                break;
            };
            let id = env.dedupe_key();

            let mut attempt = 0u32;
            loop {
                match self.transport.send(&env) {
                    o if o.is_delivered() => {
                        self.wal.ack(&id)?;
                        report.delivered += 1;
                        break;
                    }
                    Outcome::Rejected { .. } => {
                        // Permanent client error: dead-letter and drop so we don't spin.
                        self.wal.ack(&id)?;
                        self.dead_letter.push(env.clone());
                        report.dead_lettered += 1;
                        break;
                    }
                    Outcome::Backpressure => {
                        report.backpressure_hits += 1;
                        attempt += 1;
                        if attempt >= self.policy.max_attempts {
                            report.stop = DrainStop::Backpressured;
                            report.pending_after = self.wal.pending_len();
                            return Ok(report);
                        }
                        (self.policy.sleep)(self.policy.backoff_for(attempt));
                    }
                    Outcome::NetworkError { .. } => {
                        report.network_errors += 1;
                        attempt += 1;
                        if attempt >= self.policy.max_attempts {
                            report.stop = DrainStop::NetworkStalled;
                            report.pending_after = self.wal.pending_len();
                            return Ok(report);
                        }
                        (self.policy.sleep)(self.policy.backoff_for(attempt));
                    }
                    // `is_delivered()` already matched Accepted/Duplicate above; this arm is
                    // unreachable but keeps the match total without a catch-all that would
                    // hide a future variant.
                    Outcome::Accepted { .. } | Outcome::Duplicate { .. } => unreachable!(),
                }
            }
        }

        report.pending_after = self.wal.pending_len();
        Ok(report)
    }

    /// Force a WAL compaction (rewrite to hold only live pending events). Normally
    /// automatic; exposed for explicit control and testing.
    pub fn compact(&mut self) -> Result<()> {
        self.wal.compact()
    }
}
