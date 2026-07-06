package com.cadence.agent.wal

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Durable device-side WAL row.
 *
 * `agents/core/src/wal.rs` (the Rust core's own append-only, crash-safe WAL) is the
 * *authoritative* durable queue once [com.cadence.agent.core.CoreBridge] is actually
 * linked — see that file's honest-status note: there is no JNI export layer yet, so
 * nothing is linked today. Until then (and even after, as an Android-idiomatic buffer
 * ahead of the native handoff — collectors are simplest written against Room/coroutines,
 * not JNI calls directly), this table is the durability net: Room gives us a crash-safe
 * append log for free via SQLite's own WAL journal mode. `dedupe_id` is the primary key
 * so re-enqueuing the same content-hashed event is a no-op, mirroring the brain's own
 * dedupe-on-`dedupe_id` semantics (`cadence/ingest/pipeline.py::IngestPipeline.ingest`).
 */
@Entity(tableName = "event_wal")
data class EventEntity(
    @PrimaryKey @ColumnInfo(name = "dedupe_id") val dedupeId: String,
    @ColumnInfo(name = "envelope_json") val envelopeJson: String,
    @ColumnInfo(name = "enqueued_at_millis") val enqueuedAtMillis: Long,
    @ColumnInfo(name = "delivery_attempts") val deliveryAttempts: Int = 0,
    @ColumnInfo(name = "delivered") val delivered: Boolean = false,
)
