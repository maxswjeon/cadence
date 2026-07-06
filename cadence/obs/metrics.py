"""Minimal metrics rendering (Prometheus text exposition format).

Aggregates counters from the alarm sink and the raw-egress log, plus the live
replication-queue depth from a :class:`~cadence.stores.d1.D1Store` when provided.
Exposed via the ``/metrics`` endpoint in :mod:`cadence.brain.app`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cadence.obs.alarms import get_alarm_sink
from cadence.obs.egress import EgressChannel, get_egress_log

if TYPE_CHECKING:  # pragma: no cover
    from cadence.stores.d1 import D1Store


def render_prometheus(store: D1Store | None = None) -> str:
    """Render current counters in Prometheus text format."""
    sink = get_alarm_sink()
    egress = get_egress_log()
    lines: list[str] = []

    def metric(name: str, value: float, help_text: str, labels: str = "") -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        suffix = f"{{{labels}}}" if labels else ""
        lines.append(f"{name}{suffix} {value}")

    for alarm in ("raw_to_cloud_violation", "credential_vault_access", "replication_queue_depth"):
        metric(
            "cadence_alarm_total",
            sink.count(alarm),
            "Count of fired alarms by name.",
            labels=f'name="{alarm}"',
        )

    for channel in EgressChannel:
        metric(
            "cadence_raw_egress_total",
            egress.count(channel),
            "Count of sanctioned raw-content egress events by channel.",
            labels=f'channel="{channel.value}"',
        )

    if store is not None:
        metric(
            "cadence_replication_queue_depth",
            store.replica.queue_depth,
            "Pending Cloudflare-D1 replication ops.",
        )

    return "\n".join(lines) + "\n"


__all__ = ["render_prometheus"]
