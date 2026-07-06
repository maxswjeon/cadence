//! Proves the bounded queue bounds memory (backpressure), and the reference sources drive
//! the pipeline end to end.

use cadence_agent_core::{
    AgentCore, CaptureSource, EventEnvelope, MockTransport, RetryPolicy, TimerSource,
};

fn ev(id: &str) -> EventEnvelope {
    EventEnvelope::builder(id, "kakaotalk", "acct-1", "message.posted").build()
}

/// Capture is rejected once the bounded WAL is full: the pending set never exceeds the
/// configured capacity, no matter how many events arrive while the brain is unreachable.
#[test]
fn queue_is_bounded_under_outage() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let capacity = 16;
    let mock = MockTransport::new();
    let mut agent = AgentCore::open(&path, capacity, mock, RetryPolicy::no_sleep(4)).unwrap();

    let mut accepted = 0usize;
    let mut rejected = 0usize;
    // Try to push far more than capacity while "the brain is down" (we never drain).
    for i in 0..1000 {
        match agent.capture(ev(&format!("e{i}"))) {
            Ok(_) => accepted += 1,
            Err(cadence_agent_core::AgentError::QueueFull { capacity: c }) => {
                assert_eq!(c, capacity);
                rejected += 1;
            }
            Err(other) => panic!("unexpected error: {other}"),
        }
        assert!(
            agent.pending_len() <= capacity,
            "memory bound held: pending {} <= {}",
            agent.pending_len(),
            capacity
        );
    }

    assert_eq!(accepted, capacity, "accepted exactly capacity events");
    assert_eq!(rejected, 1000 - capacity);
    assert!(agent.is_full());
}

/// Backpressure is a *pause*, not a loss: after the queue fills and a drain frees it, capture
/// resumes and every event is ultimately delivered exactly once.
#[test]
fn drain_relieves_backpressure_and_resumes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let capacity = 8;
    let mock = MockTransport::new();
    let mut agent = AgentCore::open(&path, capacity, mock, RetryPolicy::no_sleep(4)).unwrap();

    // Fill to capacity.
    for i in 0..capacity {
        agent.capture(ev(&format!("first-{i}"))).unwrap();
    }
    assert!(agent.capture(ev("overflow")).is_err(), "full → rejected");

    // Drain frees the queue; capture resumes.
    let report = agent.drain().unwrap();
    assert_eq!(report.delivered, capacity);
    assert_eq!(agent.pending_len(), 0);

    for i in 0..capacity {
        agent.capture(ev(&format!("second-{i}"))).unwrap();
    }
    let report2 = agent.drain().unwrap();
    assert_eq!(report2.delivered, capacity);

    assert_eq!(
        agent.transport_ref().accepted_count(),
        capacity * 2,
        "all events across both waves delivered exactly once"
    );
}

/// The reference `TimerSource` drives capture → drain end to end through `AgentCore`.
#[test]
fn timer_source_drives_pipeline() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");
    let mock = MockTransport::new();
    let mut agent = AgentCore::open(&path, 1024, mock, RetryPolicy::no_sleep(4)).unwrap();

    let mut source = TimerSource::new("acct-1", "timer.tick").with_limit(12);
    // Pull until the source is exhausted.
    let mut captured = 0;
    loop {
        let n = agent.capture_from(&mut source).unwrap();
        if n == 0 {
            break;
        }
        captured += n;
    }
    assert_eq!(captured, 12);

    let report = agent.drain().unwrap();
    assert_eq!(report.delivered, 12);
    assert_eq!(agent.transport_ref().accepted_count(), 12);
    // Source is a read-only observer; nothing left to emit.
    assert!(source.poll().is_empty());
}
