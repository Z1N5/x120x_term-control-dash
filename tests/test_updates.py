import contextlib
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import x120x_dashboard as dashboard


class VersionTests(unittest.TestCase):
    def test_manifest_matches_running_dashboard_version(self):
        manifest = Path(__file__).resolve().parents[1] / 'VERSION'
        self.assertEqual(manifest.read_text().strip(), dashboard.DASHBOARD_VERSION)

    def test_newer_older_and_equal_versions_are_distinguished(self):
        with patch.object(dashboard, 'DASHBOARD_VERSION', '1.2.0'):
            self.assertIn('v1.10.0 available', dashboard.update_message('1.10.0')[0])
            self.assertIn('up to date', dashboard.update_message('v1.2.0')[0])
            self.assertIn('newer than GitHub', dashboard.update_message('1.1.9')[0])

    def test_malformed_version_is_not_rendered_as_remote_text(self):
        for value in ('1.0', '<script>', '1.2.3\nRUN THIS', '1.0.0; command', '-1.0.0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dashboard.update_message(value)

    def test_version_command_never_reads_hardware_or_network(self):
        out = io.StringIO()
        with patch('sys.argv', ['x120x-dashboard','--version']), \
                patch.object(dashboard, 'latest_version') as fetch, \
                patch.object(dashboard, 'pi_metrics') as metrics, \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            dashboard.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(dashboard.DASHBOARD_VERSION, out.getvalue())
        fetch.assert_not_called()
        metrics.assert_not_called()

    def test_explicit_update_check_exits_without_hardware(self):
        with patch('sys.argv', ['x120x-dashboard','--check-updates']), \
                patch.object(dashboard, 'latest_version', return_value='9.0.0'), \
                patch.object(dashboard, 'pi_metrics') as metrics, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dashboard.main(), 0)
        self.assertIn('v9.0.0 available', out.getvalue())
        metrics.assert_not_called()

    def test_failed_explicit_check_does_not_claim_up_to_date_or_print_error_details(self):
        with patch('sys.argv', ['x120x-dashboard','--check-updates']), \
                patch.object(dashboard, 'latest_version', side_effect=OSError('private-details')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dashboard.main(), 1)
        self.assertIn('unavailable', out.getvalue())
        self.assertNotIn('up to date', out.getvalue())
        self.assertNotIn('private-details', out.getvalue())

    def test_once_report_is_offline_by_default(self):
        report, metrics = dashboard.demo_data()
        with patch('sys.argv', ['x120x-dashboard','--once']), \
                patch.object(dashboard, 'latest_version') as fetch, \
                patch.object(dashboard, 'read_status', return_value=report), \
                patch.object(dashboard, 'pi_metrics', return_value=metrics), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            dashboard.main()
        fetch.assert_not_called()
        self.assertIn('Update check: off', out.getvalue())

    def test_footer_includes_project_upstreams_and_creator(self):
        report, metrics = dashboard.demo_data()
        text = '\n'.join(line for line, _ in dashboard.rows(report, metrics))
        self.assertIn('Dashboard version: v' + dashboard.DASHBOARD_VERSION, text)
        self.assertIn(dashboard.REPOSITORY_URL, text)
        self.assertIn('https://github.com/ArjenR/x120x_upsd', text)
        self.assertIn('https://github.com/suptronics/x120x', text)
        self.assertIn('Created by z1n5 | https://zing.co.uk', text)


class BackgroundCheckTests(unittest.TestCase):
    def test_disabled_checker_never_starts_network(self):
        fetch = Mock()
        checker = dashboard.UpdateChecker(enabled=False, fetch=fetch)
        self.assertFalse(checker.refresh(force=True))
        self.assertIn('off', checker.snapshot()[0])
        fetch.assert_not_called()

    def test_blocked_check_does_not_block_dashboard_and_cannot_duplicate_workers(self):
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            release.wait(3)
            return dashboard.DASHBOARD_VERSION
        checker = dashboard.UpdateChecker(fetch=fetch)
        self.assertTrue(checker.refresh())
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            self.assertIn('checking', checker.snapshot()[0])
            self.assertFalse(checker.refresh(force=True))
            self.assertLess(time.monotonic()-before, .5)
        finally:
            release.set()
            checker.thread.join(2)
        self.assertIn('up to date', checker.snapshot()[0])

    def test_background_network_failure_remains_visible_without_details(self):
        checker = dashboard.UpdateChecker(fetch=Mock(side_effect=OSError('private-details')))
        checker.refresh()
        checker.thread.join(2)
        self.assertIn('unavailable', checker.snapshot()[0])
        self.assertNotIn('private-details', checker.snapshot()[0])

    def test_rechecks_are_throttled_and_manual_refresh_has_cooldown(self):
        fetch = Mock(return_value=dashboard.DASHBOARD_VERSION)
        checker = dashboard.UpdateChecker(fetch=fetch)
        checker.refresh(now=100)
        checker.thread.join(2)
        self.assertFalse(checker.refresh(now=200))
        self.assertFalse(checker.refresh(force=True, now=105))
        self.assertTrue(checker.refresh(force=True, now=110))
        checker.thread.join(2)
        self.assertTrue(checker.refresh(now=110+6*3600))
        checker.thread.join(2)
        self.assertEqual(fetch.call_count, 3)


if __name__ == '__main__':
    unittest.main()
