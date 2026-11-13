import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo

import x120x_telegram as telegram
import x120x_control as control
import x120x_dashboard as dashboard
import x120x_upsd as ups

UTC=ZoneInfo('UTC')
LONDON=ZoneInfo('Europe/London')


def stamp(day=13,hour=8,minute=0):
    return datetime(2026,11,day,hour,minute,tzinfo=UTC).timestamp()


def sample(now,present=True):
    return dict(timestamp=now,charger_present=present,current_voltage=4.0,current_capacity=80,
                control_charging=True,automatic_shutdown=True,min_capacity=20,min_voltage=3.5,
                outage_limit_seconds=300)


def credentials():
    return telegram.TelegramCredentials(str(123456)+':'+'x'*35,'123')


class ScheduleTests(unittest.TestCase):
    def test_interval_and_clock_validation(self):
        for hours in range(1,25):
            self.assertEqual(telegram.validate_heartbeat_hours(hours),hours)
        for hours in (0,25,1.5,float('nan'),float('inf'),True,None):
            with self.subTest(hours=hours),self.assertRaises(ValueError):
                telegram.validate_heartbeat_hours(hours)
        self.assertEqual(telegram.heartbeat_time('9:05'),'09:05')
        for clock in ('24:00','12:60','abc','09:00:00',None):
            with self.subTest(clock=clock),self.assertRaises(ValueError):
                telegram.heartbeat_time(clock)

    def test_first_time_before_or_after_clock_occurrence(self):
        schedule=telegram.HeartbeatSchedule(2,'09:00',UTC)
        self.assertFalse(schedule.due(stamp(hour=8)))
        self.assertEqual(schedule.next_at,stamp(hour=9))
        self.assertTrue(schedule.due(stamp(hour=9)))
        self.assertEqual(schedule.next_at,stamp(hour=11))
        after=telegram.HeartbeatSchedule(2,'09:00',UTC)
        self.assertFalse(after.due(stamp(hour=9,minute=1)))
        self.assertEqual(after.next_at,stamp(day=14,hour=9))

    def test_hourly_does_not_duplicate_on_poll(self):
        schedule=telegram.HeartbeatSchedule(1,'09:00',UTC)
        self.assertTrue(schedule.due(stamp(hour=9)))
        self.assertFalse(schedule.due(stamp(hour=9)+2))
        self.assertTrue(schedule.due(stamp(hour=10)))

    def test_non_divisor_intervals_continue_across_midnight(self):
        schedule=telegram.HeartbeatSchedule(5,'09:00',UTC)
        schedule.due(stamp(hour=9))
        schedule.due(stamp(hour=14))
        schedule.due(stamp(hour=19))
        self.assertEqual(schedule.next_at,stamp(day=14,hour=0))

    def test_restart_resumes_due_time(self):
        schedule=telegram.HeartbeatSchedule(1,'09:00',UTC)
        schedule.due(stamp(hour=8))
        resumed=telegram.HeartbeatSchedule(1,'09:00',UTC,schedule.state())
        self.assertTrue(resumed.due(stamp(hour=9)))
        self.assertEqual(resumed.next_at,stamp(hour=10))

    def test_missed_intervals_coalesce_and_clock_rollback_does_not_repeat(self):
        schedule=telegram.HeartbeatSchedule(1,'09:00',UTC)
        schedule.due(stamp(hour=9))
        self.assertTrue(schedule.due(stamp(hour=15,minute=30)))
        self.assertEqual(schedule.next_at,stamp(hour=16))
        self.assertFalse(schedule.due(stamp(hour=10)))

    def test_daily_preserves_local_time_across_dst(self):
        for month,day,elapsed in ((3,28,23),(10,24,25)):
            schedule=telegram.HeartbeatSchedule(24,'09:00',LONDON)
            now=datetime(2026,month,day,9,tzinfo=LONDON).timestamp()
            self.assertTrue(schedule.due(now))
            self.assertEqual(schedule.next_at-now,elapsed*3600)
            self.assertEqual(datetime.fromtimestamp(schedule.next_at,LONDON).hour,9)

    def test_skipped_clock_time_moves_forward_and_repeated_time_sends_once(self):
        schedule=telegram.HeartbeatSchedule(24,'01:30',LONDON)
        start=datetime(2026,3,29,0,tzinfo=LONDON).timestamp()
        schedule.due(start)
        due=datetime.fromtimestamp(schedule.next_at,LONDON)
        self.assertEqual((due.hour,due.minute),(2,30))
        repeated=telegram.HeartbeatSchedule(24,'01:30',LONDON)
        first=datetime(2026,10,25,1,30,tzinfo=LONDON,fold=0).timestamp()
        second=datetime(2026,10,25,1,30,tzinfo=LONDON,fold=1).timestamp()
        self.assertTrue(repeated.due(first))
        self.assertFalse(repeated.due(second))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'outbox.json'

    def notifier(self,**kwargs):
        return telegram.TelegramNotifier(credentials(),self.path,heartbeat={'hours':1,'start':'09:00'},zone=UTC,**kwargs)

    def test_heartbeats_only_queue_at_due_time_and_observe_never_sends(self):
        sender=Mock()
        notifier=self.notifier(sender=sender)
        notifier.observe(sample(stamp(hour=8)))
        self.assertEqual(len(notifier.queue),0)
        notifier.observe(sample(stamp(hour=9)))
        self.assertEqual(len(notifier.queue),1)
        self.assertEqual(notifier.queue[0]['kind'],'heartbeat')
        self.assertIn('HEARTBEAT',notifier.queue[0]['text'])
        self.assertIn('External power OK',notifier.queue[0]['text'])
        self.assertNotIn(credentials().bot_token,self.path.read_text())
        sender.assert_not_called()

    def test_restart_does_not_requeue_and_offline_pulses_coalesce(self):
        notifier=self.notifier()
        notifier.observe(sample(stamp(hour=8)))
        notifier.observe(sample(stamp(hour=9)))
        resumed=self.notifier()
        resumed.observe(sample(stamp(hour=9,minute=30)))
        self.assertEqual(len(resumed.queue),1)
        old=resumed.queue[0]['id']
        resumed.observe(sample(stamp(hour=10)))
        self.assertEqual(len(resumed.queue),1)
        self.assertNotEqual(resumed.queue[0]['id'],old)

    def test_disabling_or_changing_schedule_removes_only_routine_pending_messages(self):
        notifier=self.notifier()
        notifier.observe(sample(stamp(hour=8)))
        notifier.observe(sample(stamp(hour=9)))
        notifier.observe(sample(stamp(hour=9,minute=1),False))
        disabled=telegram.TelegramNotifier(credentials(),self.path)
        self.assertEqual([item['kind'] for item in disabled.queue],['power'])
        self.assertIsNone(json.loads(self.path.read_text())['heartbeat'])

    def test_heartbeat_cannot_evict_full_queue_of_power_events(self):
        notifier=self.notifier()
        notifier.observe(sample(stamp(hour=8)))
        with notifier.condition:
            for i in range(64):
                notifier._enqueue('power '+str(i))
        notifier.observe(sample(stamp(hour=9)))
        self.assertEqual(len(notifier.queue),64)
        self.assertTrue(all(item['kind']=='power' for item in notifier.queue))
        self.assertEqual(notifier.queue[0]['text'],'power 0')

    def test_power_events_send_before_routine_heartbeat(self):
        sent=[]; done=threading.Event()
        def sender(_,text):
            sent.append(text)
            if len(sent)==2:
                done.set()
        notifier=self.notifier(sender=sender)
        notifier.observe(sample(stamp(hour=8)))
        notifier.observe(sample(stamp(hour=9)))
        notifier.observe(sample(stamp(hour=9,minute=1),False))
        notifier.start()
        try:
            self.assertTrue(done.wait(2))
        finally:
            notifier.close()
        self.assertIn('POWER LOST',sent[0])
        self.assertIn('HEARTBEAT',sent[1])
        self.assertIsNotNone(notifier.status()['telegram_heartbeat_last_sent'])


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ups.ini'
        self.original=('[general]\ntelegram_enabled=true\ncontrol_charging=true\n'
                       'automatic_shutdown=true\nac_max_downtime=110\n')
        self.path.write_text(self.original)

    def test_both_confirmation_cancellations_do_not_save_or_restart(self):
        for answers in (['no'],['yes','wrong']):
            service=Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(control.set_telegram_heartbeat(self.path,6,'09:00',ask=Mock(side_effect=answers),apply_service=service))
            self.assertEqual(self.path.read_text(),self.original)
            self.assertEqual(len(list(self.path.parent.iterdir())),1)
            service.assert_not_called()

    def test_confirmed_schedule_and_disable_preserve_ups_controls(self):
        service=Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.set_telegram_heartbeat(self.path,6,'9:00',
                ask=Mock(side_effect=['yes','ENABLE TELEGRAM HEARTBEAT EVERY 6 HOURS AT 09:00']),apply_service=service))
        cfg=ups.Settings.load(self.path)
        self.assertTrue(cfg.telegram_heartbeat_enabled)
        self.assertEqual(cfg.telegram_heartbeat_hours,6)
        self.assertEqual(cfg.telegram_heartbeat_start,'09:00')
        self.assertTrue(cfg.telegram_enabled and cfg.control_charging and cfg.automatic_shutdown)
        self.assertEqual(cfg.ac_max_downtime,110)
        service.assert_called_once()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(control.set_telegram_heartbeat(self.path,disable=True,
                ask=Mock(side_effect=['yes','DISABLE TELEGRAM HEARTBEAT'])))
        cfg=ups.Settings.load(self.path)
        self.assertFalse(cfg.telegram_heartbeat_enabled)
        self.assertTrue(cfg.telegram_enabled)
        self.assertEqual(cfg.telegram_heartbeat_hours,6)

    def test_telegram_must_be_enabled_before_heartbeat(self):
        self.path.write_text('[general]\ntelegram_enabled=false\n')
        ask=Mock()
        with self.assertRaisesRegex(ValueError,'enable Telegram'):
            control.set_telegram_heartbeat(self.path,1,'09:00',ask=ask)
        ask.assert_not_called()

    def test_invalid_interval_or_clock_cannot_be_saved(self):
        for hours,start in ((0,'09:00'),(25,'09:00'),(1.5,'09:00'),(2,'24:00')):
            ask=Mock()
            with self.subTest(hours=hours,start=start),self.assertRaises(ValueError):
                control.set_telegram_heartbeat(self.path,hours,start,ask=ask)
            ask.assert_not_called()
        self.assertEqual(self.path.read_text(),self.original)

    def test_settings_reject_non_integer_interval_and_bad_time(self):
        for fields in (dict(telegram_heartbeat_hours=1.5),dict(telegram_heartbeat_hours=25),
                       dict(telegram_heartbeat_start='24:00')):
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                ups.Settings(**fields).validate()


class MenuTests(unittest.TestCase):
    def screen(self,keys):
        screen=Mock(); screen.getmaxyx.return_value=(12,80); screen.getch.side_effect=keys
        return screen

    def test_main_menu_exposes_heartbeat_and_test(self):
        report,_=dashboard.demo_data()
        self.assertEqual(dashboard.settings_menu(self.screen([ord('3')]),report),'heartbeat')
        self.assertEqual(dashboard.settings_menu(self.screen([ord('4')]),report),'telegram-test')

    def test_schedule_entry_and_disable(self):
        screen=self.screen([ord('1'),ord('6'),10]+list(map(ord,'09:30'))+[10])
        self.assertEqual(dashboard.heartbeat_settings_menu(screen,{}),dict(hours=6,start='09:30'))
        self.assertEqual(dashboard.heartbeat_settings_menu(self.screen([ord('2')]),{}),'off')
        self.assertIsNone(dashboard.heartbeat_settings_menu(self.screen([27]),{}))

    def test_fixed_validated_helper_commands(self):
        self.assertEqual(dashboard.heartbeat_settings_command(dict(hours=6,start='9:00'),is_root=False),
            ['sudo','--',dashboard.SETTINGS_HELPER,'set-telegram-heartbeat','--hours','6','--start','09:00'])
        self.assertEqual(dashboard.heartbeat_settings_command('off',is_root=True),
            [dashboard.SETTINGS_HELPER,'set-telegram-heartbeat','--disable'])
        self.assertEqual(dashboard.telegram_test_command(is_root=True),[dashboard.SETTINGS_HELPER,'test-telegram'])

    def test_main_screen_includes_schedule_and_next_send(self):
        report,metrics=dashboard.demo_data()
        report.update(telegram_heartbeat_enabled=True,telegram_heartbeat_hours=6,
                      telegram_heartbeat_start='09:00',telegram_heartbeat_next=stamp(hour=9))
        text='\n'.join(line for line,_ in dashboard.rows(report,metrics))
        self.assertIn('every 6h, first at 09:00',text)
        self.assertIn('Next heartbeat:',text)


if __name__=='__main__':
    unittest.main()
