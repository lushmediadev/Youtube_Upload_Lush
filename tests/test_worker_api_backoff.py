from types import SimpleNamespace
from unittest.mock import patch

from workers.agent import control_plane
from workers.agent import main as worker_main


def test_retry_delay_adds_configured_jitter():
    config = SimpleNamespace(
        network_retry_base_seconds=3.0,
        network_retry_max_seconds=30.0,
        network_retry_jitter_seconds=2.0,
    )

    with patch.object(control_plane.random, "uniform", return_value=1.25) as uniform_mock:
        assert control_plane._retry_delay_seconds(config, 1) == 4.25

    uniform_mock.assert_called_once_with(0.0, 2.0)


def test_busy_live_worker_throttles_noop_claim_probe():
    config = SimpleNamespace(live_busy_claim_interval_seconds=10.0)

    assert worker_main._should_probe_live_claim(
        config,
        active_count=1,
        last_noop_probe_at=100.0,
        now=105.0,
    ) is False
    assert worker_main._should_probe_live_claim(
        config,
        active_count=1,
        last_noop_probe_at=100.0,
        now=110.0,
    ) is True
    assert worker_main._should_probe_live_claim(
        config,
        active_count=0,
        last_noop_probe_at=120.0,
        now=121.0,
    ) is True
