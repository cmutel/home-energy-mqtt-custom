"""Give the SolarEdge inverter a rest when MBMD can't reach it.

When the inverter's Modbus TCP server stops answering, MBMD keeps trying to
reconnect every second or two, forever. The inverter accepts only one Modbus
connection, and a stream of half-open connections seems to keep it from
recovering (October 2026: offline for two days until MBMD was stopped).

This watchdog checks MBMD's status API once a minute. If every device has
been offline for several checks in a row, it stops the `mbmd` service, waits,
and starts it again. The wait doubles after each failed attempt (5, 10, 20,
40, then 60 minutes) and resets once MBMD reports a device online.

The watchdog only ever starts MBMD after stopping it itself. If you stop
`mbmd` by hand (to reset the inverter, say), it stays stopped. A marker file
in /run remembers a pause across watchdog restarts, so updating the watchdog
while MBMD is paused doesn't leave MBMD stopped for good.

Runs as root (it calls `systemctl stop/start mbmd`):

    sudo systemctl enable --now dorfsteig-mbmd-watchdog
    journalctl -u dorfsteig-mbmd-watchdog -f
"""

import argparse
import json
import logging
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dorfsteig_core.logs import setup_logging

log = logging.getLogger(__name__)

STATUS_URL = "http://127.0.0.1:8090/api/status"
SERVICE = "mbmd"
PAUSE_MARKER = Path("/run/dorfsteig-mbmd-watchdog/paused")

CHECK_EVERY_S = 60
OFFLINE_CHECKS = 3          # consecutive all-offline checks before MBMD is stopped
GRACE_S = 120               # after starting MBMD, give it this long to connect
FIRST_PAUSE_S = 5 * 60
MAX_PAUSE_S = 60 * 60


@dataclass
class Watchdog:
    """Decides when to stop and start MBMD. Pure logic, no I/O, so it can be tested.

    Call `step` once per check. It returns "stop", "start" or None, and the
    caller carries that out.
    """

    paused: bool = False             # True while MBMD is stopped by us
    resume_at: float = 0.0           # when to start MBMD again (only while paused)
    pause_s: float = FIRST_PAUSE_S   # length of the next pause
    started_at: float = 0.0          # when we last started MBMD (for the grace period)
    offline_checks: int = 0

    def step(self, now: float, mbmd_active: bool, online: bool | None) -> str | None:
        """One check.

        `mbmd_active`: whether the systemd service is running.
        `online`: True if MBMD reports any device online, False if all are
        offline, None if the status API didn't answer.
        """
        if self.paused:
            if mbmd_active:
                # Someone started MBMD by hand: hand control back to normal watching
                log.info("mbmd was started by hand during the pause; watching again")
                self.paused = False
                self.started_at = now
                self.offline_checks = 0
                return None
            if now >= self.resume_at:
                self.paused = False
                self.started_at = now
                self.offline_checks = 0
                return "start"
            return None

        if not mbmd_active:
            # Stopped by hand (or crashed and systemd is restarting it): leave it alone
            self.offline_checks = 0
            return None

        if online is None or now - self.started_at < GRACE_S:
            return None

        if online:
            if self.offline_checks or self.pause_s != FIRST_PAUSE_S:
                log.info("mbmd reports a device online again")
            self.offline_checks = 0
            self.pause_s = FIRST_PAUSE_S
            return None

        self.offline_checks += 1
        if self.offline_checks < OFFLINE_CHECKS:
            return None

        self.paused = True
        self.resume_at = now + self.pause_s
        log.warning("all mbmd devices offline for %d checks; stopping mbmd for %.0f min",
                    self.offline_checks, self.pause_s / 60)
        self.pause_s = min(self.pause_s * 2, MAX_PAUSE_S)
        self.offline_checks = 0
        return "stop"


def any_device_online(status: dict) -> bool:
    """Whether MBMD's /api/status lists at least one device as online."""
    for meter in status.get("Meters") or []:
        if meter.get("Online"):
            return True
    return False


def read_online() -> bool | None:
    """Ask MBMD whether any device is online; None if the API doesn't answer."""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=10) as response:
            status = json.load(response)
    except (OSError, ValueError) as error:
        log.debug("mbmd status API not available: %s", error)
        return None
    return any_device_online(status)


def service_active() -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=False)
    return result.returncode == 0


def systemctl(action: str) -> None:
    log.info("systemctl %s %s", action, SERVICE)
    subprocess.run(["systemctl", action, SERVICE], check=False)


def run() -> None:
    watchdog = Watchdog()
    if PAUSE_MARKER.exists():
        # We paused MBMD before the watchdog was restarted: start it on the first check
        log.info("found pause marker; mbmd will be started")
        watchdog.paused = True
        watchdog.resume_at = 0.0

    while True:
        action = watchdog.step(time.time(), service_active(), read_online())
        if action == "stop":
            PAUSE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            PAUSE_MARKER.touch()
            systemctl("stop")
        elif action == "start":
            systemctl("start")
        if not watchdog.paused:
            PAUSE_MARKER.unlink(missing_ok=True)
        time.sleep(CHECK_EVERY_S)


def main() -> None:
    parser = argparse.ArgumentParser(prog="dorfsteig-mbmd-watchdog",
                                     description="Pause MBMD while the inverter is unreachable")
    parser.parse_args()
    setup_logging()
    try:
        run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
