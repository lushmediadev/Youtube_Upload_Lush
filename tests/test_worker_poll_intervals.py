from types import SimpleNamespace

from workers.agent.main import (
    _browser_session_poll_interval_seconds,
    _decommission_poll_interval_seconds,
    _should_run_periodic,
)


def test_decommission_poll_interval_uses_dedicated_config() -> None:
    config = SimpleNamespace(decommission_poll_seconds=60.0)

    assert _decommission_poll_interval_seconds(config) == 60.0


def test_browser_session_poll_uses_slow_interval_when_idle() -> None:
    config = SimpleNamespace(browser_session_poll_seconds=15.0, poll_seconds=5.0)
    browser_sessions = SimpleNamespace(local_sessions={})

    assert _browser_session_poll_interval_seconds(config, browser_sessions) == 15.0


def test_browser_session_poll_stays_fast_for_running_local_sessions() -> None:
    config = SimpleNamespace(browser_session_poll_seconds=15.0, poll_seconds=5.0)
    browser_sessions = SimpleNamespace(local_sessions={"session-1": {}})

    assert _browser_session_poll_interval_seconds(config, browser_sessions) == 5.0


def test_periodic_helper_runs_immediately_then_after_interval() -> None:
    assert _should_run_periodic(now=100.0, last_run_at=0.0, interval_seconds=60.0)
    assert not _should_run_periodic(now=120.0, last_run_at=100.0, interval_seconds=60.0)
    assert _should_run_periodic(now=160.0, last_run_at=100.0, interval_seconds=60.0)
