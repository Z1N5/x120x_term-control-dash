import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from x120x_battery_health import BatteryPerformance, MAX_HISTORY
import x120x_dashboard as dashboard
import x120x_upsd as ups


def report(capacity=80, present=False, **changes):
    value = dict(current_capacity=capacity, current_voltage=3.9, charger_present=present,
                 health='ok', timestamp=1800000000, stale=False)
    value.update(changes)
    return value


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'battery.json'
        self.monitor = BatteryPerformance(self.path)
        self.now = 0

    def run_battery(self, start=80, end=60, minutes=60, finish=True):
        steps = minutes * 2
        for step in range(steps + 1):
            capacity = start + (end - start) * step / steps
            self.monitor.observe(report(capacity, timestamp=1800000000 + self.now), self.now)
            self.now += 30
        if finish:
            self.monitor.observe(report(99, True), self.now)
            self.now += 30

    def test_empty_history_and_external_power_do_not_invent_health_or_write(self):
        for now in range(10):
            self.monitor.observe(report(present=True), now)
        self.assertEqual(self.monitor.status()['trend'], 'learning')
        self.assertEqual(self.monitor.status()['sessions'], 0)
        self.assertFalse(self.path.exists())

    def test_short_or_shallow_runs_are_not_used(self):
        self.run_battery(minutes=10)
        self.run_battery(end=75)
        self.assertEqual(self.monitor.status()['sessions'], 0)
        self.assertFalse(self.path.exists())

    def test_records_measured_segment_not_restored_charge_reading(self):
        self.run_battery()
        last = self.monitor.status()['last_run']
        self.assertEqual(last['seconds'], 3600)
        self.assertEqual((last['start'], last['end']), (80, 60))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_preserves_history_across_restart_but_never_joins_live_segments(self):
        self.run_battery()
        self.run_battery(finish=False)
        restarted = BatteryPerformance(self.path)
        self.assertEqual(restarted.status()['sessions'], 1)
        self.assertIsNone(restarted.active)
        self.assertEqual(restarted.baseline, self.monitor.baseline)

    def test_orderly_stop_can_record_qualified_segment_once(self):
        self.run_battery(finish=False)
        self.monitor.finish()
        self.monitor.finish()
        self.assertEqual(self.monitor.status()['sessions'], 1)

    def test_gap_duplicate_time_and_clock_reversal_discard_active_segment(self):
        for delta in (0, -1, 61):
            with self.subTest(delta=delta):
                self.monitor = BatteryPerformance(self.path)
                self.monitor.observe(report(80), 100)
                self.monitor.observe(report(70), 100 + delta)
                self.monitor.finish()
                self.assertEqual(self.monitor.status()['sessions'], 0)

    def test_invalid_samples_clear_active_history_without_counting_gaps(self):
        invalid = [dict(health='error'), dict(stale=True), dict(current_capacity=float('nan')),
                   dict(current_capacity=101), dict(current_capacity=True), dict(charger_present=None),
                   dict(current_voltage=0), dict(timestamp=float('inf'))]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.monitor.observe(report(), 1)
                self.monitor.observe(report(**changes), 2)
                self.assertIsNone(self.monitor.active)

    def test_gauge_rebound_or_large_step_starts_new_segment(self):
        for capacity in (83, 70):
            self.monitor.observe(report(80), 10)
            self.monitor.observe(report(capacity), 20)
            self.assertEqual(self.monitor.active['start'], capacity)
            self.assertEqual(self.monitor.active['first'], 20)
            self.monitor.active = None
            self.monitor.last_now = None

    def test_small_gauge_rebound_does_not_count_as_added_capacity(self):
        self.monitor.observe(report(80), 0)
        self.monitor.observe(report(79), 30)
        self.monitor.observe(report(80), 60)
        self.assertEqual(self.monitor.active['end'], 79)

    def test_needs_three_baseline_and_three_later_comparable_runs(self):
        for _ in range(5):
            self.run_battery()
            self.assertEqual(self.monitor.status()['trend'], 'learning')
        self.run_battery()
        self.assertEqual(self.monitor.status()['trend'], 'similar')
        self.assertEqual(self.monitor.status()['comparison_runs'], 3)

    def test_declining_and_longer_performance_are_distinguished_without_health_percentage(self):
        for _ in range(3):
            self.run_battery()
        original_baseline = list(self.monitor.baseline)
        for _ in range(3):
            self.run_battery(minutes=40)
        self.assertEqual(self.monitor.status()['trend'], 'shorter')
        for _ in range(3):
            self.run_battery(minutes=90)
        self.assertEqual(self.monitor.status()['trend'], 'longer')
        self.assertEqual(self.monitor.baseline, original_baseline)
        self.assertNotIn('health_percent', self.monitor.status())

    def test_different_charge_ranges_do_not_claim_decline(self):
        for _ in range(3):
            self.run_battery()
        for _ in range(3):
            self.run_battery(start=50, end=30, minutes=30)
        self.assertEqual(self.monitor.status()['trend'], 'learning')
        self.assertEqual(self.monitor.status()['comparison_runs'], 0)

    def test_new_charge_range_cannot_reuse_an_old_positive_trend(self):
        for _ in range(6):
            self.run_battery()
        self.assertEqual(self.monitor.status()['trend'], 'similar')
        self.run_battery(start=100, end=60)
        self.assertEqual(self.monitor.status()['trend'], 'learning')

    def test_bounded_history_keeps_original_baseline(self):
        for _ in range(MAX_HISTORY + 3):
            self.run_battery(minutes=15)
        self.assertEqual(len(self.monitor.history), MAX_HISTORY)
        self.assertEqual(len(self.monitor.baseline), 3)
        restarted = BatteryPerformance(self.path)
        self.assertEqual(restarted.total, MAX_HISTORY + 3)
        self.assertEqual(restarted.baseline, self.monitor.baseline)

    def test_corrupt_history_is_not_silently_overwritten(self):
        for content in ('broken', '[]', '{"schema":1,"history":[{}],"baseline":[]}'):
            self.path.write_text(content)
            with self.assertRaises(ValueError):
                BatteryPerformance(self.path)
            self.assertEqual(self.path.read_text(), content)

    def test_unsafe_directory_is_rejected(self):
        self.path.parent.chmod(0o755)
        try:
            with self.assertRaisesRegex(ValueError, 'private'):
                self.run_battery()
        finally:
            self.path.parent.chmod(0o700)

    def test_observe_does_not_mutate_report_or_configuration(self):
        value = report(control_charging=True, automatic_shutdown=True, outage_limit_seconds=6600)
        original = dict(value)
        self.monitor.observe(value, 10)
        self.assertEqual(value, original)

    def test_unknown_private_fields_never_reach_public_status(self):
        self.run_battery()
        data = json.loads(self.path.read_text())
        data['history'][0]['private_note'] = 'do not publish'
        self.path.write_text(json.dumps(data))
        restarted = BatteryPerformance(self.path)
        self.assertNotIn('private_note', json.dumps(restarted.status()))


class DashboardAndServiceTests(unittest.TestCase):
    def run_service_with_tracker(self, tracker, low_battery=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            settings = ups.Settings(json_report_file=str(path / 'status.json'), battery_report_schedule='',
                                    automatic_shutdown=low_battery, control_charging=low_battery)
            original_settings = vars(settings).copy()
            hardware = Mock()
            hardware.read.return_value = dict(current_voltage=3.4 if low_battery else 3.9,
                                             current_capacity=10 if low_battery else 70,
                                             charger_present=not low_battery, charging_enabled=False,
                                             battery_temperature=None)
            stop = threading.Event()
            with patch('x120x_battery_health.BatteryPerformance', return_value=tracker), \
                    patch.object(ups, 'boot_identity', return_value='test-boot'), \
                    patch.object(ups, 'minutes_since_boot', return_value=100), \
                    patch.object(ups.signal, 'signal'), patch.object(ups, 'notify') as notify, \
                    patch.object(stop, 'wait', side_effect=lambda _: stop.set()), \
                    patch.object(ups, 'request_shutdown') as shutdown, \
                    contextlib.redirect_stdout(io.StringIO()):
                ups.run_service(settings, lambda _: hardware, directory, stop, path / 'history.json')
            self.assertEqual(vars(settings), original_settings)
            self.assertIn(unittest.mock.call('WATCHDOG=1'), notify.call_args_list)
            saved = json.loads((path / 'status.json').read_text())
            return saved, hardware, shutdown

    def test_history_write_failure_cannot_block_authorised_shutdown_or_charging(self):
        tracker = Mock()
        tracker.observe.side_effect = OSError('disk full')
        saved, hardware, shutdown = self.run_service_with_tracker(tracker, low_battery=True)
        self.assertEqual(saved['battery_performance']['state'], 'storage_error')
        self.assertEqual(saved['health'], 'stopped')
        hardware.set_charging.assert_called_once_with(True)
        shutdown.assert_called_once()

    def test_monitor_receives_healthy_samples_and_no_control_hooks(self):
        tracker = Mock()
        tracker.status.return_value = dict(state='idle', trend='learning', sessions=0)
        saved, hardware, shutdown = self.run_service_with_tracker(tracker)
        self.assertEqual(saved['battery_performance']['trend'], 'learning')
        tracker.observe.assert_called_once()
        self.assertEqual(tracker.observe.call_args.args[0]['current_capacity'], 70)
        hardware.set_charging.assert_not_called()
        shutdown.assert_not_called()
        tracker.finish.assert_called_once()

    def test_dashboard_separates_charge_health_and_timer(self):
        value, pi = dashboard.demo_data()
        value['battery_performance'] = dict(state='idle', trend='shorter', sessions=6,
                                          baseline_runs=3, comparison_runs=3)
        text = '\n'.join(line for line, _ in dashboard.rows(value, pi))
        for expected in ('Battery charge:', 'BATTERY HEALTH / BACKUP PERFORMANCE',
                         'Not measurable', 'possible decline or higher load', 'Shutdown timer limit:'):
            self.assertIn(expected, text)
        self.assertNotIn('Battery health: 80', text)

    def test_stale_or_missing_history_does_not_claim_a_live_trend(self):
        value, pi = dashboard.demo_data()
        value['battery_performance'] = dict(state='idle', trend='similar')
        value['stale'] = True
        text = '\n'.join(line for line, _ in dashboard.rows(value, pi))
        self.assertIn('Passive monitoring: UNVERIFIED', text)
        self.assertNotIn('Similar backup performance', text)
        del value['battery_performance']
        self.assertIn('update the service', '\n'.join(line for line, _ in dashboard.rows(value, pi)))

    def test_failed_history_does_not_stop_service_or_change_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'bad-history.json').write_text('invalid')
            settings = ups.Settings(json_report_file=str(path / 'status.json'), battery_report_schedule='')
            hardware = Mock()
            hardware.read.return_value = dict(current_voltage=3.9, current_capacity=70,
                                             charger_present=True, charging_enabled=True,
                                             battery_temperature=None)
            stop = threading.Event()
            with patch.object(ups, 'boot_identity', return_value='test-boot'), \
                    patch.object(ups, 'minutes_since_boot', return_value=100), \
                    patch.object(ups.signal, 'signal'), patch.object(ups, 'notify') as notify, \
                    patch.object(stop, 'wait', side_effect=lambda _: stop.set()), \
                    patch.object(ups, 'request_shutdown') as shutdown, \
                    contextlib.redirect_stdout(io.StringIO()):
                ups.run_service(settings, lambda _: hardware, directory, stop, path / 'bad-history.json')
            saved = json.loads((path / 'status.json').read_text())
            self.assertEqual(saved['battery_performance']['state'], 'unavailable')
            self.assertEqual(saved['health'], 'stopped')
            self.assertIn(unittest.mock.call('WATCHDOG=1'), notify.call_args_list)
            hardware.set_charging.assert_not_called()
            shutdown.assert_not_called()
            self.assertFalse(settings.control_charging)
            self.assertFalse(settings.automatic_shutdown)


if __name__ == '__main__':
    unittest.main()
