"""Passive backup-performance history, not a measurement of battery state of health.

Consumes copies of normal UPS readings only. No hardware, network, configuration,
charging or shutdown operations belong in this module.
"""
import json
import math
import os
from pathlib import Path
from statistics import median
import tempfile

DEFAULT_HISTORY = '/var/lib/x120x-ups/battery-performance.json'
MIN_SECONDS = 15 * 60
MIN_DROP = 10
MAX_HISTORY = 60


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_session(item):
    return (isinstance(item, dict) and
            all(number(item.get(key)) for key in ('start', 'end', 'seconds', 'ended_at')) and
            0 <= item['end'] < item['start'] <= 100 and item['start'] - item['end'] >= MIN_DROP and
            item['seconds'] >= MIN_SECONDS and item['ended_at'] > 0)


def comparable(a, b):
    return abs(a['start'] - b['start']) <= 5 and abs(a['end'] - b['end']) <= 5


def minutes_per_point(item):
    return item['seconds'] / 60 / (item['start'] - item['end'])


class BatteryPerformance:
    def __init__(self, path=DEFAULT_HISTORY, max_gap=60):
        self.path = Path(path)
        self.max_gap = max_gap
        self.history = []
        self.baseline = []
        self.active = None
        self.last_now = None
        self.total = 0
        if self.path.exists():
            if self.path.stat().st_size > 65536:
                raise ValueError('Battery history is oversized')
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or data.get('schema') != 1:
                raise ValueError('Invalid battery history format')
            history, baseline = data.get('history'), data.get('baseline')
            if (not isinstance(history, list) or not isinstance(baseline, list) or
                    len(history) > MAX_HISTORY or len(baseline) > 3 or
                    not all(valid_session(item) for item in history + baseline)):
                raise ValueError('Invalid battery history sessions')
            total = data.get('total')
            if type(total) is not int or total < max(len(history), len(baseline)):
                raise ValueError('Invalid battery history count')
            if baseline and any(not comparable(baseline[0], item) for item in baseline):
                raise ValueError('Invalid battery baseline')
            # Only known numeric summaries may reach the public status report.
            keys = ('start', 'end', 'seconds', 'ended_at')
            self.history = [{key: item[key] for key in keys} for item in history]
            self.baseline = [{key: item[key] for key in keys} for item in baseline]
            self.total = total

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.path.parent.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError('Battery history directory must be private (0700)')
        fd, temporary = tempfile.mkstemp(prefix='.battery-performance-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(dict(schema=1, history=self.history, baseline=self.baseline,
                               total=self.total), stream, allow_nan=False)
                stream.write('\n')
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def finish(self):
        active, self.active = self.active, None
        if active is None:
            return
        session = dict(start=active['start'], end=active['end'],
                       seconds=active['last'] - active['first'], ended_at=active['ended_at'])
        if not valid_session(session):
            return
        self.total += 1
        if len(self.baseline) < 3 and (not self.baseline or comparable(self.baseline[0], session)):
            # Freeze the first three comparable observations; do not let an ageing
            # battery silently move its own reference. These are NOT factory capacity.
            self.baseline.append(session)
        self.history = (self.history + [session])[-MAX_HISTORY:]
        self._save()

    def observe(self, report, now):
        capacity, voltage = report.get('current_capacity'), report.get('current_voltage')
        valid = (report.get('health') == 'ok' and not report.get('stale', False) and
                 type(report.get('charger_present')) is bool and number(now) and
                 number(report.get('timestamp')) and report['timestamp'] > 0 and
                 number(capacity) and 0 <= capacity <= 100 and number(voltage) and voltage > 0)
        if not valid:
            self.active = None
            self.last_now = None
            return
        # Never count an unobserved period, clock reversal or gauge reset as a run.
        if self.last_now is not None and not 0 < now - self.last_now <= self.max_gap:
            self.active = None
        self.last_now = now
        if report['charger_present']:
            # Use the last BATTERY sample; the restored supply may already charge.
            self.finish()
            return
        if self.active and (capacity > self.active['end'] + 2 or self.active['end'] - capacity > 5):
            self.active = None
        if self.active is None:
            self.active = dict(start=capacity, end=capacity, first=now, last=now,
                               ended_at=report['timestamp'])
        else:
            self.active.update(end=min(self.active['end'], capacity), last=now,
                               ended_at=report['timestamp'])

    def status(self):
        recent = []
        if len(self.baseline) == 3:
            # Baseline observations are excluded even when retained in history.
            recent = [item for item in self.history if item not in self.baseline and
                      comparable(self.baseline[0], item)][-3:]
        trend = 'learning'
        if len(recent) == 3 and comparable(self.baseline[0], self.history[-1]):
            ratio = median(map(minutes_per_point, recent)) / median(map(minutes_per_point, self.baseline))
            trend = 'shorter' if ratio < .8 else 'longer' if ratio > 1.25 else 'similar'
        result = dict(state='collecting' if self.active else 'idle', trend=trend,
                      sessions=self.total, baseline_runs=len(self.baseline), comparison_runs=len(recent))
        if self.baseline and self.history and not comparable(self.baseline[0], self.history[-1]):
            result['learning_reason'] = 'different_range'
        if self.history:
            last = self.history[-1]
            result['last_run'] = dict(last)
        if self.baseline:
            result['reference_start'] = self.baseline[0]['start']
            result['reference_end'] = self.baseline[0]['end']
        if self.active:
            result['current_seconds'] = self.active['last'] - self.active['first']
            result['current_drop'] = self.active['start'] - self.active['end']
        return result
