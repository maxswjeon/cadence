package com.cadence.agent.wal

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

/**
 * DAO for the device-side WAL (see [EventEntity] for how this relates to the Rust core's
 * own WAL). `dedupe_id` is the primary key, so `enqueue` of an already-seen id is a
 * silent `IGNORE` — duplicate suppression happens on both ends of the wire, mirroring
 * the brain's dedupe-on-`dedupe_id` (`cadence/ingest/pipeline.py`).
 */
@Dao
interface EventDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(event: EventEntity): Long

    @Query("SELECT * FROM event_wal WHERE delivered = 0 ORDER BY enqueued_at_millis ASC LIMIT :limit")
    suspend fun pendingBatch(limit: Int = 200): List<EventEntity>

    @Query("SELECT * FROM event_wal WHERE delivered = 0 ORDER BY enqueued_at_millis ASC")
    fun observePending(): Flow<List<EventEntity>>

    @Query("UPDATE event_wal SET delivered = 1 WHERE dedupe_id = :dedupeId")
    suspend fun markDelivered(dedupeId: String)

    @Query("UPDATE event_wal SET delivery_attempts = delivery_attempts + 1 WHERE dedupe_id = :dedupeId")
    suspend fun recordAttempt(dedupeId: String)

    @Query("SELECT COUNT(*) FROM event_wal WHERE delivered = 0")
    suspend fun pendingDepth(): Int

    // TODO(device): a max-depth check here (mirroring the brain's own WALBuffer.max_depth
    // backpressure, cadence/ingest/pipeline.py) belongs to CaptureService's drain-loop
    // policy, not this DAO — this DAO only persists.
}
