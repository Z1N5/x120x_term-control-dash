import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import x120x_control as control
import x120x_dashboard as dashboard
import x120x_upsd as ups


def screen_with_keys(keys, size=(12, 80)):
    screen = Mock()
    screen.getmaxyx.return_value = size
    screen.getch.side_effect = keys
    return screen


class ChargeTargetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'settings.ini'
        self.original = ('[general]\n# retain comment\nmax_charge_capacity=80\nmax_voltage=4.1\n'
                         'min_charge_capacity=20\nmin_voltage=3.5\nac_max_downtime=110\n'
                         'automatic_shutdown=true\ncontrol_charging=true\ninitialise_power_input=true\n'
                         'telegram_enabled=true\ntelegram_heartbeat_enabled=true\n'
                         'telegram_heartbeat_hours=6\ntelegram_heartbeat_start=09:00\n'
                         '[other]\nkeep=value\n')
        self.path.write_text(self.original)

    def test_double_confirm_saves_target_only_and_backup(self):
        before = ups.Settings.load(self.path)
        apply = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.set_charge_target(self.path, 100,
                Mock(side_effect=['yes', 'SET CHARGE TARGET TO 100%']), apply))
        after = ups.Settings.load(self.path)
        before.max_charge_capacity = 100
        self.assertEqual(vars(after), vars(before))
        self.assertEqual(self.path.read_text(), self.original.replace('max_charge_capacity=80',
                                                                    'max_charge_capacity = 100'))
        backups = list(self.path.parent.glob('*.before-charge-target-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        apply.assert_called_once()

    def test_target_change_cannot_enable_charging(self):
        self.path.write_text(self.original.replace('control_charging=true', 'control_charging=false'))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(control.set_charge_target(self.path, 85,
                Mock(side_effect=['yes', 'SET CHARGE TARGET TO 85%'])))
        self.assertFalse(ups.Settings.load(self.path).control_charging)
        self.assertIn('must be enabled separately', out.getvalue())

    def test_either_cancellation_leaves_config_and_service_untouched(self):
        for answers in (['no'], ['yes', 'wrong']):
            apply = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(control.set_charge_target(self.path, 100, Mock(side_effect=answers), apply))
            self.assertEqual(self.path.read_text(), self.original)
            self.assertEqual(len(list(self.path.parent.iterdir())), 1)
            apply.assert_not_called()

    def test_invalid_target_and_incompatible_cutoff_fail_before_confirmation(self):
        for target in (0, 49, 101, 80.5, float('nan'), float('inf'), True, '100'):
            ask = Mock()
            with self.subTest(target=target), self.assertRaises(ValueError):
                control.set_charge_target(self.path, target, ask)
            ask.assert_not_called()
        self.path.write_text(self.original.replace('min_charge_capacity=20', 'min_charge_capacity=60'))
        ask = Mock()
        with self.assertRaises(ValueError):
            control.set_charge_target(self.path, 50, ask)
        ask.assert_not_called()

    def test_concurrent_edit_is_not_overwritten(self):
        def ask(prompt):
            if '1 of 2' in prompt:
                return 'yes'
            self.path.write_text(self.original + '# concurrent edit\n')
            return 'SET CHARGE TARGET TO 100%'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            control.set_charge_target(self.path, 100, ask)
        self.assertTrue(self.path.read_text().endswith('# concurrent edit\n'))

    def test_existing_controller_uses_new_target_and_three_point_hysteresis(self):
        cfg = ups.Settings(control_charging=True, max_charge_capacity=100)
        charge = Mock()
        controller = ups.UPSController(cfg, charge, Mock())
        def sample(capacity, enabled):
            return dict(current_capacity=capacity, current_voltage=4.0, charger_present=True,
                        charging_enabled=enabled, battery_temperature=None)
        controller.tick(sample(96, False), 0, 100)
        charge.assert_called_once_with(True)
        charge.reset_mock()
        controller.tick(sample(98, False), 1, 100)
        charge.assert_not_called()
        controller.tick(sample(100, True), 2, 100)
        charge.assert_called_once_with(False)

    def test_cli_dispatch_uses_target_only_action_and_cancellation_code(self):
        for completed, code in ((True, 0), (False, 2)):
            with patch('sys.argv', ['x120x-control', 'set-charge-target', '--limit', '100']), \
                    patch.object(control.sys.stdin, 'isatty', return_value=True), \
                    patch.object(control.os, 'geteuid', return_value=0), \
                    patch.object(control, 'set_charge_target', return_value=completed) as helper, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(control.main(), code)
            helper.assert_called_once_with('/usr/local/etc/x120x_upsd.ini', 100,
                                           apply_service=control.restart_service)

    def test_eof_during_confirmation_cannot_report_success(self):
        with patch('sys.argv', ['x120x-control', 'set-charge-target', '--limit', '100']), \
                patch.object(control.sys.stdin, 'isatty', return_value=True), \
                patch.object(control.os, 'geteuid', return_value=0), \
                patch.object(control, 'set_charge_target', side_effect=EOFError), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(control.main(), 2)


class ChargeMenuTests(unittest.TestCase):
    def setUp(self):
        self.report, _ = dashboard.demo_data()

    def test_main_menu_exposes_charge_target_without_applying(self):
        screen = screen_with_keys([ord('5')])
        self.assertEqual(dashboard.settings_menu(screen, self.report), 'charge-target')
        rendered = [call.args[2] for call in screen.addnstr.call_args_list]
        self.assertTrue(any('Battery charge target' in line for line in rendered))
        self.assertTrue(any('Esc: back' in line for line in rendered))

    def test_presets_include_100_and_unchanged_target_does_nothing(self):
        for key, expected in (('1', None), ('2', 85), ('3', 90), ('4', 100)):
            self.assertEqual(dashboard.charge_target_menu(screen_with_keys([ord(key)]), self.report), expected)

    def test_custom_target_and_invalid_value_correction(self):
        self.assertEqual(dashboard.charge_target_menu(
            screen_with_keys([ord('5'), ord('9'), ord('5'), 10]), self.report), 95)
        self.assertEqual(dashboard.charge_target_menu(
            screen_with_keys([ord('5'), ord('4'), ord('9'), 10, 127, 127, ord('5'), ord('0'), 10]),
            self.report), 50)

    def test_escape_demo_and_small_terminal_do_not_apply(self):
        for keys in ([27], [ord('5'), ord('9'), 27]):
            self.assertIsNone(dashboard.charge_target_menu(screen_with_keys(keys), self.report))
        self.assertIsNone(dashboard.settings_menu(screen_with_keys([ord('5'), 10]), self.report, demo=True))
        self.assertIsNone(dashboard.charge_target_menu(screen_with_keys([ord('4'), 10], (6, 30)), self.report))

    def test_fixed_privileged_command_and_validation(self):
        self.assertEqual(dashboard.charge_target_command(100, is_root=False),
                         ['sudo', '--', dashboard.SETTINGS_HELPER, 'set-charge-target', '--limit', '100'])
        for target in (49, 101, 80.5, float('nan'), True):
            with self.assertRaises(ValueError):
                dashboard.charge_target_command(target)

    def test_menu_dispatch_and_return_from_double_confirmation_helper(self):
        screen = Mock()
        with patch.object(dashboard, 'settings_menu', return_value='charge-target'), \
                patch.object(dashboard, 'charge_target_menu', return_value=100), \
                patch.object(dashboard.Path, 'is_file', return_value=True), \
                patch.object(dashboard.curses, 'def_prog_mode'), patch.object(dashboard.curses, 'endwin'), \
                patch.object(dashboard.curses, 'reset_prog_mode') as restore, \
                patch.object(dashboard.subprocess, 'run', return_value=Mock(returncode=2)), \
                patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertIn('cancelled', dashboard.open_settings(screen, self.report))
        restore.assert_called_once()
        screen.clearok.assert_called_once_with(True)


if __name__ == '__main__':
    unittest.main()
