//! Crash-safe, append-only write-ahead log with a bounded pending set.
//!
//! # Durability model
//! Every captured event is written to disk **and `fsync`ed before** the transport is
//! allowed to send it. Delivery is confirmed by appending an `Ack` record (also
//! `fsync`ed). On restart the log is replayed: `pending = appended − acked`. So:
//!
//! * A crash **after append, before ack** leaves the event pending → it is redelivered.
//!   The brain dedupes on `dedupe_id`, collapsing the redelivery to a no-op. → no loss.
//! * A crash **mid-write** leaves a torn tail frame; replay stops at the first frame that
//!   fails its length/CRC check, recovering every intact prior frame. → no corruption.
//!
//! # Framing
//! ```text
//! [8-byte magic "CADWAL01"]  (file header, written once)
//! repeated frames:
//!   [u32 be payload_len][u32 be crc32(payload)][payload = JSON WalRecord]
//! ```
//!
//! # Bounded memory (backpressure)
//! The pending set is capped at `capacity`. [`Wal::append`] returns
//! [`AgentError::QueueFull`] once full, which the agent maps to backpressure on its
//! capture source — bounding device memory regardless of how long the brain is down.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::envelope::EventEnvelope;
use crate::error::{AgentError, Result};

const MAGIC: &[u8; 8] = b"CADWAL01";

/// A single durable log record.
#[derive(Debug, serde::Serialize, serde::Deserialize)]
enum WalRecord {
    /// An event was captured and must be delivered.
    Append(Box<EventEnvelope>),
    /// The event with this dedupe id was delivered (or permanently dead-lettered) and
    /// may be dropped from the pending set.
    Ack { dedupe_id: String },
}

/// Append-only write-ahead log holding the un-acked (pending) events in memory.
pub struct Wal {
    path: PathBuf,
    file: File,
    capacity: usize,
    /// Dedupe ids in append order (may reference already-acked entries until compaction).
    order: Vec<String>,
    /// Live pending events keyed by dedupe id.
    pending: HashMap<String, EventEnvelope>,
    /// Ack records written since the last compaction — triggers auto-compaction.
    acks_since_compact: usize,
}

impl Wal {
    /// Open (creating if absent) the WAL at `path`, replay it, and cap the pending set at
    /// `capacity` events.
    pub fn open(path: impl AsRef<Path>, capacity: usize) -> Result<Self> {
        assert!(capacity > 0, "wal capacity must be > 0");
        let path = path.as_ref().to_path_buf();
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;

        let (order, pending) = Self::replay(&mut file, &path)?;

        Ok(Wal {
            path,
            file,
            capacity,
            order,
            pending,
            acks_since_compact: 0,
        })
    }

    /// Replay the log from the start, returning the surviving pending set in append order.
    fn replay(
        file: &mut File,
        path: &Path,
    ) -> Result<(Vec<String>, HashMap<String, EventEnvelope>)> {
        file.seek(SeekFrom::Start(0))?;
        let len = file.metadata()?.len();

        // Empty file: write the header and start fresh.
        if len == 0 {
            file.write_all(MAGIC)?;
            file.sync_all()?;
            file.seek(SeekFrom::End(0))?;
            return Ok((Vec::new(), HashMap::new()));
        }

        let mut reader = BufReader::new(&mut *file);
        let mut magic = [0u8; 8];
        reader
            .read_exact(&mut magic)
            .map_err(|_| AgentError::CorruptHeader {
                detail: format!("{}: file too short for header", path.display()),
            })?;
        if &magic != MAGIC {
            return Err(AgentError::CorruptHeader {
                detail: format!("{}: bad magic {magic:?}", path.display()),
            });
        }

        let mut order: Vec<String> = Vec::new();
        let mut pending: HashMap<String, EventEnvelope> = HashMap::new();
        // Byte offset of the last good frame boundary (after the header initially).
        let mut good_offset: u64 = MAGIC.len() as u64;

        // Stops at the first non-`Frame` outcome: a clean EOF, or a torn tail from a crash
        // mid-write. Either way we stop and (below) truncate any partial trailing bytes so
        // the next append starts at a clean frame boundary.
        while let FrameRead::Frame(bytes) = read_frame(&mut reader) {
            good_offset += 8 + bytes.len() as u64;
            let rec: WalRecord = serde_json::from_slice(&bytes)?;
            match rec {
                WalRecord::Append(env) => {
                    let id = env.dedupe_key();
                    if !pending.contains_key(&id) {
                        order.push(id.clone());
                    }
                    pending.insert(id, *env);
                }
                WalRecord::Ack { dedupe_id } => {
                    pending.remove(&dedupe_id);
                }
            }
        }

        drop(reader);
        // Discard any torn trailing bytes past the last intact frame.
        if good_offset < len {
            file.set_len(good_offset)?;
            file.sync_all()?;
        }
        file.seek(SeekFrom::End(0))?;
        Ok((order, pending))
    }

    /// Number of un-acked events currently held.
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Configured maximum pending set size.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// `true` when no more events can be appended until some are acked.
    pub fn is_full(&self) -> bool {
        self.pending.len() >= self.capacity
    }

    /// Pending events in append order (oldest first) — the drain order.
    pub fn pending_in_order(&self) -> Vec<EventEnvelope> {
        self.order
            .iter()
            .filter_map(|id| self.pending.get(id).cloned())
            .collect()
    }

    /// Durably append an event. The `dedupe_id` is filled if unset. Fails with
    /// [`AgentError::QueueFull`] when the bounded pending set is full (backpressure).
    pub fn append(&mut self, env: EventEnvelope) -> Result<String> {
        let env = env.ensure_dedupe_id();
        let id = env.dedupe_key();

        // Re-appending an already-pending event (e.g. idempotent re-capture) is a no-op
        // that must not count against capacity or duplicate the on-disk record.
        if self.pending.contains_key(&id) {
            return Ok(id);
        }
        if self.pending.len() >= self.capacity {
            return Err(AgentError::QueueFull {
                capacity: self.capacity,
            });
        }

        self.write_record(&WalRecord::Append(Box::new(env.clone())))?;
        self.order.push(id.clone());
        self.pending.insert(id.clone(), env);
        Ok(id)
    }

    /// Durably record that `dedupe_id` was delivered (or dead-lettered); drops it from the
    /// pending set. Unknown ids are ignored (idempotent).
    pub fn ack(&mut self, dedupe_id: &str) -> Result<()> {
        if self.pending.remove(dedupe_id).is_none() {
            return Ok(());
        }
        self.write_record(&WalRecord::Ack {
            dedupe_id: dedupe_id.to_string(),
        })?;
        self.acks_since_compact += 1;
        // Keep the log from growing without bound: once acks dominate, rewrite it to hold
        // only the live pending set. Threshold is heuristic; correctness is independent.
        if self.acks_since_compact >= 128 && self.acks_since_compact > self.pending.len() {
            self.compact()?;
        }
        Ok(())
    }

    /// Rewrite the log to contain only the live pending set. Crash-safe: writes a sibling
    /// temp file, `fsync`s it, then atomically renames over the live path.
    pub fn compact(&mut self) -> Result<()> {
        let tmp_path = self.path.with_extension("wal.compact");
        {
            let mut tmp = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp_path)?;
            tmp.write_all(MAGIC)?;
            for id in &self.order {
                if let Some(env) = self.pending.get(id) {
                    let bytes = serde_json::to_vec(&WalRecord::Append(Box::new(env.clone())))?;
                    write_frame(&mut tmp, &bytes)?;
                }
            }
            tmp.sync_all()?;
        }
        std::fs::rename(&tmp_path, &self.path)?;
        // Reopen the live handle at the end of the freshly compacted log.
        let mut file = OpenOptions::new().read(true).write(true).open(&self.path)?;
        file.seek(SeekFrom::End(0))?;
        self.file = file;
        // Rebuild `order` to drop acked ids that are no longer present.
        self.order.retain(|id| self.pending.contains_key(id));
        self.acks_since_compact = 0;
        Ok(())
    }

    fn write_record(&mut self, rec: &WalRecord) -> Result<()> {
        let bytes = serde_json::to_vec(rec)?;
        write_frame(&mut self.file, &bytes)?;
        // The durability point: the bytes are on stable storage before we return, so the
        // caller may safely attempt delivery knowing a crash cannot lose the event.
        self.file.sync_all()?;
        Ok(())
    }
}

/// Outcome of attempting to read one frame.
enum FrameRead {
    Frame(Vec<u8>),
    /// Clean end of file at a frame boundary.
    Eof,
    /// A partially written trailing frame (crash mid-write) — treat as end of log.
    Torn,
}

fn read_frame<R: Read>(reader: &mut R) -> FrameRead {
    let mut header = [0u8; 8];
    match read_full(reader, &mut header) {
        ReadN::Full => {}
        ReadN::Eof => return FrameRead::Eof,
        ReadN::Partial => return FrameRead::Torn,
    }
    let len = u32::from_be_bytes([header[0], header[1], header[2], header[3]]) as usize;
    let crc = u32::from_be_bytes([header[4], header[5], header[6], header[7]]);
    // Guard against an absurd length from a torn/garbage header.
    if len > 64 * 1024 * 1024 {
        return FrameRead::Torn;
    }
    let mut payload = vec![0u8; len];
    match read_full(reader, &mut payload) {
        ReadN::Full => {}
        _ => return FrameRead::Torn,
    }
    if crc32fast::hash(&payload) != crc {
        return FrameRead::Torn;
    }
    FrameRead::Frame(payload)
}

fn write_frame<W: Write>(w: &mut W, payload: &[u8]) -> Result<()> {
    let len = payload.len() as u32;
    let crc = crc32fast::hash(payload);
    w.write_all(&len.to_be_bytes())?;
    w.write_all(&crc.to_be_bytes())?;
    w.write_all(payload)?;
    Ok(())
}

enum ReadN {
    Full,
    Eof,
    Partial,
}

/// Read exactly `buf.len()` bytes, distinguishing a clean EOF (nothing read) from a
/// partial read (torn tail).
fn read_full<R: Read>(reader: &mut R, buf: &mut [u8]) -> ReadN {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => {
                return if filled == 0 {
                    ReadN::Eof
                } else {
                    ReadN::Partial
                };
            }
            Ok(n) => filled += n,
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return ReadN::Partial,
        }
    }
    ReadN::Full
}
