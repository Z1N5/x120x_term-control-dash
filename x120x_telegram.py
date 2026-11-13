"""Optional, outbound-only Telegram alerts. Never controls the UPS.

Credentials and the retry outbox belong outside the source checkout. Only safe
delivery-state summaries are exposed in the public-readable UPS status report.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, time as clock_time
import json
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

DEFAULT_CREDENTIALS = '/usr/local/etc/x120x-telegram.json'
DEFAULT_OUTBOX = '/var/lib/x120x-ups/telegram-outbox.json'


def heartbeat_time(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,2}:\d{2}',value.strip()):
        raise ValueError('First heartbeat time must be HH:MM in 24-hour local time')
    try:
        return datetime.strptime(value.strip(),'%H:%M').strftime('%H:%M')
    except ValueError:
        raise ValueError('First heartbeat time must be HH:MM in 24-hour local time') from None


def validate_heartbeat_hours(hours):
    if (not isinstance(hours,(int,float)) or isinstance(hours,bool) or
            not math.isfinite(hours) or not 1<=hours<=24 or int(hours)!=hours):
        raise ValueError('Heartbeat interval must be a whole number from 1 to 24 hours')
    return int(hours)


def local_timezone():
    try:
        path=Path('/etc/localtime')
        with path.open('rb') as stream:
            return ZoneInfo.from_file(stream,key=str(path.resolve()))
    except (OSError,ValueError):
        return datetime.now().astimezone().tzinfo


class HeartbeatSchedule:
    def __init__(self,hours,start,zone=None,previous=None):
        self.hours=validate_heartbeat_hours(hours)
        self.start=heartbeat_time(start)
        self.zone=local_timezone() if zone is None else zone
        self.signature=f'{self.hours}|{self.start}|{self.zone}'
        self.next_at=None
        if previous and previous.get('signature')==self.signature:
            value=previous.get('next_at')
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0:
                self.next_at=value

    def first_after(self,now,strict=False):
        local=datetime.fromtimestamp(now,self.zone)
        hour,minute=map(int,self.start.split(':'))
        def candidate(day):
            # Round-trip through UTC: a skipped DST clock time moves forward
            # through the gap. A repeated time uses its first occurrence only.
            instant=datetime.combine(day,clock_time(hour,minute),self.zone)
            return instant.astimezone(timezone.utc).timestamp()
        result=candidate(local.date())
        if result<now or (strict and result==now):
            result=candidate(local.date()+timedelta(days=1))
        return result

    def due(self,now):
        if self.next_at is None:
            self.next_at=self.first_after(now)
        if now<self.next_at:
            return False
        if self.hours==24:
            # Daily reports keep the requested local clock time across DST.
            self.next_at=self.first_after(now,strict=True)
        else:
            period=self.hours*3600
            self.next_at+= (math.floor((now-self.next_at)/period)+1)*period
        return True

    def state(self):
        return dict(signature=self.signature,next_at=self.next_at)


def heartbeat_message(credentials,report,zone):
    when=datetime.fromtimestamp(report['timestamp'],zone).isoformat(timespec='seconds')
    power='External power OK' if report['charger_present'] else 'ON BATTERY'
    return (f'{credentials.label}\nHEARTBEAT - UPS monitoring running\nTime: {when}\n{power}\n'
            f"Battery: {report['current_capacity']:.1f}% / {report['current_voltage']:.3f}V\n"
            f"Charging control: {'ON' if report.get('control_charging') else 'OFF'}\n"
            f"Automatic shutdown: {'ON' if report.get('automatic_shutdown') else 'OFF'}")


@dataclass
class TelegramCredentials:
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)
    label: str = 'Raspberry Pi UPS'

    def validate(self):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', self.bot_token):
            raise ValueError('Invalid Telegram bot token format (value withheld)')
        if not re.fullmatch(r'-?[0-9]+', self.chat_id):
            raise ValueError('Chat ID must be a numeric Telegram chat ID (value withheld)')
        if not self.label.strip() or len(self.label) > 80 or any(not c.isprintable() for c in self.label):
            raise ValueError('Device label must be 1 to 80 printable characters')

    @classmethod
    def load(cls, path):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                    raise ValueError('Credentials must be owned by the service user and accessible only to that user (0600)')
                data = json.load(stream)
            result = cls(str(data['bot_token']), str(data['chat_id']), data.get('label', 'Raspberry Pi UPS'))
            result.validate()
            return result
        except (OSError, ValueError, TypeError, KeyError):
            # Never propagate JSON fragments, private path contents or token URLs.
            raise ValueError('Cannot load Telegram credentials; check the private file, owner, permissions and format') from None


def private_json(path, data):
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.write('\n')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DeliveryError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api_request(token, method, parameters, opener=None):
    """Fixed HTTPS endpoint; no token in arguments, logs, exception text or repr."""
    if method not in ('sendMessage', 'getUpdates'):
        raise ValueError('Unsupported Telegram operation')
    request = Request('https://api.telegram.org/bot' + token + '/' + method,
                      data=urlencode(parameters).encode(),
                      headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    opener = build_opener(NoRedirect()) if opener is None else opener
    try:
        with opener.open(request, timeout=5) as response:
            result = json.loads(response.read(65536))
        if result.get('ok') is not True:
            raise DeliveryError('Telegram rejected the message')
        return result
    except HTTPError as error:
        delay = 0
        if error.code == 429:
            try:
                delay = max(1, min(3600, int(json.loads(error.read(65536))['parameters']['retry_after'])))
            except (ValueError, TypeError, KeyError, OSError):
                delay = 60
        raise DeliveryError(f'Telegram HTTP {error.code}', delay) from None
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError('Telegram network or response error') from None


def send_message(credentials, text, opener=None):
    api_request(credentials.bot_token, 'sendMessage',
                {'chat_id': credentials.chat_id, 'text': text}, opener)


def recent_chat_ids(token):
    credentials = TelegramCredentials(token, '0')
    credentials.validate()
    result = api_request(token, 'getUpdates', {'timeout': 0, 'limit': 100})
    chats = set()
    for update in result.get('result', []):
        for key in ('message', 'channel_post', 'my_chat_member'):
            chat = update.get(key, {}).get('chat', {})
            if isinstance(chat.get('id'), int):
                chats.add(str(chat['id']))
    return sorted(chats)


def event_message(credentials, report, present, duration=None):
    when = datetime.fromtimestamp(report['timestamp'], timezone.utc).isoformat(timespec='seconds')
    title = 'EXTERNAL POWER RESTORED' if present else 'EXTERNAL POWER LOST - running on battery'
    text = (f'{credentials.label}\n{title}\nTime: {when}\n'
            f"Battery: {report['current_capacity']:.1f}% / {report['current_voltage']:.3f}V")
    if present and duration is not None:
        text += f'\nOutage duration: {duration:.0f} seconds'
    if not present:
        limit = report.get('outage_limit_seconds')
        text += ('\nAutomatic shutdown: ' +
                 (f"at {limit/60:g} minutes or {report['min_capacity']:g}% / {report['min_voltage']:g}V"
                  if report.get('automatic_shutdown') and limit else
                  'enabled (battery/startup policy)' if report.get('automatic_shutdown') else 'OFF'))
    return text


class TelegramNotifier:
    """A bounded, persistent retry queue with no network operations on observe()."""
    def __init__(self, credentials, outbox=DEFAULT_OUTBOX, sender=send_message, heartbeat=None, zone=None):
        self.credentials = credentials
        self.recipient_digest = hashlib.sha256((credentials.bot_token + '\0' + credentials.chat_id).encode()).hexdigest()
        self.outbox = Path(outbox)
        self.sender = sender
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.queue = []
        self.previous_present = None
        self.outage_started_epoch = None
        self.state = 'idle'
        self.dropped = 0
        self.last_sent = None
        self.thread = None
        self.heartbeat=None
        self.heartbeat_saved=None
        self.last_heartbeat_sent=None
        self.outbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.outbox.parent.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError('Telegram state directory must be private to the service user (0700)')
        self._load()
        old_signature=self.heartbeat_saved.get('signature') if self.heartbeat_saved else None
        if heartbeat is not None:
            self.heartbeat=HeartbeatSchedule(heartbeat['hours'],heartbeat['start'],zone,self.heartbeat_saved)
        new_signature=self.heartbeat.signature if self.heartbeat else None
        if old_signature!=new_signature or self.heartbeat is None:
            self.queue=[item for item in self.queue if item.get('kind')!='heartbeat']
            self._save()

    def _load(self):
        try:
            fd = os.open(self.outbox, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise ValueError('Telegram outbox must be private to the service user (0600)')
            data = json.load(stream)
        if data.get('recipient_digest') != self.recipient_digest:
            # Never replay a previous destination's messages to a newly set chat.
            return
        pending = data.get('queue', [])
        if not isinstance(pending, list) or any(not isinstance(item, dict) or
                not isinstance(item.get('text'), str) or not isinstance(item.get('id'), str) for item in pending):
            raise ValueError('Invalid Telegram outbox')
        self.queue = pending[-64:]
        previous = data.get('previous_present')
        self.previous_present = previous if isinstance(previous, bool) else None
        self.outage_started_epoch = data.get('outage_started_epoch')
        if not isinstance(self.outage_started_epoch, (int, float)):
            self.outage_started_epoch = None
        self.dropped = int(data.get('dropped', 0))
        saved=data.get('heartbeat')
        self.heartbeat_saved=saved if isinstance(saved,dict) else None
        self.last_heartbeat_sent=data.get('last_heartbeat_sent')

    def _save(self):
        private_json(self.outbox, dict(queue=self.queue, previous_present=self.previous_present,
                     outage_started_epoch=self.outage_started_epoch, dropped=self.dropped,
                     recipient_digest=self.recipient_digest,
                     heartbeat=self.heartbeat.state() if self.heartbeat else None,
                     last_heartbeat_sent=self.last_heartbeat_sent))

    def _enqueue(self,text,kind='power'):
        if kind=='heartbeat':
            # Keep only the most recent scheduled health sample while offline.
            self.queue=[item for item in self.queue if item.get('kind')!='heartbeat']
        if len(self.queue)>=64:
            if kind=='heartbeat':
                self.dropped+=1
                return  # A routine heartbeat must not evict a power event.
            heartbeat=next((i for i,item in enumerate(self.queue) if item.get('kind')=='heartbeat'),None)
            self.queue.pop(heartbeat if heartbeat is not None else 0)
            self.dropped+=1
        self.queue.append(dict(id=str(time.time_ns()),text=text,kind=kind))
        self.state='pending'
        self.condition.notify_all()

    def observe(self, report):
        present = report['charger_present']
        with self.condition:
            changed=False
            if self.previous_present is None:
                # Establish the initial baseline without inventing a power event.
                self.previous_present = present
                if not present:
                    self.outage_started_epoch = report['timestamp']
                changed=True
            elif present != self.previous_present:
                duration = (max(0, report['timestamp'] - self.outage_started_epoch)
                        if present and self.outage_started_epoch is not None else None)
                if not present:
                    self.outage_started_epoch = report['timestamp']
                self._enqueue(event_message(self.credentials, report, present, duration))
                self.previous_present = present
                if present:
                    self.outage_started_epoch = None
                changed=True
            if self.heartbeat:
                previous_due=self.heartbeat.next_at
                if self.heartbeat.due(report['timestamp']):
                    self._enqueue(heartbeat_message(self.credentials,report,self.heartbeat.zone),'heartbeat')
                    changed=True
                changed=changed or previous_due!=self.heartbeat.next_at
            if changed:
                self._save()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='telegram-notifications', daemon=True)
        self.thread.start()

    def status(self):
        with self.condition:
            return dict(telegram_state=self.state, telegram_pending=len(self.queue),
                        telegram_dropped=self.dropped, telegram_last_sent=self.last_sent,
                        telegram_heartbeat_next=self.heartbeat.next_at if self.heartbeat else None,
                        telegram_heartbeat_last_sent=self.last_heartbeat_sent)

    def _run(self):
        backoff = 1
        try:
            while not self.stop_event.is_set():
                with self.condition:
                    if not self.queue:
                        self.condition.wait(timeout=1)
                        continue
                    item = dict(next((entry for entry in self.queue if entry.get('kind')!='heartbeat'),self.queue[0]))
                try:
                    # Critically, no condition lock is held during network access.
                    self.sender(self.credentials, item['text'])
                except Exception as error:
                    with self.condition:
                        self.state = 'delivery_failed'
                    delay = getattr(error, 'retry_after', 0)
                    delay = delay if isinstance(delay, (int, float)) and 0 < delay <= 3600 else 0
                    self.stop_event.wait(max(backoff, delay))
                    backoff = min(60, backoff * 2)
                    continue
                with self.condition:
                    self.queue=[entry for entry in self.queue if entry['id']!=item['id']]
                    self.last_sent = time.time()
                    if item.get('kind')=='heartbeat':
                        self.last_heartbeat_sent=self.last_sent
                    self.state = 'pending' if self.queue else 'sent'
                    self._save()
                backoff = 1
        except Exception:
            # Never print exceptions containing a token-bearing request or message.
            with self.condition:
                self.state = 'worker_failed'

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=.1)
