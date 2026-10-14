from dorfsteig_control.mbmd_watchdog import (
    CHECK_EVERY_S,
    FIRST_PAUSE_S,
    GRACE_S,
    MAX_PAUSE_S,
    OFFLINE_CHECKS,
    Watchdog,
    any_device_online,
)


def run_offline_until_stop(watchdog: Watchdog, now: float) -> float:
    """Feed all-offline checks until the watchdog stops MBMD; return that time."""
    for _ in range(OFFLINE_CHECKS + GRACE_S // CHECK_EVERY_S + 1):
        if watchdog.step(now, mbmd_active=True, online=False) == "stop":
            return now
        now += CHECK_EVERY_S
    raise AssertionError("watchdog never stopped mbmd")


def test_any_device_online():
    assert any_device_online({"Meters": [{"Online": False}, {"Online": True}]})
    assert not any_device_online({"Meters": [{"Online": False}, {"Online": False}]})
    assert not any_device_online({"Meters": None})
    assert not any_device_online({})


def test_online_never_stops():
    watchdog = Watchdog()
    for i in range(20):
        assert watchdog.step(1000.0 + i * CHECK_EVERY_S, mbmd_active=True, online=True) is None


def test_stops_after_consecutive_offline_checks():
    watchdog = Watchdog()
    now = 1000.0
    for _ in range(OFFLINE_CHECKS - 1):
        assert watchdog.step(now, mbmd_active=True, online=False) is None
        now += CHECK_EVERY_S
    assert watchdog.step(now, mbmd_active=True, online=False) == "stop"
    assert watchdog.paused
    assert watchdog.resume_at == now + FIRST_PAUSE_S


def test_one_online_check_resets_the_count():
    watchdog = Watchdog()
    now = 1000.0
    for online in [False] * (OFFLINE_CHECKS - 1) + [True] + [False] * (OFFLINE_CHECKS - 1):
        assert watchdog.step(now, mbmd_active=True, online=online) is None
        now += CHECK_EVERY_S


def test_unknown_status_is_not_counted():
    watchdog = Watchdog()
    now = 1000.0
    for online in [False, None, None, None, False]:
        assert watchdog.step(now, mbmd_active=True, online=online) is None
        now += CHECK_EVERY_S


def test_leaves_mbmd_alone_when_stopped_by_hand():
    watchdog = Watchdog()
    for i in range(100):
        assert watchdog.step(1000.0 + i * CHECK_EVERY_S, mbmd_active=False, online=None) is None


def test_starts_after_pause_with_grace_and_doubling_backoff():
    watchdog = Watchdog()
    now = run_offline_until_stop(watchdog, 1000.0)

    # Paused: nothing until the pause is over, then start
    assert watchdog.step(now + FIRST_PAUSE_S - 1, mbmd_active=False, online=None) is None
    now += FIRST_PAUSE_S
    assert watchdog.step(now, mbmd_active=False, online=None) == "start"

    # Grace period: offline checks right after starting don't count
    for _ in range(GRACE_S // CHECK_EVERY_S):
        assert watchdog.step(now, mbmd_active=True, online=False) is None
        now += CHECK_EVERY_S

    # Still offline: the next pause is twice as long
    now = run_offline_until_stop(watchdog, now)
    assert watchdog.resume_at == now + 2 * FIRST_PAUSE_S


def test_pause_is_capped():
    watchdog = Watchdog()
    now = 1000.0
    for _ in range(10):
        now = run_offline_until_stop(watchdog, now)
        now = watchdog.resume_at
        assert watchdog.step(now, mbmd_active=False, online=None) == "start"
    assert watchdog.pause_s == MAX_PAUSE_S


def test_backoff_resets_when_online_again():
    watchdog = Watchdog()
    now = run_offline_until_stop(watchdog, 1000.0)
    now = watchdog.resume_at
    assert watchdog.step(now, mbmd_active=False, online=None) == "start"
    now += GRACE_S
    assert watchdog.step(now, mbmd_active=True, online=True) is None
    assert watchdog.pause_s == FIRST_PAUSE_S


def test_started_by_hand_during_pause():
    watchdog = Watchdog()
    now = run_offline_until_stop(watchdog, 1000.0)
    assert watchdog.step(now + CHECK_EVERY_S, mbmd_active=True, online=None) is None
    assert not watchdog.paused
