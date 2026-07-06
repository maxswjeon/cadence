package com.cadence.agent.envelope

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Mirrors `cadence.adapters.base.AcquisitionTier` (brain, Python `StrEnum`) field-for-field.
 *
 * Includes `device_os_api` — added by the W1 owner (`m2-1`) to close a real taxonomy gap
 * this skeleton's on-device-OS-API collectors (SMS, app-usage, location, telemetry) hit:
 * none of the original eight values cleanly described "read a system API directly, not a
 * UI scrape, not an OAuth-token API call" (`cadence/adapters/base.py`'s
 * `DEVICE_OS_API` comment: "direct on-device OS/system-API read, not otherwise
 * categorized (app-usage, active-window, telemetry, SMS provider, location);
 * notifications keep [NOTIFICATION_WAL]"). The Windows sibling skeleton independently
 * hit the identical gap first and worked around it locally with a non-canonical
 * `DeviceOsApi` member (`agents/windows/Models/AcquisitionTier.cs`) before this
 * reconciliation landed; that file has not yet been updated to the canonical value.
 */
@Serializable
enum class AcquisitionTier {
    @SerialName("official_api") OFFICIAL_API,
    @SerialName("oauth") OAUTH,
    @SerialName("user_token") USER_TOKEN,
    @SerialName("file_import") FILE_IMPORT,
    @SerialName("notification_wal") NOTIFICATION_WAL,
    @SerialName("scrape_nonroot") SCRAPE_NONROOT,
    @SerialName("manual") MANUAL,
    @SerialName("unknown") UNKNOWN,
    @SerialName("device_os_api") DEVICE_OS_API,
}
