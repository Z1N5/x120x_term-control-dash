"""Optional, outbound-only Telegram alerts. Never controls the UPS.

Credentials and the retry outbox belong outside the source checkout. Only safe
delivery-state summaries are exposed in the public-readable UPS status report.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import hashlib
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

DEFAULT_CREDENTIALS = '/usr/local/etc/x120x-telegram.json'
DEFAULT_OUTBOX = '/var/lib/x120x-ups/telegram-outbox.json'


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
    def __init__(self, credentials, outbox=DEFAULT_OUTBOX, sender=send_message):
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
        self.outbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.outbox.parent.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError('Telegram state directory must be private to the service user (0700)')
        self._load()

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

    def _save(self):
        private_json(self.outbox, dict(queue=self.queue, previous_present=self.previous_present,
                     outage_started_epoch=self.outage_started_epoch, dropped=self.dropped,
                     recipient_digest=self.recipient_digest))

    def observe(self, report):
        present = report['charger_present']
        with self.condition:
            if self.previous_present is None:
                # Establish the initial baseline without inventing a power event.
                self.previous_present = present
                if not present:
                    self.outage_started_epoch = report['timestamp']
                self._save()
                return
            if present == self.previous_present:
                return
            duration = (max(0, report['timestamp'] - self.outage_started_epoch)
                        if present and self.outage_started_epoch is not None else None)
            if not present:
                self.outage_started_epoch = report['timestamp']
            text = event_message(self.credentials, report, present, duration)
            if len(self.queue) >= 64:
                self.queue.pop(0)
                self.dropped += 1
            self.queue.append(dict(id=str(time.time_ns()), text=text))
            self.previous_present = present
            if present:
                self.outage_started_epoch = None
            self._save()
            self.state = 'pending'
            self.condition.notify_all()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='telegram-notifications', daemon=True)
        self.thread.start()

    def status(self):
        with self.condition:
            return dict(telegram_state=self.state, telegram_pending=len(self.queue),
                        telegram_dropped=self.dropped, telegram_last_sent=self.last_sent)

    def _run(self):
        backoff = 1
        try:
            while not self.stop_event.is_set():
                with self.condition:
                    if not self.queue:
                        self.condition.wait(timeout=1)
                        continue
                    item = dict(self.queue[0])
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
                    if self.queue and self.queue[0]['id'] == item['id']:
                        self.queue.pop(0)
                    self.last_sent = time.time()
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
