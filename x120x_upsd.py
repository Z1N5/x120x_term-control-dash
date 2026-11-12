#!/usr/bin/env python3
"""X120X monitor and explicitly opt-in UPS controller for Raspberry Pi OS Lite.

Derived from ArjenR/x120x_upsd (CC BY-SA 4.0). Modified 2026-11-12.
One supervised loop owns all hardware and decisions. The dashboard only reads JSON.
"""
import argparse
import configparser
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

DEFAULT_STATUS = '/run/x120x-ups/status.json'


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=10).stdout


def notify(message):
    # Optional for foreground use; installed with python3-systemd on the Pi.
    try:
        from systemd.daemon import notify as sd_notify
    except ImportError:
        return
    sd_notify(message)


@dataclass
class Settings:
    max_voltage: float = 0
    min_voltage: float = 3.5
    max_charge_capacity: float = 80
    min_charge_capacity: float = 20
    ac_max_downtime: float = 5
    warmup_time: float = 0
    no_power_at_start: str = 'standard'
    disable_self_protect: bool = False
    # No hardware changes or shutdown commands without these explicit opt-ins.
    control_charging: bool = False
    automatic_shutdown: bool = False
    # Legacy field accepted, but there is no added grace period after a trigger.
    shutdown_grace_seconds: float = 0
    poll_seconds: float = 2
    json_report_file: str = DEFAULT_STATUS
    json_report_period: float = 2
    battery_report_schedule: str = '0 * * * *'
    temperature_sensor_type: str = ''
    telegram_enabled: bool = False
    telegram_config_file: str = '/usr/local/etc/x120x-telegram.json'

    @classmethod
    def load(cls, path):
        parser = configparser.ConfigParser()
        if not parser.read(path):
            raise ValueError(f'Configuration not found: {path}')
        if 'general' not in parser:
            raise ValueError('Missing [general] configuration section')
        section = parser['general']
        result = cls()
        for name in cls.__dataclass_fields__:
            if name not in section:
                continue
            default = getattr(result, name)
            if isinstance(default, bool):
                value = section.getboolean(name)
            elif isinstance(default, (int, float)):
                value = section.getfloat(name)
            else:
                value = section[name].strip().strip('"')
            setattr(result, name, value)
        result.validate()
        return result

    def validate(self):
        for key in ('max_voltage', 'min_voltage', 'max_charge_capacity',
                    'min_charge_capacity', 'ac_max_downtime', 'warmup_time',
                    'shutdown_grace_seconds', 'poll_seconds', 'json_report_period'):
            if not math.isfinite(getattr(self, key)):
                raise ValueError(f'{key} must be finite')
        if not (20 <= self.max_charge_capacity <= 100):
            raise ValueError('max_charge_capacity must be between 20 and 100')
        if not (10 <= self.min_charge_capacity < self.max_charge_capacity):
            raise ValueError('min_charge_capacity must be >=10 and below maximum')
        if self.max_voltage != 0 and not (3.5 <= self.max_voltage <= 4.2):
            raise ValueError('max_voltage must be 0 or between 3.5 and 4.2')
        if self.min_voltage != 0 and not (3.0 <= self.min_voltage <= 4.0):
            raise ValueError('min_voltage must be 0 or between 3.0 and 4.0')
        if self.max_voltage and self.min_voltage >= self.max_voltage:
            raise ValueError('min_voltage must be below max_voltage')
        if self.no_power_at_start not in ('standard', 'run_till_minimums', 'run_till_protect'):
            raise ValueError('Invalid no_power_at_start policy')
        if min(self.ac_max_downtime, self.warmup_time, self.shutdown_grace_seconds) < 0:
            raise ValueError('Times must not be negative')
        if not (1 <= self.poll_seconds <= 30) or not (1 <= self.json_report_period <= 30):
            raise ValueError('Poll/report periods must be between 1 and 30 seconds')
        path = Path(self.json_report_file)
        if not path.is_absolute() or path.is_dir():
            raise ValueError('json_report_file must be an absolute filename')
        if not Path(self.telegram_config_file).is_absolute():
            raise ValueError('telegram_config_file must be an absolute filename')
        if self.temperature_sensor_type:
            parts = self.temperature_sensor_type.split(',')
            if len(parts) != 3 or parts[0] not in ('DHT11', 'DHT22'):
                raise ValueError('temperature_sensor_type must be DHT11/22,GPIO,NO_PULL_UP')
            if not parts[1].isdigit() or int(parts[1]) in (2, 3, 6, 16):
                raise ValueError('Temperature sensor cannot use an I2C or UPS GPIO')
            if parts[2] != 'NO_PULL_UP':
                raise ValueError('DHT sensors require an external resistor; use NO_PULL_UP')


def atomic_json(path, report):
    """Never expose half a JSON document, and leave readable permissions."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(report, stream, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_gpio(output):
    states = {}
    for line in output.splitlines():
        match = re.match(r'\s*(\d+):\s+(\S+).*\|\s*(hi|lo|--)(?=\s|$)', line)
        if match:
            level = None if match[3] == '--' else match[3] == 'hi'
            states[int(match[1])] = (match[2], level)
    if 6 not in states or 16 not in states:
        raise ValueError('Cannot read GPIO 6 and 16 from pinctrl output')
    if states[6][0] != 'ip':
        raise ValueError('GPIO 6 is not an input; refusing an unreliable power reading')
    if states[6][1] is None:
        raise ValueError('GPIO 6 has no readable level; external power state is unknown')
    # Pi 5 may leave GPIO 16 in "no" (none) mode before explicit control.
    # This does not invalidate GPIO 6 or grant permission to configure GPIO 16.
    mode, level = states[16]
    enabled = None if mode not in ('ip', 'op') or level is None else not level
    return states[6][1], enabled


class Hardware:
    def __init__(self, settings):
        import smbus2
        self.bus = smbus2.SMBus(1)
        self.sensor = None
        try:
            if settings.temperature_sensor_type:
                import board
                import adafruit_dht
                kind, pin, _ = settings.temperature_sensor_type.split(',')
                self.sensor = getattr(adafruit_dht, kind)(getattr(board, 'D' + pin))
        except Exception:
            self.bus.close()
            raise

    def read(self):
        def word(register):
            raw = self.bus.read_word_data(0x36, register)
            return ((raw & 255) << 8) | (raw >> 8)
        voltage = word(2) * 1.25 / 1000 / 16
        capacity = word(4) / 256
        if not (0 < voltage <= 5 and 0 <= capacity <= 110):
            raise ValueError(f'Invalid fuel-gauge readings: {voltage}V, {capacity}%')
        present, enabled = parse_gpio(command(['pinctrl', 'get', '6,16']))
        temperature = None
        if self.sensor is not None:
            # Missing configured temperature never grants permission to charge.
            try:
                temperature = self.sensor.temperature
            except RuntimeError:
                pass
            if temperature is not None and not math.isfinite(temperature):
                temperature = None
        return dict(current_voltage=voltage, current_capacity=capacity,
                    charger_present=present, charging_enabled=enabled,
                    battery_temperature=temperature)

    def set_charging(self, enabled):
        # BCM GPIO 16: high disables, low enables (Suptronics documentation).
        command(['pinctrl', 'set', '16', 'op', 'dl' if enabled else 'dh'])

    def close(self):
        try:
            if self.sensor is not None:
                self.sensor.exit()
        finally:
            self.bus.close()


class UPSController:
    """Deterministic policy; actions are separately gated and injectable in tests."""
    def __init__(self, settings, set_charging, shutdown, previous=None):
        self.settings = settings
        self.set_charging = set_charging
        self.shutdown = shutdown
        self.outage_started = None
        self.shutdown_deadline = None
        self.shutdown_reason = ''
        self.shutdown_executed = False
        self.startup_policy = None
        self.last_charge_request = None
        if previous:
            self.outage_started = previous.get('outage_started_monotonic')
            stored_policy = previous.get('startup_policy')
            if stored_policy in ('standard', settings.no_power_at_start):
                self.startup_policy = stored_policy
            # Re-evaluate thresholds from fresh readings. Do not inherit an old
            # version's extra shutdown grace period.

    def tick(self, sample, now, uptime_minutes):
        cfg = self.settings
        present = sample['charger_present']
        capacity, voltage = sample['current_capacity'], sample['current_voltage']
        temp = sample.get('battery_temperature')
        if self.startup_policy is None:
            self.startup_policy = cfg.no_power_at_start if not present else 'standard'
        if present:
            self.outage_started = None
            self.shutdown_deadline = None
            self.shutdown_reason = ''
            self.shutdown_executed = False
            # Special startup policy expires when mains has been seen.
            self.startup_policy = 'standard'
        elif self.outage_started is None:
            self.outage_started = now
        outage = 0 if self.outage_started is None else max(0, now - self.outage_started)

        warm = uptime_minutes >= cfg.warmup_time
        temperature_ok = not cfg.temperature_sensor_type or (temp is not None and 15 <= temp <= 50)
        enabled = sample['charging_enabled']
        # Observe hardware on every iteration, rather than trusting an old command.
        # Once explicitly authorised, initialise an unknown charging switch
        # conservatively to disabled in the hysteresis band. Below the resume
        # threshold normal policy permits charging. Observation never writes it.
        desired = enabled if enabled is not None else False
        if not warm or not temperature_ok:
            desired = False
        elif capacity >= cfg.max_charge_capacity or (cfg.max_voltage and voltage >= cfg.max_voltage):
            desired = False
        elif capacity <= cfg.max_charge_capacity - 3 and (not cfg.max_voltage or voltage <= cfg.max_voltage * .97):
            desired = True
        if cfg.control_charging and desired != enabled:
            self.set_charging(desired)
            self.last_charge_request = desired
        # A requested state is not presented as a measured state.
        charge_reason = ('Waiting for warm-up' if not warm else
                         'Battery temperature unavailable/out of range' if not temperature_ok else
                         'Charge limit / hysteresis')

        reason = ''
        if not present:
            if not cfg.disable_self_protect and voltage < 3.0:
                reason = 'Battery below 3.0V protection threshold'
            elif not cfg.disable_self_protect and temp is not None and temp > 55:
                reason = 'Battery above 55C protection threshold'
            elif self.startup_policy != 'run_till_protect':
                if capacity <= cfg.min_charge_capacity:
                    reason = 'Battery capacity below configured minimum'
                elif cfg.min_voltage and voltage <= cfg.min_voltage:
                    reason = 'Battery voltage below configured minimum'
                elif self.startup_policy == 'standard' and cfg.ac_max_downtime and outage >= cfg.ac_max_downtime * 60:
                    reason = 'External power outage exceeded configured duration'
        if cfg.automatic_shutdown and reason:
            self.shutdown_deadline = now
            self.shutdown_reason = reason
        if (cfg.automatic_shutdown and not present and self.shutdown_deadline is not None
                and now >= self.shutdown_deadline and not self.shutdown_executed):
            self.shutdown(self.shutdown_reason)
            self.shutdown_executed = True

        return dict(sample, control_charging=cfg.control_charging,
                    charger_charging=(None if enabled is None and present else bool(enabled and present)),
                    automatic_shutdown=cfg.automatic_shutdown,
                    fan_control='Raspberry Pi OS', charging_requested=desired,
                    charging_recommendation=charge_reason, warmup_complete=warm,
                    shutdown_recommended=reason, shutdown_reason=self.shutdown_reason,
                    shutdown_initiated=self.shutdown_deadline is not None,
                    shutdown_executed=self.shutdown_executed,
                    shutdown_deadline_monotonic=self.shutdown_deadline,
                    shutdown_seconds_remaining=(max(0, self.shutdown_deadline - now)
                        if self.shutdown_deadline is not None else None),
                    outage_started_monotonic=self.outage_started,
                    timer_no_power=round(outage, 1), startup_policy=self.startup_policy,
                    battery_runtime_remaining_seconds=(max(0, cfg.ac_max_downtime * 60 - outage)
                        if not present and self.startup_policy == 'standard' and cfg.ac_max_downtime else None),
                    outage_limit_seconds=(cfg.ac_max_downtime * 60
                        if self.startup_policy == 'standard' and cfg.ac_max_downtime else None),
                    min_capacity=cfg.min_charge_capacity, min_voltage=cfg.min_voltage,
                    max_capacity=cfg.max_charge_capacity, max_voltage=cfg.max_voltage)


def restore_state(path, boot_id):
    try:
        data = json.loads(Path(path).read_text())
        if data.get('boot_id') != boot_id:
            return None
        for name in ('outage_started_monotonic', 'shutdown_deadline_monotonic'):
            value = data.get(name)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def request_shutdown(reason):
    shutdown = shutil.which('shutdown') or '/sbin/shutdown'
    print(f'Shutting down: {reason}', flush=True)
    command([shutdown, '-h', 'now', reason])


def boot_identity():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def minutes_since_boot():
    return float(Path('/proc/uptime').read_text().split()[0]) / 60


def run_service(settings, hardware_factory=Hardware, runtime_dir='/run/x120x-ups', stop_event=None):
    settings.validate()
    stop = stop_event if stop_event is not None else threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    status_path = Path(settings.json_report_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    # Lock independent of the configured report path. Dashboards never acquire it.
    lock_path = Path(runtime_dir) / 'daemon.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another x120x_upsd instance is already running')
    hardware = None
    report = {}
    boot_id = boot_identity()
    scheduler = None
    telegram = None
    telegram_state = 'disabled'
    try:
        previous = restore_state(status_path, boot_id)
        hardware = hardware_factory(settings)
        if settings.telegram_enabled:
            try:
                from x120x_telegram import TelegramCredentials, TelegramNotifier
                telegram = TelegramNotifier(TelegramCredentials.load(settings.telegram_config_file))
                telegram.start()
            except Exception:
                telegram_state = 'configuration_error'
                print('Telegram setup failed; UPS monitoring continues. Check private credentials/state permissions.', flush=True)
        controller = UPSController(settings, hardware.set_charging, request_shutdown, previous)
        # Cron reporting stays on the monitored loop, not a second hardware worker.
        if settings.battery_report_schedule:
            from apscheduler.triggers.cron import CronTrigger
            from datetime import datetime
            trigger = CronTrigger.from_crontab(settings.battery_report_schedule)
            scheduler = trigger.get_next_fire_time(None, datetime.now().astimezone())
        ready = False
        last_events = {}
        while not stop.is_set():
            now = time.monotonic()
            uptime_minutes = minutes_since_boot()
            report = controller.tick(hardware.read(), now, uptime_minutes)
            report.update(timestamp=time.time(), boot_id=boot_id, health='ok', error=None,
                          report_interval=settings.poll_seconds, schema_version=2)
            if telegram is not None:
                try:
                    telegram.observe(report)
                    report.update(telegram.status())
                except Exception:
                    telegram.close()
                    telegram = None
                    telegram_state = 'outbox_error'
                    print('Telegram outbox failed; UPS monitoring continues.', flush=True)
            if telegram is None:
                report['telegram_state'] = telegram_state
            atomic_json(status_path, report)
            for key in ('charger_present', 'charging_enabled', 'shutdown_recommended',
                        'shutdown_initiated'):
                if key not in last_events or report[key] != last_events[key]:
                    print(f'{key}: {report[key]}', flush=True)
                    last_events[key] = report[key]
            if scheduler is not None:
                current = datetime.now().astimezone()
                if current >= scheduler:
                    print(f"Battery {report['current_capacity']:.1f}% {report['current_voltage']:.3f}V; "
                          f"external power {'OK' if report['charger_present'] else 'LOST'}", flush=True)
                    scheduler = trigger.get_next_fire_time(scheduler, current)
            if not ready:
                print(f'Monitoring started. Charging control={settings.control_charging}; '
                      f'automatic shutdown={settings.automatic_shutdown}', flush=True)
                notify('READY=1')
                ready = True
            # A heartbeat follows a COMPLETE successful monitoring and report cycle.
            notify('WATCHDOG=1')
            stop.wait(settings.poll_seconds)
    except Exception as error:
        report.update(health='error', error=str(error), error_timestamp=time.time(), boot_id=boot_id)
        try:
            atomic_json(status_path, report)
        except OSError:
            pass
        raise
    finally:
        notify('STOPPING=1')
        try:
            if report.get('health') == 'ok':
                atomic_json(status_path, dict(report, health='stopped'))
        finally:
            try:
                if telegram is not None:
                    telegram.close()
                if hardware is not None:
                    hardware.close()
            finally:
                lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/usr/local/etc/x120x_upsd.ini')
    parser.add_argument('--check-config', action='store_true', help='Validate without opening hardware')
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
        if args.check_config:
            if settings.battery_report_schedule:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(settings.battery_report_schedule)
            print(f'Configuration valid. Charging control: {settings.control_charging}; '
                  f'automatic shutdown: {settings.automatic_shutdown}; fan: OS managed.')
        else:
            run_service(settings)
        return 0
    except Exception as error:
        print(f'x120x_upsd failed: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
