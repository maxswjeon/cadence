package com.cadence.agent.wal

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [EventEntity::class], version = 1, exportSchema = true)
abstract class CadenceDatabase : RoomDatabase() {
    abstract fun eventDao(): EventDao

    companion object {
        const val DATABASE_NAME = "cadence_wal.db"
        // TODO(device): Room.databaseBuilder(context, CadenceDatabase::class.java,
        //   DATABASE_NAME).build() — wired up in CaptureService.onCreate() once this
        //   module can actually build.
    }
}
