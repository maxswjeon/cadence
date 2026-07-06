//! Error type shared across the agent core.

use std::fmt;

/// Errors surfaced by the WAL and agent orchestration layers.
#[derive(Debug)]
pub enum AgentError {
    /// An I/O error touching the write-ahead log file.
    Io(std::io::Error),
    /// Serialization/deserialization of a WAL record or envelope failed.
    Serde(serde_json::Error),
    /// The bounded WAL queue is full — the caller must apply backpressure to its
    /// capture source instead of buffering more (bounds device memory usage).
    QueueFull {
        /// Configured maximum number of un-acked events held in the WAL.
        capacity: usize,
    },
    /// The on-disk WAL header is missing or from an incompatible format version.
    CorruptHeader {
        /// Human-readable detail about what failed the header check.
        detail: String,
    },
}

impl fmt::Display for AgentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AgentError::Io(e) => write!(f, "wal io error: {e}"),
            AgentError::Serde(e) => write!(f, "wal serde error: {e}"),
            AgentError::QueueFull { capacity } => {
                write!(
                    f,
                    "wal queue full (capacity {capacity}); apply backpressure"
                )
            }
            AgentError::CorruptHeader { detail } => write!(f, "corrupt wal header: {detail}"),
        }
    }
}

impl std::error::Error for AgentError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            AgentError::Io(e) => Some(e),
            AgentError::Serde(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for AgentError {
    fn from(e: std::io::Error) -> Self {
        AgentError::Io(e)
    }
}

impl From<serde_json::Error> for AgentError {
    fn from(e: serde_json::Error) -> Self {
        AgentError::Serde(e)
    }
}

/// Convenience alias for results returned by the core.
pub type Result<T> = std::result::Result<T, AgentError>;
