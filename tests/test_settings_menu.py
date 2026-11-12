import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import x120x_control as control
import x120x_dashboard as dashboard
import x120x_upsd as ups


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'settings.ini'
        self.original = ('[general]\n# keep this comment\nac_max_downtime=5\n'
                         'min_charge_capacity=25\nmin_voltage=3.6\nmax_charge_capacity=85\n'
                         'automatic_shutdown=false\ncontrol_charging=true\ntelegram_enabled=true\n'
                         'initialise_power_input=true\n[other]\nkeep=value\n')
        self.path.write_text(self.original)

    def test_both_confirmations_update_time_only_and_back_up(self):
        apply = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.set_shutdown_time(self.path,120,
                Mock(side_effect=['yes','SET SHUTDOWN TIMER TO 120 MINUTES']),apply))
        cfg = ups.Settings.load(self.path)
        self.assertEqual(cfg.ac_max_downtime,120)
        self.assertEqual(cfg.min_charge_capacity,25)
        self.assertEqual(cfg.min_voltage,3.6)
        self.assertEqual(cfg.max_charge_capacity,85)
        self.assertFalse(cfg.automatic_shutdown)
        self.assertTrue(cfg.control_charging)
        self.assertTrue(cfg.telegram_enabled)
        self.assertTrue(cfg.initialise_power_input)
        self.assertEqual(self.path.read_text(),self.original.replace('ac_max_downtime=5','ac_max_downtime = 120'))
        backup=list(self.path.parent.glob('*.before-shutdown-time-*'))
        self.assertEqual(len(backup),1)
        self.assertEqual(backup[0].read_text(),self.original)
        apply.assert_called_once()

    def test_first_or_second_cancellation_never_saves_or_restarts(self):
        for answers in (['no'],['yes','wrong phrase']):
            with self.subTest(answers=answers):
                apply=Mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(control.set_shutdown_time(self.path,120,Mock(side_effect=answers),apply))
                self.assertEqual(self.path.read_text(),self.original)
                self.assertEqual(len(list(self.path.parent.iterdir())),1)
                apply.assert_not_called()

    def test_invalid_minutes_do_not_prompt_or_write(self):
        for value in (0,-1,float('nan'),float('inf')):
            ask=Mock()
            with self.subTest(value=value),self.assertRaises(ValueError):
                control.set_shutdown_time(self.path,value,ask)
            ask.assert_not_called()
        self.assertEqual(self.path.read_text(),self.original)

    def test_concurrent_edit_is_preserved(self):
        def ask(prompt):
            if '1 of 2' in prompt:
                return 'yes'
            self.path.write_text(self.original+'# concurrent edit\n')
            return 'SET SHUTDOWN TIMER TO 120 MINUTES'
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'changed during'):
            control.set_shutdown_time(self.path,120,ask)
        self.assertTrue(self.path.read_text().endswith('# concurrent edit\n'))

    def test_time_change_restarts_without_enabling_boot(self):
        with patch.object(control.subprocess,'run') as run,contextlib.redirect_stdout(io.StringIO()):
            control.restart_service()
        self.assertEqual(run.call_args_list,[
            unittest.mock.call(['systemctl','reset-failed','x120x_upsd.service'],check=True),
            unittest.mock.call(['systemctl','restart','x120x_upsd.service'],check=True)])

    def test_new_limit_uses_elapsed_outage_and_keeps_battery_cutoff(self):
        initial=ups.Settings(automatic_shutdown=True,ac_max_downtime=120)
        charge,shutdown=Mock(),Mock()
        controller=ups.UPSController(initial,charge,shutdown)
        sample=dict(current_voltage=4.0,current_capacity=80,charger_present=False,
                    charging_enabled=False,battery_temperature=None)
        previous=controller.tick(sample,0,100)
        restarted=ups.UPSController(ups.Settings(automatic_shutdown=True,ac_max_downtime=5),charge,shutdown,previous)
        restarted.tick(sample,600,110)
        shutdown.assert_called_once()


def screen_with_keys(keys):
    screen=Mock()
    screen.getmaxyx.return_value=(12,80)
    screen.getch.side_effect=keys
    return screen


class MenuTests(unittest.TestCase):
    def test_menu_selects_and_validates_new_time_without_applying(self):
        screen=screen_with_keys([ord('1'),ord('1'),ord('2'),ord('0'),10])
        report,_=dashboard.demo_data()
        with patch.object(dashboard,'run_settings_helper') as apply:
            self.assertEqual(dashboard.settings_menu(screen,report),120)
        apply.assert_not_called()

    def test_escape_cancels_from_menu_or_entry(self):
        for keys in ([27],[ord('1'),ord('9'),27]):
            with self.subTest(keys=keys):
                report,_=dashboard.demo_data()
                self.assertIsNone(dashboard.settings_menu(screen_with_keys(keys),report))

    def test_demo_cannot_apply(self):
        report,_=dashboard.demo_data()
        with patch.object(dashboard,'run_settings_helper') as apply:
            self.assertIsNone(dashboard.settings_menu(screen_with_keys([ord('1'),10]),report,demo=True))
        apply.assert_not_called()

    def test_small_terminal_cannot_hide_a_settings_action(self):
        report,_=dashboard.demo_data()
        screen=screen_with_keys([ord('1'),10])
        screen.getmaxyx.return_value=(6,25)
        self.assertIsNone(dashboard.settings_menu(screen,report))

    def test_unchanged_time_does_not_restart(self):
        report,_=dashboard.demo_data()
        self.assertIsNone(dashboard.settings_menu(screen_with_keys([ord('1'),ord('5'),10]),report))

    def test_invalid_time_can_be_corrected(self):
        report,_=dashboard.demo_data()
        screen=screen_with_keys([ord('1'),ord('0'),10,127,ord('3'),ord('0'),10])
        self.assertEqual(dashboard.settings_menu(screen,report),30)

    def test_permission_uses_fixed_helper_and_numeric_arguments_without_shell(self):
        self.assertEqual(dashboard.settings_command(120,is_root=False),
                         ['sudo','--',dashboard.SETTINGS_HELPER,'set-shutdown-time','--minutes','120'])
        self.assertEqual(dashboard.settings_command(30,is_root=True),
                         [dashboard.SETTINGS_HELPER,'set-shutdown-time','--minutes','30'])
        runner=Mock(return_value=Mock(returncode=2))
        with patch.object(dashboard.os,'geteuid',return_value=1000):
            self.assertEqual(dashboard.run_settings_helper(120,runner),2)
        runner.assert_called_once_with(
            ['sudo','--',dashboard.SETTINGS_HELPER,'set-shutdown-time','--minutes','120'],check=False)

    def test_closing_settings_never_starts_helper(self):
        report,_=dashboard.demo_data()
        with patch.object(dashboard,'run_settings_helper') as apply:
            self.assertIn('no change',dashboard.open_settings(screen_with_keys([27]),report))
        apply.assert_not_called()

    def test_missing_helper_is_reported_without_sudo(self):
        report,_=dashboard.demo_data()
        with patch.object(dashboard,'settings_menu',return_value=120), \
                patch.object(dashboard.Path,'is_file',return_value=False), \
                patch.object(dashboard,'run_settings_helper') as apply:
            self.assertIn('missing',dashboard.open_settings(Mock(),report))
        apply.assert_not_called()

    def test_helper_cancellation_returns_to_live_dashboard(self):
        report,_=dashboard.demo_data()
        screen=Mock()
        with patch.object(dashboard,'settings_menu',return_value=120), \
                patch.object(dashboard.Path,'is_file',return_value=True), \
                patch.object(dashboard.curses,'def_prog_mode'), \
                patch.object(dashboard.curses,'endwin'), \
                patch.object(dashboard.curses,'reset_prog_mode') as restore, \
                patch.object(dashboard,'run_settings_helper',return_value=2), \
                patch('builtins.input',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            self.assertIn('cancelled',dashboard.open_settings(screen,report).lower())
        restore.assert_called_once()
        screen.clearok.assert_called_once_with(True)


if __name__=='__main__':
    unittest.main()
