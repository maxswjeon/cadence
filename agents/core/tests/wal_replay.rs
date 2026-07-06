//! Proves the WAL's crash-safety properties.

use std::io::{Seek, SeekFrom, Write};

use cadence_agent_core::{AcquisitionTier, EventEnvelope, Wal};

fn ev(id: &str) -> EventEnvelope {
    EventEnvelope::builder(id, "kakaotalk", "acct-1", "message.posted")
        .acquisition_tier(AcquisitionTier::NotificationWal)
        .payload_hash(format!("hash-{id}"))
        .summary("non-verbatim summary")
        .build()
}

/// Un-acked events survive a "crash" (drop without ack) and are replayed on reopen; acked
/// events do not come back. Nothing is lost.
#[test]
fn replay_recovers_unacked_and_drops_acked() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");

    let (id1, id3);
    {
        let mut wal = Wal::open(&path, 1024).unwrap();
        id1 = wal.append(ev("e1")).unwrap();
        let id2 = wal.append(ev("e2")).unwrap();
        id3 = wal.append(ev("e3")).unwrap();
        // e2 is delivered+acked; e1 and e3 are still in flight when we "crash".
        wal.ack(&id2).unwrap();
        assert_eq!(wal.pending_len(), 2);
        // drop without acking e1/e3 == process crash
    }

    let wal = Wal::open(&path, 1024).unwrap();
    let pending: Vec<String> = wal
        .pending_in_order()
        .iter()
        .map(|e| e.dedupe_key())
        .collect();
    assert_eq!(
        pending,
        vec![id1, id3],
        "exactly the un-acked events replay, in order"
    );
    assert_eq!(wal.pending_len(), 2);
}

/// A torn tail (partial frame from a crash mid-write, or trailing garbage) is discarded on
/// replay; every intact prior frame is still recovered.
#[test]
fn torn_tail_is_discarded_intact_prefix_recovered() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");

    let id1;
    {
        let mut wal = Wal::open(&path, 1024).unwrap();
        id1 = wal.append(ev("e1")).unwrap();
        wal.append(ev("e2")).unwrap();
    }

    // Simulate a crash mid-append: append a bogus, incomplete frame to the file tail.
    {
        let mut f = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        f.seek(SeekFrom::End(0)).unwrap();
        // A length header claiming a big payload, then only a few bytes — a torn frame.
        f.write_all(&1024u32.to_be_bytes()).unwrap();
        f.write_all(&0xdead_beefu32.to_be_bytes()).unwrap();
        f.write_all(b"partial").unwrap();
        f.sync_all().unwrap();
    }

    let wal = Wal::open(&path, 1024).unwrap();
    assert_eq!(
        wal.pending_len(),
        2,
        "both intact events survive the torn tail"
    );
    assert_eq!(wal.pending_in_order()[0].dedupe_key(), id1);

    // The log is usable after recovery: appending works and persists.
    let mut wal = wal;
    wal.append(ev("e3")).unwrap();
    drop(wal);
    let wal = Wal::open(&path, 1024).unwrap();
    assert_eq!(wal.pending_len(), 3);
}

/// Compaction preserves the exact pending set and order, and is durable across reopen.
#[test]
fn compaction_preserves_pending() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("agent.wal");

    let mut wal = Wal::open(&path, 1024).unwrap();
    let ids: Vec<String> = (0..10)
        .map(|i| wal.append(ev(&format!("e{i}"))).unwrap())
        .collect();
    // Ack the even ones.
    for i in (0..10).step_by(2) {
        wal.ack(&ids[i]).unwrap();
    }
    let before: Vec<String> = wal
        .pending_in_order()
        .iter()
        .map(|e| e.dedupe_key())
        .collect();
    wal.compact().unwrap();
    let after: Vec<String> = wal
        .pending_in_order()
        .iter()
        .map(|e| e.dedupe_key())
        .collect();
    assert_eq!(before, after, "compaction keeps pending set + order");
    drop(wal);

    let wal = Wal::open(&path, 1024).unwrap();
    let reopened: Vec<String> = wal
        .pending_in_order()
        .iter()
        .map(|e| e.dedupe_key())
        .collect();
    assert_eq!(reopened, after, "compacted log survives reopen");
    assert_eq!(wal.pending_len(), 5);
}
