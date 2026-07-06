//! Proves exactly-once delivery under `503` backpressure, network drops, and crashes.
//!
//! "Exactly once" here means: every distinct event is `Accepted` (fresh `202`) at the brain
//! **exactly once**, with **no loss** (all captured events delivered) and **no duplicate**
//! ingest (retries collapse to `200` at the brain, which dedupes on `dedupe_id`).

use cadence_agent_core::{AgentCore, DrainStop, EventEnvelope, Fault, MockTransport, RetryPolicy};

fn ev(id: &str) -> EventEnvelope {
    EventEnvelope::builder(id, "kakaotalk", "acct-1", "message.posted")
        .payload_hash(format!("hash-{id}"))
        .build()
}

/// Happy path: everything delivered once, queue emptied.
#[test]
fn all_delivered_exactly_once() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let mock = MockTransport::new();
    let mut agent = AgentCore::open(&path, 1024, mock, RetryPolicy::no_sleep(8)).unwrap();

    let ids: Vec<String> = (0..20)
        .map(|i| agent.capture(ev(&format!("e{i}"))).unwrap())
        .collect();
    let report = agent.drain().unwrap();

    assert_eq!(report.stop, DrainStop::Drained);
    assert_eq!(report.delivered, 20);
    assert_eq!(agent.pending_len(), 0, "no loss: queue drained");

    let accepted = agent_transport_accepted(&agent);
    assert_eq!(accepted.len(), 20, "each event accepted exactly once");
    let mut unique = accepted.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(unique.len(), 20, "no event accepted twice");
    for id in &ids {
        assert!(accepted.contains(id));
    }
}

/// Under a burst of network drops + `503`s, the agent retries and still delivers every
/// event exactly once (no loss, no dup).
#[test]
fn retries_through_faults_no_loss_no_dup() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let mock = MockTransport::new();
    // Script faults on the first several send calls: drop, drop, 503, drop, 503 — then clear.
    mock.push_faults([
        Fault::NetworkDrop,
        Fault::NetworkDrop,
        Fault::Backpressure,
        Fault::NetworkDrop,
        Fault::Backpressure,
    ]);
    let mut agent = AgentCore::open(&path, 1024, mock, RetryPolicy::no_sleep(50)).unwrap();

    for i in 0..10 {
        agent.capture(ev(&format!("e{i}"))).unwrap();
    }
    let report = agent.drain().unwrap();

    assert_eq!(report.stop, DrainStop::Drained);
    assert_eq!(report.delivered, 10);
    assert!(report.network_errors >= 3);
    assert!(report.backpressure_hits >= 2);
    assert_eq!(agent.pending_len(), 0);

    let accepted = agent_transport_accepted(&agent);
    assert_eq!(accepted.len(), 10, "exactly-once despite retries");
}

/// Persistent backpressure stalls the drain, leaving events **safely buffered** (no loss);
/// once the wall clears a later drain delivers them exactly once.
#[test]
fn persistent_backpressure_buffers_then_delivers() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let mock = MockTransport::new();
    // Exactly the retry budget's worth of 503s on the head event → the first drain
    // exhausts its attempts and gives up (buffering everything); the faults are then spent.
    mock.push_faults(std::iter::repeat(Fault::Backpressure).take(4));
    let mut agent = AgentCore::open(&path, 1024, mock, RetryPolicy::no_sleep(4)).unwrap();

    for i in 0..5 {
        agent.capture(ev(&format!("e{i}"))).unwrap();
    }
    let first = agent.drain().unwrap();
    assert_eq!(first.stop, DrainStop::Backpressured);
    assert_eq!(first.delivered, 0);
    assert_eq!(
        agent.pending_len(),
        5,
        "no loss: still buffered under backpressure"
    );

    // Second drain: the scripted faults are exhausted, so it now delivers all five.
    let second = agent.drain().unwrap();
    assert_eq!(second.stop, DrainStop::Drained);
    assert_eq!(second.delivered, 5);
    assert_eq!(agent.pending_len(), 0);

    let accepted = agent_transport_accepted(&agent);
    assert_eq!(accepted.len(), 5, "exactly-once across the two drains");
}

/// A crash between the wire send and the WAL ack redelivers the event; the brain dedupes it
/// (`200`), so it is ingested **exactly once** — the core exactly-once guarantee.
#[test]
fn crash_between_send_and_ack_is_exactly_once() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");

    // Shared "brain" state across the two process lifetimes so the dedupe memory persists,
    // exactly like the real brain would remember the dedupe_id across an agent restart.
    let brain = std::sync::Arc::new(MockTransport::new());

    let dedupe_id;
    {
        // First lifetime: capture, and deliver via a transport that records the send at the
        // brain but then we "crash" before the agent can ack (drop without draining ack).
        let mut agent = AgentCore::open(
            &path,
            1024,
            SharedMock(brain.clone()),
            RetryPolicy::no_sleep(4),
        )
        .unwrap();
        dedupe_id = agent.capture(ev("e1")).unwrap();

        // Manually send once (brain records the 202) but do NOT let the agent ack: simulate
        // the crash window by sending directly through the shared brain.
        use cadence_agent_core::Transport;
        let env = ev("e1");
        let outcome = SharedMock(brain.clone()).send(&env);
        assert!(outcome.is_delivered());
        assert_eq!(brain.accepted_count(), 1);
        // Agent still has it pending (it never acked) → drop == crash.
        assert_eq!(agent.pending_len(), 1);
    }

    // Second lifetime: replay the WAL and drain. The event is redelivered; the brain returns
    // a duplicate (200), the agent acks, and the brain's accepted count stays at 1.
    {
        let mut agent = AgentCore::open(
            &path,
            1024,
            SharedMock(brain.clone()),
            RetryPolicy::no_sleep(4),
        )
        .unwrap();
        assert_eq!(
            agent.pending_len(),
            1,
            "un-acked event replayed after crash"
        );
        let report = agent.drain().unwrap();
        assert_eq!(report.delivered, 1);
        assert_eq!(agent.pending_len(), 0);
    }

    assert_eq!(
        brain.accepted_count(),
        1,
        "ingested exactly once despite the crash-redelivery"
    );
    assert!(brain.has_seen(&dedupe_id));
}

/// A permanent `422` rejection is dead-lettered (not retried forever) and does not block the
/// rest of the queue.
#[test]
fn rejected_event_is_dead_lettered() {
    use cadence_agent_core::{Outcome, Transport};

    // A transport that rejects one specific event id and accepts the rest.
    struct Rejecting;
    impl Transport for Rejecting {
        fn send(&self, env: &EventEnvelope) -> Outcome {
            if env.event_id == "bad" {
                Outcome::Rejected {
                    reason: "raw_boundary_violation".into(),
                }
            } else {
                Outcome::Accepted {
                    dedupe_id: env.dedupe_key(),
                }
            }
        }
    }

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let mut agent = AgentCore::open(&path, 1024, Rejecting, RetryPolicy::no_sleep(4)).unwrap();

    agent.capture(ev("good1")).unwrap();
    agent
        .capture(EventEnvelope::builder("bad", "s", "a", "k").build())
        .unwrap();
    agent.capture(ev("good2")).unwrap();

    let report = agent.drain().unwrap();
    assert_eq!(report.delivered, 2);
    assert_eq!(report.dead_lettered, 1);
    assert_eq!(
        agent.pending_len(),
        0,
        "queue not blocked by the rejected event"
    );
    assert_eq!(agent.dead_letter().len(), 1);
    assert_eq!(agent.dead_letter()[0].event_id, "bad");
}

// --- helpers ------------------------------------------------------------- //

/// A `Transport` wrapper letting the test share one `MockTransport` (the "brain") across two
/// `AgentCore` lifetimes, so dedupe memory persists across the simulated crash.
struct SharedMock(std::sync::Arc<MockTransport>);
impl cadence_agent_core::Transport for SharedMock {
    fn send(&self, env: &EventEnvelope) -> cadence_agent_core::Outcome {
        self.0.send(env)
    }
}

/// Pull the accepted-dedupe-id log out of an agent whose transport is a `MockTransport`.
/// (Kept as a free fn so the two mock-based tests share the assertion shape.)
fn agent_transport_accepted(agent: &AgentCore<MockTransport>) -> Vec<String> {
    agent.transport_ref().accepted_log()
}
