"""Shared pytest fixtures.

Every test gets isolated temp storage dirs and a fresh in-memory D1. The process-wide
alarm sink and egress log are singletons, so an autouse fixture clears them between
tests to keep invariant assertions (e.g. "zero raw-to-cloud violations") independent.
"""

from __future__ import annotations

import pytest

from cadence.config import Settings
from cadence.obs.alarms import get_alarm_sink
from cadence.obs.egress import get_egress_log
from cadence.stores.d1 import D1Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        d1_path=tmp_path / "d1.sqlite",
        nas_dir=tmp_path / "nas",
        r2_dir=tmp_path / "r2",
        vault_dir=tmp_path / "vault",
        vault_master_key="test-master-key",
        # mTLS is not terminated in tests; the fail-closed default would 401 every
        # TestClient call (see cadence.brain.app.create_app / require_mtls).
        require_mtls=False,
    )


@pytest.fixture
def store(settings: Settings) -> D1Store:
    s = D1Store(settings, url="sqlite://")
    s.init_schema()
    return s


@pytest.fixture(autouse=True)
def _reset_singletons():
    get_alarm_sink().clear()
    # egress log has no clear(); rebuild its internal list
    get_egress_log()._records.clear()  # noqa: SLF001
    yield
    get_alarm_sink().clear()
    get_egress_log()._records.clear()  # noqa: SLF001
