using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cadence.WindowsAgent.Models;

/// <summary>
/// Mirrors <c>contract/event-envelope.schema.json</c>'s `acquisition_tier` enum (9 values,
/// closed — a 10th value is a `422` envelope-shape rejection under the schema's
/// `additionalProperties: false`/closed-enum semantics) and the brain's
/// <c>cadence.adapters.base.AcquisitionTier</c> (Python `StrEnum`,
/// /home/swjeon/projects/cadence/cadence/adapters/base.py) field-for-field.
///
/// <see cref="DeviceOsApi"/> ("device_os_api") started as this skeleton's speculative
/// placeholder for a real taxonomy gap — none of the original 8 brain-side values cleanly
/// describe an agent reading OS-level telemetry directly (active window, app-usage dwell
/// time, on-change screen-context metadata), as opposed to a notification-listener WAL, an
/// accessibility scrape, or an OAuth/API pull. That gap is now CLOSED: the brain/contract
/// (W1) added a canonical `device_os_api` member for exactly this case (see
/// `cadence/adapters/base.py`'s `AcquisitionTier.DEVICE_OS_API` and the schema's
/// `acquisition_tier.enum`), and the Android sibling agent independently hit the same gap
/// and uses the identical resolved value (`AcquisitionTier.DEVICE_OS_API` in
/// `agents/android/app/src/main/java/com/cadence/agent/envelope/AcquisitionTier.kt`).
/// </summary>
[JsonConverter(typeof(AcquisitionTierJsonConverter))]
public enum AcquisitionTier
{
    OfficialApi,
    OAuth,
    UserToken,
    FileImport,
    NotificationWal,
    ScrapeNonroot,
    Manual,
    Unknown,
    DeviceOsApi,
}

internal static class AcquisitionTierExtensions
{
    /// <summary>Wire representation — matches the schema's closed enum and the brain's StrEnum
    /// values exactly. Source of truth: contract/event-envelope.schema.json, cadence/adapters/base.py.</summary>
    public static string ToWireValue(this AcquisitionTier tier) => tier switch
    {
        AcquisitionTier.OfficialApi => "official_api",
        AcquisitionTier.OAuth => "oauth",
        AcquisitionTier.UserToken => "user_token",
        AcquisitionTier.FileImport => "file_import",
        AcquisitionTier.NotificationWal => "notification_wal",
        AcquisitionTier.ScrapeNonroot => "scrape_nonroot",
        AcquisitionTier.Manual => "manual",
        AcquisitionTier.DeviceOsApi => "device_os_api",
        _ => "unknown",
    };

    /// <summary>Reverse of <see cref="ToWireValue"/>. An unrecognized wire string maps to
    /// <see cref="AcquisitionTier.Unknown"/> (matches the schema's own `"default": "unknown"`)
    /// rather than throwing — this agent only ever writes this type, but a converter needs a
    /// symmetric Read path to be a well-formed <c>JsonConverter&lt;T&gt;</c>.</summary>
    public static AcquisitionTier FromWireValue(string? value) => value switch
    {
        "official_api" => AcquisitionTier.OfficialApi,
        "oauth" => AcquisitionTier.OAuth,
        "user_token" => AcquisitionTier.UserToken,
        "file_import" => AcquisitionTier.FileImport,
        "notification_wal" => AcquisitionTier.NotificationWal,
        "scrape_nonroot" => AcquisitionTier.ScrapeNonroot,
        "manual" => AcquisitionTier.Manual,
        "device_os_api" => AcquisitionTier.DeviceOsApi,
        _ => AcquisitionTier.Unknown,
    };
}

/// <summary>
/// Explicit per-member string (de)serialization for <see cref="AcquisitionTier"/> — NOT a
/// <c>JsonNamingPolicy</c>-based converter (e.g. snake_case naming policies) because that
/// heuristic gets <see cref="AcquisitionTier.OAuth"/> wrong: a mechanical PascalCase-to-
/// snake_case split would emit "o_auth", not the schema's actual "oauth". Explicit mapping
/// (reusing <see cref="AcquisitionTierExtensions.ToWireValue"/>/<c>FromWireValue</c>) avoids
/// that class of bug entirely, mirroring the Android sibling's per-value `@SerialName`
/// annotations (`agents/android/.../envelope/AcquisitionTier.kt`). Without a converter like
/// this at all, System.Text.Json serializes an enum as its underlying `int` by default (e.g.
/// `0` instead of `"official_api"`) — silently wrong on the wire, not merely mis-cased.
/// </summary>
internal sealed class AcquisitionTierJsonConverter : JsonConverter<AcquisitionTier>
{
    public override AcquisitionTier Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
        => AcquisitionTierExtensions.FromWireValue(reader.GetString());

    public override void Write(Utf8JsonWriter writer, AcquisitionTier value, JsonSerializerOptions options)
        => writer.WriteStringValue(value.ToWireValue());
}
