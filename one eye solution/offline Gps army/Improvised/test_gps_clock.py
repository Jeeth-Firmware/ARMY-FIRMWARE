import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import gps_clock


class _Runner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.returncode, stderr="")

    @property
    def date_calls(self):
        return [c for c in self.calls if "date" in c]


def _utc(offset_seconds=0.0):
    return datetime.fromtimestamp(time.time() + offset_seconds, timezone.utc)


class GpsClockSyncTest(unittest.TestCase):
    def test_small_drift_leaves_clock_alone(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        self.assertFalse(sync.offer(_utc(5)))
        self.assertEqual(runner.calls, [])

    def test_large_drift_sets_clock_after_second_matching_fix(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        eight_hours = 8 * 3600
        self.assertFalse(sync.offer(_utc(eight_hours)))
        self.assertEqual(runner.date_calls, [])
        self.assertTrue(sync.offer(_utc(eight_hours + 0.2)))

        cmd = runner.date_calls[0]
        self.assertEqual(cmd[:4], ["sudo", "-n", "date", "-s"])
        self.assertAlmostEqual(float(cmd[4].lstrip("@")), time.time() + eight_hours, delta=2)
        self.assertIn(["sudo", "-n", "fake-hwclock", "save"], runner.calls)

    def test_inconsistent_fixes_do_not_move_clock(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        self.assertFalse(sync.offer(_utc(3600)))
        self.assertFalse(sync.offer(_utc(-7200)))
        self.assertEqual(runner.date_calls, [])

    def test_implausible_year_is_ignored(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        rollover = datetime(2006, 3, 1, tzinfo=timezone.utc)
        sync.offer(rollover)
        sync.offer(rollover + timedelta(seconds=1))
        self.assertEqual(runner.calls, [])

    def test_backs_off_after_failure(self):
        runner = _Runner(returncode=1)
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        sync.offer(_utc(-3 * 3600))
        self.assertFalse(sync.offer(_utc(-3 * 3600)))
        self.assertEqual(len(runner.date_calls), 1)
        sync.offer(_utc(-3 * 3600))
        sync.offer(_utc(-3 * 3600))
        self.assertEqual(len(runner.date_calls), 1)

    def test_rechecks_once_a_minute_after_sync(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=True, runner=runner)
        sync.offer(_utc(600))
        sync.offer(_utc(600))
        sync.offer(_utc(600))
        sync.offer(_utc(600))
        self.assertEqual(len(runner.date_calls), 1)
        with mock.patch.object(gps_clock.time, "monotonic", return_value=time.monotonic() + 61):
            sync.offer(_utc(600))
            sync.offer(_utc(600))
        self.assertEqual(len(runner.date_calls), 2)

    def test_disabled_does_nothing(self):
        runner = _Runner()
        sync = gps_clock.GpsClockSync(enabled=False, runner=runner)
        sync.offer(_utc(9999))
        sync.offer(_utc(9999))
        self.assertEqual(runner.calls, [])


def _nmea(body):
    import pynmea2
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return pynmea2.parse(f"${body}*{checksum:02X}")


class ParseTest(unittest.TestCase):
    def test_rmc_with_fix(self):
        msg = _nmea("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,201026,003.1,W")
        self.assertEqual(gps_clock.utc_from_rmc(msg), datetime(2026, 10, 20, 12, 35, 19, tzinfo=timezone.utc))

    def test_rmc_without_fix(self):
        msg = _nmea("GPRMC,123519,V,,,,,,,201026,,,N")
        self.assertIsNone(gps_clock.utc_from_rmc(msg))

    def test_gpsd_tpv(self):
        tpv = {"mode": 3, "time": "2026-10-20T12:35:19.000Z"}
        self.assertEqual(gps_clock.utc_from_gpsd_tpv(tpv), datetime(2026, 10, 20, 12, 35, 19, tzinfo=timezone.utc))
        self.assertIsNone(gps_clock.utc_from_gpsd_tpv({"mode": 1, "time": "2026-10-20T12:35:19.000Z"}))
        self.assertIsNone(gps_clock.utc_from_gpsd_tpv({"mode": "n/a", "time": "n/a"}))


if __name__ == "__main__":
    unittest.main()
