import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

import x120x_control as control
import x120x_telegram as telegram
import x120x_upsd as ups


def credentials(chat='123'):
    # Synthetic assembled test value, never a live credential.
    return telegram.TelegramCredentials(str(123456) + ':' + 'x' * 35, chat)


def report(present, timestamp=1000):
    return dict(charger_present=present, timestamp=timestamp, current_capacity=80,
                current_voltage=4.0, min_capacity=20, min_voltage=3.5,
                automatic_shutdown=True, outage_limit_seconds=300)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outbox = Path(self.tmp.name) / 'outbox.json'

    def notifier(self, **kwargs):
        return telegram.TelegramNotifier(credentials(), self.outbox, **kwargs)

    def test_baseline_no_false_alert_and_one_per_transition(self):
        notifier = self.notifier()
        notifier.observe(report(True))
        self.assertEqual(notifier.status()['telegram_pending'], 0)
        for _ in range(5):
            notifier.observe(report(False, 1010))
        self.assertEqual(notifier.status()['telegram_pending'], 1)
        notifier.observe(report(True, 1070))
        self.assertEqual(notifier.status()['telegram_pending'], 2)
        self.assertIn('POWER LOST', notifier.queue[0]['text'])
        self.assertIn('POWER RESTORED', notifier.queue[1]['text'])
        self.assertIn('60 seconds', notifier.queue[1]['text'])
        self.assertNotIn(credentials().bot_token, self.outbox.read_text())
        self.assertEqual(self.outbox.stat().st_mode & 0o777, 0o600)

    def test_restart_recovers_queue_and_transition_state(self):
        first = self.notifier()
        first.observe(report(True))
        first.observe(report(False, 1010))
        second = self.notifier()
        second.observe(report(False, 1020))
        self.assertEqual(len(second.queue), 1)
        second.observe(report(True, 1070))
        self.assertEqual(len(second.queue), 2)
        self.assertIn('60 seconds', second.queue[1]['text'])

    def test_changed_destination_does_not_receive_old_messages(self):
        first = self.notifier()
        first.observe(report(True))
        first.observe(report(False))
        second = telegram.TelegramNotifier(credentials('456'), self.outbox)
        self.assertEqual(second.queue, [])
        self.assertIsNone(second.previous_present)

    def test_network_is_never_called_by_observe(self):
        sender = Mock()
        notifier = self.notifier(sender=sender)
        notifier.observe(report(True))
        notifier.observe(report(False))
        sender.assert_not_called()

    def test_blocked_network_does_not_block_power_observation_or_close(self):
        started, release = threading.Event(), threading.Event()
        def sender(*_):
            started.set()
            release.wait(5)
        notifier = self.notifier(sender=sender)
        notifier.observe(report(True))
        notifier.observe(report(False))
        notifier.start()
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            notifier.observe(report(True, 1010))
            self.assertLess(time.monotonic()-before, .5)
            before = time.monotonic()
            notifier.close()
            self.assertLess(time.monotonic()-before, .5)
        finally:
            release.set()
            notifier.thread.join(2)

    def test_successful_delivery_preserves_order_and_empties_outbox(self):
        delivered = []
        done = threading.Event()
        def sender(_, text):
            delivered.append(text)
            if len(delivered) == 2:
                done.set()
        notifier = self.notifier(sender=sender)
        notifier.observe(report(True))
        notifier.observe(report(False))
        notifier.observe(report(True, 1060))
        notifier.start()
        try:
            self.assertTrue(done.wait(2))
            deadline = time.monotonic()+1
            while notifier.status()['telegram_pending'] and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(notifier.status()['telegram_pending'], 0)
            self.assertIn('POWER LOST', delivered[0])
            self.assertIn('POWER RESTORED', delivered[1])
        finally:
            notifier.close()

    def test_failed_delivery_is_retained_and_safe_status_has_no_secret(self):
        attempted = threading.Event()
        def sender(*_):
            attempted.set()
            raise RuntimeError(credentials().bot_token)
        notifier = self.notifier(sender=sender)
        notifier.observe(report(True))
        notifier.observe(report(False))
        notifier.start()
        try:
            self.assertTrue(attempted.wait(1))
            deadline = time.monotonic()+1
            while notifier.status()['telegram_state'] != 'delivery_failed' and time.monotonic()<deadline:
                time.sleep(.01)
            status = notifier.status()
            self.assertEqual(status['telegram_pending'], 1)
            self.assertEqual(status['telegram_state'], 'delivery_failed')
            self.assertNotIn(credentials().bot_token, repr(status))
            self.assertEqual(len(json.loads(self.outbox.read_text())['queue']), 1)
        finally:
            notifier.close()

    def test_bounded_queue_reports_overflow(self):
        notifier = self.notifier()
        notifier.observe(report(True))
        for number in range(70):
            notifier.observe(report(number % 2 == 1, 1000+number))
        self.assertEqual(len(notifier.queue), 64)
        self.assertEqual(notifier.status()['telegram_dropped'], 6)

    def test_failed_message_is_retried_and_delivered(self):
        done = threading.Event()
        calls = []
        def sender(*_):
            calls.append(1)
            if len(calls) == 1:
                raise telegram.DeliveryError('Offline')
            done.set()
        notifier = self.notifier(sender=sender)
        notifier.observe(report(True))
        notifier.observe(report(False))
        notifier.start()
        try:
            self.assertTrue(done.wait(3))
            self.assertEqual(len(calls), 2)
        finally:
            notifier.close()

    def test_world_readable_state_directory_rejected(self):
        self.outbox.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            self.notifier()
        self.outbox.parent.chmod(0o700)


class TransportTests(unittest.TestCase):
    def test_api_uses_post_and_no_message_format_interpretation(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.return_value = response
        telegram.send_message(credentials(), 'test <literal>', opener)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.method, 'POST')
        data = parse_qs(request.data.decode())
        self.assertEqual(data['chat_id'], ['123'])
        self.assertEqual(data['text'], ['test <literal>'])
        self.assertNotIn('parse_mode', data)

    def test_http_errors_never_disclose_token_url(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('https://example.invalid/'+credentials().bot_token,
                                            401, 'bad token', {}, None)
        with self.assertRaises(telegram.DeliveryError) as caught:
            telegram.send_message(credentials(), 'hello', opener)
        self.assertEqual(str(caught.exception), 'Telegram HTTP 401')
        self.assertNotIn(credentials().bot_token, str(caught.exception))

    def test_rate_limit_retry_after_is_respected(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('https://example.invalid/', 429, 'slow', {},
                                            io.BytesIO(b'{"parameters":{"retry_after":120}}'))
        with self.assertRaises(telegram.DeliveryError) as caught:
            telegram.send_message(credentials(), 'hello', opener)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_arbitrary_network_errors_are_redacted(self):
        opener = Mock()
        opener.open.side_effect = OSError(credentials().bot_token)
        with self.assertRaisesRegex(telegram.DeliveryError, '^Telegram network or response error$'):
            telegram.send_message(credentials(), 'hello', opener)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(telegram.NoRedirect().redirect_request(None,None,302,'',{},'https://example.invalid/'))

    def test_secret_values_are_not_in_credential_repr(self):
        self.assertNotIn(credentials().bot_token, repr(credentials()))
        self.assertNotIn('123', repr(credentials()))

    def test_chat_discovery_only_returns_ids(self):
        with patch.object(telegram, 'api_request', return_value=dict(ok=True, result=[
                {'message': {'chat': {'id': 123, 'first_name': 'private name'}}},
                {'my_chat_member': {'chat': {'id': -456}}}])):
            self.assertEqual(telegram.recent_chat_ids(credentials().bot_token), ['-456','123'])


class PrivateSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'ups.ini'
        self.secrets = self.root / 'telegram.json'
        self.original = '[general]\ncontrol_charging=true\nautomatic_shutdown=true\n'
        self.config.write_text(self.original)

    def test_credentials_require_private_permissions_and_no_symlinks(self):
        data = dict(bot_token=credentials().bot_token, chat_id='123')
        telegram.private_json(self.secrets, data)
        self.assertEqual(telegram.TelegramCredentials.load(self.secrets).chat_id, '123')
        self.secrets.chmod(0o644)
        with self.assertRaises(ValueError):
            telegram.TelegramCredentials.load(self.secrets)
        self.secrets.chmod(0o600)
        link = self.root / 'link.json'
        link.symlink_to(self.secrets)
        with self.assertRaises(ValueError):
            telegram.TelegramCredentials.load(link)

    def test_cancel_either_confirmation_does_not_even_ask_for_secrets(self):
        for answers in (['no'], ['yes','no']):
            ask_secret, service = Mock(), Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(control.enable_telegram(self.config, Mock(side_effect=answers),
                    ask_secret, service, self.secrets))
            ask_secret.assert_not_called()
            service.assert_not_called()
            self.assertFalse(self.secrets.exists())
            self.assertEqual(self.config.read_text(), self.original)

    def test_confirmed_setup_keeps_secrets_outside_configuration_and_preserves_controls(self):
        service = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(control.enable_telegram(self.config,
                Mock(side_effect=['yes','ENABLE TELEGRAM ALERTS']),
                Mock(side_effect=[credentials().bot_token, '123']), service, self.secrets))
        cfg = ups.Settings.load(self.config)
        self.assertTrue(cfg.telegram_enabled)
        self.assertTrue(cfg.control_charging)
        self.assertTrue(cfg.automatic_shutdown)
        self.assertEqual(cfg.telegram_config_file, str(self.secrets))
        self.assertNotIn(credentials().bot_token, self.config.read_text()+output.getvalue())
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)
        service.assert_called_once()

    def test_test_message_is_only_sent_after_confirmation(self):
        telegram.private_json(self.secrets, dict(bot_token=credentials().bot_token, chat_id='123'))
        self.config.write_text(self.original + 'telegram_config_file='+str(self.secrets)+'\n')
        sender = Mock()
        self.assertFalse(control.test_telegram(self.config, Mock(return_value='no'), sender))
        sender.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.test_telegram(self.config, Mock(return_value='yes'), sender))
        sender.assert_called_once()


class ServiceIsolationTests(unittest.TestCase):
    def test_bad_telegram_credentials_do_not_stop_monitoring_or_leak_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / 'status.json'
            settings = ups.Settings(json_report_file=str(status), battery_report_schedule='',
                                    telegram_enabled=True)
            hardware = Mock()
            hardware.read.return_value = dict(current_voltage=4.0, current_capacity=80,
                 charger_present=True, charging_enabled=False, battery_temperature=None)
            stop = threading.Event()
            def stop_after_cycle(_):
                stop.set()
            output = io.StringIO()
            with patch.object(ups, 'boot_identity', return_value='boot'), \
                 patch.object(ups, 'minutes_since_boot', return_value=100), \
                 patch.object(ups.signal, 'signal'), patch.object(ups, 'notify') as notify, \
                 patch.object(stop, 'wait', side_effect=stop_after_cycle), \
                 patch.object(telegram.TelegramCredentials, 'load', side_effect=ValueError(credentials().bot_token)), \
                 contextlib.redirect_stdout(output):
                ups.run_service(settings, lambda _: hardware, directory, stop)
            data = json.loads(status.read_text())
            self.assertEqual(data['telegram_state'], 'configuration_error')
            self.assertEqual(data['health'], 'stopped')
            self.assertNotIn(credentials().bot_token, output.getvalue()+status.read_text())
            self.assertIn(unittest.mock.call('WATCHDOG=1'), notify.call_args_list)
            hardware.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
