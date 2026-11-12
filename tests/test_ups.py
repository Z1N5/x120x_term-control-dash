import configparser
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import x120x_upsd as ups
import x120x_dashboard as dashboard
import x120x_control as control


def sample(**changes):
    data = dict(current_voltage=3.9, current_capacity=70,
                charger_present=True, charging_enabled=True, battery_temperature=None)
    data.update(changes)
    return data


class PolicyTests(unittest.TestCase):
    def make(self, **options):
        self.charge, self.shutdown = Mock(), Mock()
        self.cfg = ups.Settings(**options)
        return ups.UPSController(self.cfg, self.charge, self.shutdown)

    def test_observe_only_never_changes_charging_or_shuts_down(self):
        controller = self.make()
        for now, changes in enumerate((dict(current_capacity=95), dict(current_capacity=40),
                dict(charger_present=False, current_capacity=10),
                dict(charger_present=False, current_voltage=2.9),
                dict(charger_present=False, battery_temperature=60))):
            controller.tick(sample(**changes), now * 1000, 100)
        self.charge.assert_not_called()
        self.shutdown.assert_not_called()

    def test_unknown_charge_switch_in_monitor_mode_is_preserved_without_writes(self):
        controller = self.make()
        result = controller.tick(sample(charging_enabled=None), 0, 100)
        self.assertIsNone(result['charging_enabled'])
        self.assertIsNone(result['charger_charging'])
        self.charge.assert_not_called()
        self.shutdown.assert_not_called()

    def test_authorised_charging_initialises_unknown_switch_using_thresholds(self):
        for capacity, expected in ((70, True), (77, True), (79, False), (80, False), (90, False)):
            with self.subTest(capacity=capacity):
                controller = self.make(control_charging=True)
                result = controller.tick(sample(charging_enabled=None, current_capacity=capacity), 0, 100)
                self.charge.assert_called_once_with(expected)
                # A command is not a measured switch level.
                self.assertIsNone(result['charging_enabled'])

    def test_unknown_charge_switch_does_not_block_low_battery_shutdown(self):
        controller = self.make(automatic_shutdown=True)
        controller.tick(sample(charging_enabled=None, charger_present=False, current_capacity=20), 0, 100)
        self.shutdown.assert_called_once()
        self.charge.assert_not_called()

    def test_charge_limit_and_hysteresis(self):
        controller = self.make(control_charging=True)
        controller.tick(sample(current_capacity=80), 0, 10)
        self.charge.assert_called_once_with(False)
        self.charge.reset_mock()
        controller.tick(sample(current_capacity=79, charging_enabled=False), 2, 10)
        self.charge.assert_not_called()
        controller.tick(sample(current_capacity=77, charging_enabled=False), 4, 10)
        self.charge.assert_called_once_with(True)
        self.shutdown.assert_not_called()

    def test_voltage_ceiling_is_additional_charge_limit(self):
        controller = self.make(control_charging=True, max_voltage=4.0)
        controller.tick(sample(current_voltage=4.01), 0, 10)
        self.charge.assert_called_once_with(False)

    def test_warmup_finishes_and_allows_charge(self):
        controller = self.make(control_charging=True, warmup_time=10)
        report = controller.tick(sample(charging_enabled=True), 0, 5)
        self.assertFalse(report['warmup_complete'])
        self.charge.assert_called_once_with(False)
        report = controller.tick(sample(charging_enabled=False), 1, 10)
        self.assertTrue(report['warmup_complete'])
        self.charge.assert_called_with(True)

    def test_missing_cold_and_zero_degree_sensor_block_charge(self):
        for temp in (None, 0, 10, 51):
            with self.subTest(temp=temp):
                controller = self.make(control_charging=True,
                                       temperature_sensor_type='DHT22,4,NO_PULL_UP')
                controller.tick(sample(battery_temperature=temp), 0, 100)
                self.charge.assert_called_once_with(False)

    def test_outage_starts_even_at_full_charge(self):
        controller = self.make(automatic_shutdown=True)
        controller.tick(sample(charger_present=False, current_capacity=95), 0, 100)
        report = controller.tick(sample(charger_present=False, current_capacity=95), 300, 105)
        self.assertEqual(report['timer_no_power'], 300)
        self.assertEqual(report['shutdown_seconds_remaining'], 0)
        self.shutdown.assert_called_once()
        controller.tick(sample(charger_present=False, current_capacity=95), 600, 110)
        self.shutdown.assert_called_once()

    def test_return_of_power_resets_runtime_before_deadline(self):
        controller = self.make(automatic_shutdown=True)
        controller.tick(sample(charger_present=False), 0, 100)
        report = controller.tick(sample(), 299, 105)
        self.assertFalse(report['shutdown_initiated'])
        controller.tick(sample(), 900, 120)
        self.shutdown.assert_not_called()

    def test_restart_does_not_extend_battery_runtime(self):
        controller = self.make(automatic_shutdown=True)
        report = controller.tick(sample(charger_present=False), 100, 100)
        restarted = ups.UPSController(self.cfg, self.charge, self.shutdown, report)
        result = restarted.tick(sample(charger_present=False), 400, 105)
        self.assertEqual(result['shutdown_deadline_monotonic'], 400)
        self.shutdown.assert_called_once()

    def test_low_battery_thresholds_are_immediate_even_with_legacy_grace(self):
        for changes in (dict(current_capacity=20), dict(current_voltage=3.5)):
            with self.subTest(changes=changes):
                controller = self.make(automatic_shutdown=True, ac_max_downtime=60,
                                       shutdown_grace_seconds=300)
                report = controller.tick(sample(charger_present=False, **changes), 0, 100)
                self.shutdown.assert_called_once()
                self.assertEqual(report['shutdown_seconds_remaining'], 0)

    def test_runtime_is_configurable_and_has_no_additional_grace(self):
        controller = self.make(automatic_shutdown=True, ac_max_downtime=12.5)
        controller.tick(sample(charger_present=False), 0, 100)
        controller.tick(sample(charger_present=False), 749, 110)
        self.shutdown.assert_not_called()
        controller.tick(sample(charger_present=False), 750, 112.5)
        self.shutdown.assert_called_once()

    def test_power_restored_at_deadline_prevents_request(self):
        controller = self.make(automatic_shutdown=True)
        controller.tick(sample(charger_present=False), 0, 100)
        controller.tick(sample(current_capacity=10, current_voltage=3.4), 300, 105)
        self.shutdown.assert_not_called()

    def test_old_grace_deadline_does_not_delay_low_battery_after_upgrade(self):
        controller = self.make(automatic_shutdown=True)
        previous = dict(shutdown_deadline_monotonic=9999, startup_policy='standard')
        upgraded = ups.UPSController(self.cfg, self.charge, self.shutdown, previous)
        upgraded.tick(sample(charger_present=False, current_capacity=20), 10, 100)
        self.shutdown.assert_called_once()

    def test_old_startup_override_cannot_bypass_new_standard_policy(self):
        controller = self.make(automatic_shutdown=True, no_power_at_start='standard')
        previous = dict(startup_policy='run_till_protect')
        upgraded = ups.UPSController(self.cfg, self.charge, self.shutdown, previous)
        upgraded.tick(sample(charger_present=False, current_capacity=20), 10, 100)
        self.shutdown.assert_called_once()

    def test_restart_with_shutdown_disabled_cannot_inherit_action(self):
        controller = self.make()
        previous = dict(shutdown_deadline_monotonic=0, shutdown_reason='previous', startup_policy='standard')
        restarted = ups.UPSController(self.cfg, self.charge, self.shutdown, previous)
        result = restarted.tick(sample(charger_present=False), 10000, 100)
        self.assertFalse(result['shutdown_initiated'])
        self.shutdown.assert_not_called()

    def test_battery_start_minimums_still_checks_battery(self):
        controller = self.make(automatic_shutdown=True, no_power_at_start='run_till_minimums')
        result = controller.tick(sample(charger_present=False), 0, 100)
        self.assertIsNone(result['outage_limit_seconds'])
        result = controller.tick(sample(charger_present=False, current_capacity=19), 1000, 120)
        self.assertTrue(result['shutdown_initiated'])

    def test_battery_start_protect_skips_normal_minimums_but_not_emergency(self):
        controller = self.make(automatic_shutdown=True, no_power_at_start='run_till_protect')
        report = controller.tick(sample(charger_present=False, current_capacity=15, current_voltage=3.4), 0, 100)
        self.assertFalse(report['shutdown_initiated'])
        controller.tick(sample(charger_present=False, current_capacity=10, current_voltage=2.99), 10, 100)
        self.shutdown.assert_called_once()

    def test_battery_start_override_expires_on_power_return(self):
        controller = self.make(automatic_shutdown=True, no_power_at_start='run_till_protect')
        controller.tick(sample(charger_present=False), 0, 100)
        controller.tick(sample(), 10, 100)
        report = controller.tick(sample(charger_present=False, current_capacity=19), 20, 100)
        self.assertEqual(report['startup_policy'], 'standard')
        self.assertTrue(report['shutdown_initiated'])

    def test_emergency_shutdown_requires_separate_opt_in(self):
        controller = self.make(control_charging=True)
        report = controller.tick(sample(charger_present=False, current_voltage=2.99), 0, 100)
        self.assertIn('3.0V', report['shutdown_recommended'])
        self.shutdown.assert_not_called()


class DataTests(unittest.TestCase):
    def test_pi5_unconfigured_charge_pin_is_unknown_not_a_read_failure(self):
        output = ' 6: ip    pu | hi // GPIO6 = input\n16: no    pd | -- // GPIO16 = none\n'
        self.assertEqual(ups.parse_gpio(output), (True, None))
        self.assertEqual(ups.parse_gpio(output.replace('| hi', '| lo')), (False, None))

    def test_unconfigured_and_alternate_charge_modes_never_claim_a_switch_state(self):
        for state in ('no pd | --', 'no pd | hi', 'a0 pn | lo', 'op dh pn | --'):
            with self.subTest(state=state):
                self.assertEqual(ups.parse_gpio('6: ip pu | hi\n16: ' + state), (True, None))

    def test_realistic_pinctrl_output(self):
        output = ' 6: ip    pd | hi // GPIO6 = input\n16: op dh pn | hi // GPIO16 = output\n'
        self.assertEqual(ups.parse_gpio(output), (True, False))
        self.assertEqual(ups.parse_gpio(output.replace('| hi', '| lo')), (False, True))

    def test_invalid_gpio_output_and_output_mode_are_not_power_ok(self):
        for output in ('', '6: ip pd | --\n16: ip pd | hi', '6: op dh pn | hi\n16: ip pd | hi'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                ups.parse_gpio(output)

    def test_fuel_gauge_endianness(self):
        raw_voltage = 52480  # 4.1 V in fuel-gauge units
        raw_capacity = 80 * 256
        swap = lambda value: ((value & 255) << 8) | (value >> 8)
        hw = ups.Hardware.__new__(ups.Hardware)
        hw.bus, hw.sensor = Mock(), None
        hw.bus.read_word_data.side_effect = [swap(raw_voltage), swap(raw_capacity)]
        with patch.object(ups, 'command', return_value='6: ip pd | hi\n16: op dh pn | hi'):
            data = hw.read()
        self.assertAlmostEqual(data['current_voltage'], 4.1)
        self.assertEqual(data['current_capacity'], 80)
        self.assertFalse(data['charging_enabled'])

    def test_i2c_failure_is_not_fabricated_zero_battery(self):
        hw = ups.Hardware.__new__(ups.Hardware)
        hw.bus, hw.sensor = Mock(), None
        hw.bus.read_word_data.side_effect = OSError('I2C failed')
        with self.assertRaisesRegex(OSError, 'I2C failed'):
            hw.read()

    def test_atomic_report_and_cross_boot_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            data = dict(boot_id='first', shutdown_deadline_monotonic=400)
            ups.atomic_json(path, data)
            self.assertEqual(ups.restore_state(path, 'first'), data)
            self.assertIsNone(ups.restore_state(path, 'second'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_invalid_values_rejected(self):
        for values in (dict(max_charge_capacity=101), dict(min_voltage=2.5),
                       dict(poll_seconds=0), dict(ac_max_downtime=-1),
                       dict(warmup_time=float('nan')), dict(shutdown_grace_seconds=-1),
                       dict(no_power_at_start='typo'), dict(json_report_file='/tmp'),
                       dict(temperature_sensor_type='DHT22,16,NO_PULL_UP')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ups.Settings(**values).validate()

    def test_shipped_config_is_observe_only(self):
        cfg = ups.Settings.load(Path(__file__).resolve().parents[1] / 'x120x_upsd.ini')
        self.assertFalse(cfg.control_charging)
        self.assertFalse(cfg.automatic_shutdown)

    def test_legacy_config_does_not_silently_enable_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.ini'
            path.write_text('[general]\nmax_charge_capacity: 85\n')
            cfg = ups.Settings.load(path)
            self.assertEqual(cfg.max_charge_capacity, 85)
            self.assertFalse(cfg.control_charging)
            self.assertFalse(cfg.automatic_shutdown)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.ini'
        self.original = '[general]\n# preserve this comment\nmax_charge_capacity: 85\n[other]\nfoo=bar\n'
        self.path.write_text(self.original)

    def test_first_confirmation_cancel_leaves_everything_untouched(self):
        self.check_cancel(['no'])

    def test_second_confirmation_cancel_leaves_everything_untouched(self):
        self.check_cancel(['yes', 'yes'])

    def check_cancel(self, answers):
        apply = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            result = control.enable_charging(self.path, 80, Mock(side_effect=answers), apply)
        self.assertFalse(result)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)
        apply.assert_not_called()

    def test_two_confirmations_enable_only_charging_with_backup(self):
        apply = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.enable_charging(self.path, 80,
                Mock(side_effect=['yes', 'ENABLE CHARGING AT 80%']), apply))
        cfg = ups.Settings.load(self.path)
        self.assertTrue(cfg.control_charging)
        self.assertFalse(cfg.automatic_shutdown)
        self.assertEqual(cfg.max_charge_capacity, 80)
        text = self.path.read_text()
        self.assertIn('# preserve this comment', text)
        self.assertIn('[other]\nfoo=bar', text)
        backups = list(self.path.parent.glob('*.before-charging-*'))
        self.assertEqual(backups[0].read_text(), self.original)
        apply.assert_called_once()

    def test_charging_change_preserves_previously_enabled_shutdown(self):
        self.path.write_text('[general]\nautomatic_shutdown=true\n')
        with contextlib.redirect_stdout(io.StringIO()):
            control.enable_charging(self.path, 80,
                                    Mock(side_effect=['yes', 'ENABLE CHARGING AT 80%']))
        self.assertTrue(ups.Settings.load(self.path).automatic_shutdown)

    def test_shutdown_requires_both_confirmations_without_changing_charging(self):
        self.path.write_text(self.original.replace('[general]', '[general]\ncontrol_charging=true'))
        apply = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.enable_shutdown(self.path, 12.5,
                Mock(side_effect=['yes', 'ENABLE SHUTDOWN AFTER 12.5 MINUTES']), apply))
        cfg = ups.Settings.load(self.path)
        self.assertTrue(cfg.automatic_shutdown)
        self.assertTrue(cfg.control_charging)
        self.assertEqual(cfg.max_charge_capacity, 85)
        self.assertEqual(cfg.ac_max_downtime, 12.5)
        self.assertEqual(cfg.min_charge_capacity, 20)
        self.assertEqual(cfg.min_voltage, 3.5)
        self.assertEqual(cfg.no_power_at_start, 'standard')
        self.assertEqual(cfg.shutdown_grace_seconds, 0)
        apply.assert_called_once()
        self.assertEqual(len(list(self.path.parent.glob('*.before-shutdown-*'))), 1)

    def test_shutdown_first_or_second_cancellation_changes_nothing(self):
        for answers in (['no'], ['yes', 'no']):
            with self.subTest(answers=answers):
                apply = Mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(control.enable_shutdown(self.path, 5, Mock(side_effect=answers), apply))
                self.assertEqual(self.path.read_text(), self.original)
                self.assertEqual(len(list(self.path.parent.iterdir())), 1)
                apply.assert_not_called()

    def test_invalid_shutdown_runtime_never_prompts_or_saves(self):
        for minutes in (0, -1, float('nan'), float('inf')):
            ask = Mock()
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                control.enable_shutdown(self.path, minutes, ask)
            ask.assert_not_called()
        self.assertEqual(self.path.read_text(), self.original)

    def test_concurrent_config_edit_aborts_before_save(self):
        def ask(prompt):
            if '1 of 2' in prompt:
                return 'yes'
            self.path.write_text(self.original + '# concurrent edit\n')
            return 'ENABLE CHARGING AT 80%'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'changed during'):
            control.enable_charging(self.path, 80, ask)
        self.assertTrue(self.path.read_text().endswith('# concurrent edit\n'))


class DashboardTests(unittest.TestCase):
    def test_unknown_charging_switch_is_displayed_as_unknown(self):
        report, metrics = dashboard.demo_data()
        report['charging_enabled'] = None
        text = '\n'.join(line for line, _ in dashboard.rows(report, metrics))
        self.assertIn('Charging switch:   Unknown', text)

    def test_pmic_pairs_and_units(self):
        data = dashboard.parse_pmic('VDD_CORE_A current(0)=1.5A\nVDD_CORE_V volt(0)=0.8V\nEXT5V_V volt(1)=5.1V\n')
        self.assertAlmostEqual(data['pmic_watts'], 1.2)
        self.assertEqual(data['input_voltage'], 5.1)
        self.assertIsNone(dashboard.parse_pmic('')['pmic_watts'])

    def test_fan_sysfs_and_stopped_fan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hwmon = root / 'class/hwmon/hwmon7'
            hwmon.mkdir(parents=True)
            (hwmon / 'name').write_text('pwmfan\n')
            (hwmon / 'fan1_input').write_text('0\n')
            (hwmon / 'pwm1').write_text('75\n')
            with patch.object(dashboard, 'read_command', return_value=''):
                data = dashboard.pi_metrics(root)
            self.assertEqual(data['fan_rpm'], 0)
            self.assertAlmostEqual(data['fan_pwm_percent'], 75/255*100)

    def test_stale_status_never_claims_external_power_ok(self):
        report, pi = dashboard.demo_data()
        report['stale'] = True
        text = '\n'.join(line for line, _ in dashboard.rows(report, pi))
        self.assertIn('External power: UNKNOWN', text)
        self.assertNotIn('External power: OK', text)

    def test_missing_malformed_and_error_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            self.assertTrue(dashboard.read_status(path)['stale'])
            path.write_text('partial {')
            self.assertTrue(dashboard.read_status(path)['stale'])
            path.write_text('{"health":"error","error":"I2C failure"}')
            self.assertEqual(dashboard.read_status(path)['error'], 'I2C failure')

    def test_old_and_future_timestamps_are_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            for stamp in (0, 10000):
                path.write_text(json.dumps(dict(timestamp=stamp, health='ok')))
                self.assertTrue(dashboard.read_status(path, now=100)['stale'])

    def test_unavailable_metric_never_shown_as_zero(self):
        self.assertEqual(dashboard.fmt(None, ' RPM', 0), 'Unavailable')
        self.assertEqual(dashboard.fmt(0, ' RPM', 0), '0 RPM')


class ServiceTests(unittest.TestCase):
    def test_main_propagates_monitor_failure_as_nonzero_exit(self):
        with patch('sys.argv', ['x120x_upsd.py']), \
                patch.object(ups.Settings, 'load', return_value=ups.Settings()), \
                patch.object(ups, 'run_service', side_effect=OSError('hardware error')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ups.main(), 1)

    def test_failed_monitor_cycle_has_no_heartbeat_and_preserves_failure(self):
        self.run_fake_service(fail=True)

    def test_successful_cycle_heartbeats_and_closes_hardware(self):
        self.run_fake_service(fail=False)

    def run_fake_service(self, fail):
        with tempfile.TemporaryDirectory() as directory:
            cfg = ups.Settings(json_report_file=str(Path(directory)/'status.json'),
                               battery_report_schedule='')
            hw = Mock()
            hw.read.side_effect = OSError('I2C failed') if fail else [sample()]
            stop = threading.Event()
            def wait(_):
                stop.set()
            with patch.object(ups, 'boot_identity', return_value='boot'), \
                    patch.object(ups, 'minutes_since_boot', return_value=100), \
                    patch.object(ups.signal, 'signal'), \
                    patch.object(ups, 'notify') as notify, \
                    patch.object(stop, 'wait', side_effect=wait):
                if fail:
                    with self.assertRaisesRegex(OSError, 'I2C failed'):
                        ups.run_service(cfg, lambda _: hw, directory, stop)
                    self.assertNotIn(unittest.mock.call('WATCHDOG=1'), notify.call_args_list)
                    self.assertEqual(json.loads(Path(cfg.json_report_file).read_text())['health'], 'error')
                else:
                    ups.run_service(cfg, lambda _: hw, directory, stop)
                    self.assertIn(unittest.mock.call('WATCHDOG=1'), notify.call_args_list)
                    self.assertEqual(json.loads(Path(cfg.json_report_file).read_text())['health'], 'stopped')
            hw.close.assert_called_once()
            hw.set_charging.assert_not_called()


if __name__ == '__main__':
    unittest.main()
