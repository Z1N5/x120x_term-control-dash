#!/usr/bin/env python3
"""Read-only X120X terminal dashboard. No GPIO writes or shutdown commands."""
import argparse
import curses
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

DEFAULT_STATUS = '/run/x120x-ups/status.json'
DASHBOARD_VERSION = '1.0.0'
REPOSITORY_URL = 'https://github.com/Z1N5/x120x_term-control-dash'
VERSION_URL = 'https://raw.githubusercontent.com/Z1N5/x120x_term-control-dash/main/VERSION'


def version_tuple(value):
    match = re.fullmatch(r'v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})', value.strip())
    if not match:
        raise ValueError('Invalid published version')
    return tuple(int(part) for part in match.groups())


def latest_version():
    # Public version text only: no keys, Git credentials or device details sent.
    request = Request(VERSION_URL, headers={
        'User-Agent': 'x120x-term-control-dash/' + DASHBOARD_VERSION,
        'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=4) as response:
        value = response.read(65).decode('ascii').strip()
    version_tuple(value)
    return value.lstrip('v')


def update_message(published):
    comparison = version_tuple(published)
    installed = version_tuple(DASHBOARD_VERSION)
    if comparison > installed:
        return ('Update: v' + published.lstrip('v') + ' available on GitHub (manual install)', 'warn')
    if comparison == installed:
        return ('Update: up to date (GitHub version checked)', 'good')
    return ('Update: installed version is newer than GitHub publication', 'normal')


class UpdateChecker:
    """Background read-only checks; no Git commands, installs or local writes."""
    def __init__(self, enabled=True, fetch=latest_version):
        self.enabled = enabled
        self.fetch = fetch
        self.lock = threading.Lock()
        self.thread = None
        self.last_attempt = None
        self.message = ('Update: not checked yet', 'normal') if enabled else ('Update check: off', 'normal')

    def refresh(self, force=False, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.enabled or (self.thread is not None and self.thread.is_alive()):
                return False
            interval = 10 if force else 6 * 60 * 60
            if self.last_attempt is not None and now - self.last_attempt < interval:
                return False
            self.last_attempt = now
            self.message = ('Update: checking GitHub...', 'normal')
            self.thread = threading.Thread(target=self._check, name='dashboard-update-check', daemon=True)
            self.thread.start()
            return True

    def _check(self):
        try:
            message = update_message(self.fetch())
        except Exception:
            # Do not surface URLs, network details or response contents as errors.
            message = ('Update: check unavailable (offline or GitHub unavailable)', 'warn')
        with self.lock:
            self.message = message

    def snapshot(self):
        with self.lock:
            return self.message


def read_command(args):
    try:
        return subprocess.run(args, check=True, capture_output=True,
                              text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def parse_pmic(output):
    values = {}
    for line in output.splitlines():
        match = re.match(r'\s*([A-Z0-9_]+_[AV])\s+.*?=\s*([-+0-9.eE]+)\s*([AV])\s*$', line)
        if match:
            try:
                value = float(match[2])
            except ValueError:
                continue
            if math.isfinite(value):
                values[match[1]] = value
    pairs = [value * values[key[:-2] + '_V'] for key, value in values.items()
             if key.endswith('_A') and key[:-2] + '_V' in values]
    return dict(input_voltage=values.get('EXT5V_V'), cpu_volts=values.get('VDD_CORE_V'),
                cpu_amps=values.get('VDD_CORE_A'), pmic_watts=sum(pairs) if pairs else None)


def number_file(path, divisor=1):
    try:
        return float(Path(path).read_text().strip()) / divisor
    except (OSError, ValueError):
        return None


def pi_metrics(root=Path('/sys')):
    result = parse_pmic(read_command(['vcgencmd', 'pmic_read_adc']))
    result['cpu_temp'] = number_file(root / 'class/thermal/thermal_zone0/temp', 1000)
    result['fan_rpm'] = None
    result['fan_pwm_percent'] = None
    for hwmon in sorted((root / 'class/hwmon').glob('hwmon*')):
        try:
            name = (hwmon / 'name').read_text().strip()
        except OSError:
            continue
        if name not in ('pwmfan', 'pwm_fan'):
            continue
        result['fan_rpm'] = number_file(hwmon / 'fan1_input')
        duty = number_file(hwmon / 'pwm1')
        if duty is not None:
            result['fan_pwm_percent'] = duty / 255 * 100
        break
    for device in sorted((root / 'class/thermal').glob('cooling_device*')):
        try:
            if (device / 'type').read_text().strip() == 'pwm-fan':
                result['fan_cooling_state'] = number_file(device / 'cur_state')
                break
        except OSError:
            pass
    throttled = read_command(['vcgencmd', 'get_throttled'])
    match = re.search(r'=0x([0-9a-fA-F]+)', throttled)
    bits = int(match[1], 16) if match else None
    result['undervoltage_now'] = None if bits is None else bool(bits & 1)
    result['undervoltage_history'] = None if bits is None else bool(bits & (1 << 16))
    result['throttled_now'] = None if bits is None else bool(bits & (1 << 2))
    return result


def read_status(path, now=None):
    now = time.time() if now is None else now
    try:
        report = json.loads(Path(path).read_text())
        if not isinstance(report, dict):
            raise ValueError('Status must be an object')
        stamp = report.get('timestamp')
        if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
            if report.get('health') == 'error':
                report['stale'] = True
                return report
            raise ValueError('No valid sample timestamp')
        age = now - stamp
        report['age_seconds'] = max(0, age)
        report['stale'] = age < -5 or age > max(10, report.get('report_interval', 2) * 3)
        boot_file = Path('/proc/sys/kernel/random/boot_id')
        if boot_file.exists() and report.get('boot_id') != boot_file.read_text().strip():
            report['stale'] = True
        return report
    except (OSError, ValueError, TypeError) as error:
        return dict(health='unavailable', stale=True, error=str(error))


def fmt(value, unit='', precision=3):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return 'Unavailable'
    return f'{value:.{precision}f}{unit}'


def boolean(value, yes, no):
    return yes if value is True else no if value is False else 'Unknown'


@dataclass(frozen=True)
class RuntimeModel:
    # Defaults describe two 3000mAh, 3.7V cells, not measured pack capacity.
    battery_wh: float = 22.2
    load_watts: float = 5.0
    efficiency: float = .85

    def validate(self):
        if not all(math.isfinite(value) and value > 0 for value in
                   (self.battery_wh, self.load_watts, self.efficiency)):
            raise ValueError('Battery Wh, assumed total load and efficiency must be positive and finite')
        if self.efficiency > 1:
            raise ValueError('Efficiency must be a fraction greater than 0 and at most 1')

    def estimate_hours(self, report):
        self.validate()
        if report.get('health') != 'ok' or report.get('stale', True):
            return None
        capacity = report.get('current_capacity')
        reserve = report.get('min_capacity')
        voltage = report.get('current_voltage')
        cutoff = report.get('min_voltage')
        values = (capacity, reserve, voltage, cutoff)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(value) for value in values):
            return None
        if not (0 <= capacity <= 110 and 0 <= reserve <= 100 and voltage > 0 and cutoff >= 0):
            return None
        if cutoff and voltage <= cutoff:
            return 0.0
        usable_fraction = max(0, min(100, capacity) - reserve) / 100
        hours = self.battery_wh * usable_fraction * self.efficiency / self.load_watts
        return hours if math.isfinite(hours) else None


def runtime_text(hours):
    if hours is None:
        return 'Unavailable'
    if hours <= 0:
        return '0 min (battery threshold reached)'
    if hours * 60 < 5:
        return 'under 5 min (rough estimate)'
    minutes = round(hours * 60 / 5) * 5
    whole_hours, remainder = divmod(minutes, 60)
    duration = (f'{whole_hours}h {remainder:02d}m' if whole_hours else f'{remainder} min')
    return '~' + duration


def rows(report, pi, demo=False, runtime_model=None, update_status=None):
    """Text plus semantic colour; shared by curses and --once."""
    lines = [('X120X UPS / RASPBERRY PI 5' + (' [DEMO]' if demo else ''), 'title')]
    lines.append(('Dashboard version: v' + DASHBOARD_VERSION, 'normal'))
    lines.append(update_status if update_status is not None else ('Update: not checked', 'normal'))
    health = report.get('health', 'unknown')
    usable = health == 'ok' and not report.get('stale', True)
    lines.append((f"Service: {health.upper()} | " + ('LIVE' if usable else 'READINGS UNVERIFIED'),
                  'good' if usable else 'bad'))
    lines.append(('Controls: charging ' + boolean(report.get('control_charging'), 'ON', 'OFF') +
                  ' | shutdown ' + boolean(report.get('automatic_shutdown'), 'ON', 'OFF') +
                  ' | fan OS managed', 'warn'))
    if report.get('error'):
        lines.append(('Error: ' + str(report['error']), 'bad'))
    if report.get('age_seconds') is not None:
        lines.append((f"Last sample: {report['age_seconds']:.0f}s ago", 'normal'))
    lines.extend([('', 'normal'), ('--- X120X UPS ---', 'title'),
                  ('UPS voltage:       ' + fmt(report.get('current_voltage'), ' V'), 'normal'),
                  ('Battery:           ' + fmt(report.get('current_capacity'), '%', 1), 'normal')])
    capacity = report.get('current_capacity')
    if isinstance(capacity, (int, float)) and math.isfinite(capacity):
        filled = round(min(100, max(0, capacity)) / 100 * 26)
        lines.append(('                   [' + '#' * filled + '-' * (26-filled) + ']',
                      'good' if capacity > 25 else 'warn'))
    model = RuntimeModel() if runtime_model is None else runtime_model
    hours = model.estimate_hours(report)
    label = 'Est. battery backup: ' if report.get('charger_present') is True else 'Est. battery runtime: '
    lines.extend([
        (label + runtime_text(hours), 'normal'),
        ('Estimate basis: ' + f'{model.battery_wh:g}Wh / assumed {model.load_watts:g}W total / '
         f'{model.efficiency:.0%} efficiency', 'normal'),
        ('To ' + fmt(report.get('min_capacity'), '%', 0) +
         ' reserve; voltage cutoff may stop it sooner.', 'normal'),
        ('Estimate only; the separate shutdown timer still applies.', 'normal'),
    ])
    lines.extend([
        ('Charging switch:   ' + boolean(report.get('charging_enabled'), 'ENABLED', 'DISABLED'), 'normal'),
        ('Charge limit:      ' + fmt(report.get('max_capacity'), '%', 0) + ' (configured)', 'normal'),
        ('Battery temp:      ' + fmt(report.get('battery_temperature'), ' C', 1), 'normal'),
        ('', 'normal'), ('--- RPi5 / ACTIVE COOLER ---', 'title'),
        ('Input voltage:     ' + fmt(pi.get('input_voltage'), ' V'), 'normal'),
        ('CPU volts:         ' + fmt(pi.get('cpu_volts'), ' V'), 'normal'),
        ('CPU amps:          ' + fmt(pi.get('cpu_amps'), ' A'), 'normal'),
        ('PMIC rail estimate:' + ' ' + fmt(pi.get('pmic_watts'), ' W'), 'normal'),
        ('CPU temperature:   ' + fmt(pi.get('cpu_temp'), ' C', 1), 'normal'),
        ('Fan speed:         ' + fmt(pi.get('fan_rpm'), ' RPM', 0), 'normal'),
        ('Fan PWM duty:      ' + fmt(pi.get('fan_pwm_percent'), '%', 0), 'normal'),
        ('Fan cooling state: ' + fmt(pi.get('fan_cooling_state'), '', 0) + ' (OS controlled)', 'normal'),
        ('Undervoltage now:  ' + boolean(pi.get('undervoltage_now'), 'YES', 'No'),
         'bad' if pi.get('undervoltage_now') else 'normal'),
        ('Low voltage since boot: ' + boolean(pi.get('undervoltage_history'), 'YES', 'No'), 'normal'),
        ('Throttling now:    ' + boolean(pi.get('throttled_now'), 'YES', 'No'), 'normal'),
        ('', 'normal'), ('--- POWER STATUS / PERMISSIONS ---', 'title'),
    ])
    present = report.get('charger_present')
    if usable:
        lines.append(('External power: ' + boolean(present, 'OK', 'LOST - on battery'),
                      'good' if present else 'bad'))
    else:
        lines.append(('External power: UNKNOWN (service data unavailable/stale)', 'bad'))
    lines.extend([
        ('Charging control:  ' + boolean(report.get('control_charging'), 'ON', 'OFF - observation only'), 'warn'),
        ('Automatic shutdown:' + ' ' + boolean(report.get('automatic_shutdown'), 'ON', 'OFF'), 'warn'),
        ('Fan settings:      Raspberry Pi OS managed; dashboard is read-only', 'normal'),
        ('Telegram alerts:   ' + report.get('telegram_state', 'disabled') +
         ' | queued: ' + str(report.get('telegram_pending', 0)) +
         ' | dropped: ' + str(report.get('telegram_dropped', 0)), 'normal'),
    ])
    limit = report.get('outage_limit_seconds')
    lines.append(('Shutdown timer limit: ' + (fmt(limit / 60, ' minutes', 2)
                  if isinstance(limit, (int, float)) else 'Disabled / unavailable'), 'normal'))
    lines.append(('Low-battery cutoff: ' + fmt(report.get('min_capacity'), '%', 0) + ' or ' +
                  fmt(report.get('min_voltage'), ' V', 2) + ' (immediate if shutdown ON)', 'normal'))
    remaining_runtime = report.get('battery_runtime_remaining_seconds')
    if remaining_runtime is not None and report.get('automatic_shutdown') is True:
        if usable:
            remaining_runtime = max(0, remaining_runtime - report.get('age_seconds', 0))
        lines.append(('Shutdown timer remaining: ' + fmt(remaining_runtime, ' seconds', 0) +
                      ('' if usable else ' (UNVERIFIED)'), 'warn'))
    if report.get('shutdown_recommended'):
        lines.append(('Shutdown condition: ' + report['shutdown_recommended'], 'bad'))
        if report.get('automatic_shutdown') is False:
            lines.append(('No shutdown will be issued: automatic shutdown is OFF.', 'warn'))
    if report.get('shutdown_initiated'):
        remaining = report.get('shutdown_seconds_remaining')
        if remaining is not None and not demo:
            remaining = max(0, remaining - report.get('age_seconds', 0))
        lines.append(('Shutdown requested: ' + fmt(remaining, ' seconds', 0) +
                      (' (UNVERIFIED)' if not usable else ''), 'bad'))
    lines.extend([('', 'normal'),
                  ('Charge switch state does not measure battery charging current.', 'normal'),
                  ('PMIC estimate excludes unmeasured loads; not total PSU/SSD power.', 'normal'),
                  ('', 'normal'),
                  ('Project: ' + REPOSITORY_URL, 'normal'),
                  ('Based on ArjenR: https://github.com/ArjenR/x120x_upsd', 'normal'),
                  ('Original Suptronics: https://github.com/suptronics/x120x', 'normal'),
                  ('Created by z1n5 | https://zing.co.uk', 'title'),
                  ('q: quit  r: refresh  u: check updates  arrows/PageUp/PageDown: scroll', 'normal')])
    return lines


def demo_data():
    return (dict(health='ok', stale=False, age_seconds=0, current_voltage=4.075,
                 current_capacity=80.2, charger_present=True, charging_enabled=False,
                 max_capacity=80, control_charging=True, automatic_shutdown=False,
                 min_capacity=20, min_voltage=3.5, outage_limit_seconds=300,
                 battery_temperature=None, shutdown_recommended='', shutdown_initiated=False),
            dict(input_voltage=5.102, cpu_volts=.857, cpu_amps=1.563, pmic_watts=2.529,
                 cpu_temp=50.5, fan_rpm=2655, fan_pwm_percent=30, fan_cooling_state=1,
                 undervoltage_now=False, undervoltage_history=False, throttled_now=False))


def display(screen, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    colours = {}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        for index, (kind, colour) in enumerate((('title', curses.COLOR_MAGENTA),
                ('good', curses.COLOR_GREEN), ('warn', curses.COLOR_YELLOW),
                ('bad', curses.COLOR_RED)), 1):
            curses.init_pair(index, colour, background)
            colours[kind] = curses.color_pair(index)
    screen.timeout(100)
    offset = 0
    refresh_at = 0
    content = []
    while True:
        args.update_checker.refresh()
        if time.monotonic() >= refresh_at:
            report, pi = demo_data() if args.demo else (read_status(args.status), pi_metrics())
            content = rows(report, pi, args.demo, args.runtime_model, args.update_checker.snapshot())
            refresh_at = time.monotonic() + args.interval
        height, width = screen.getmaxyx()
        offset = min(max(0, offset), max(0, len(content) - max(1, height-1)))
        screen.erase()
        for y, (line, kind) in enumerate(content[offset:offset + max(0, height-1)]):
            if width > 1:
                safe = ''.join(char if char.isprintable() else ' ' for char in line)
                try:
                    screen.addnstr(y, 0, safe, width-1, colours.get(kind, 0))
                except curses.error:
                    pass
        if height > 1 and width > 1:
            footer = f'v{DASHBOARD_VERSION} | Read-only | q quit | u updates | scroll {offset+1}/{len(content)}'
            try:
                screen.addnstr(height-1, 0, footer, width-1, curses.A_REVERSE)
            except curses.error:
                pass
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        if key in (ord('r'), ord('R')):
            refresh_at = 0
        elif key in (ord('u'), ord('U')):
            args.update_checker.refresh(force=True)
            refresh_at = 0
        elif key in (curses.KEY_DOWN, ord('j')):
            offset += 1
        elif key in (curses.KEY_UP, ord('k')):
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += max(1, height-2)
        elif key == curses.KEY_PPAGE:
            offset -= max(1, height-2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='x120x-dashboard ' + DASHBOARD_VERSION)
    parser.add_argument('--no-update-check', action='store_true', help='Disable all dashboard GitHub checks')
    parser.add_argument('--check-updates', action='store_true', help='Check the public GitHub version and exit; no hardware access')
    parser.add_argument('--status', default=DEFAULT_STATUS)
    parser.add_argument('--demo', action='store_true', help='Use clearly labelled sample data; no hardware access')
    parser.add_argument('--once', action='store_true', help='Print one plain-text report')
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--battery-wh', type=float, default=22.2,
                        help='Nominal pack Wh for display estimate only (default: 22.2, two 3000mAh 3.7V cells)')
    parser.add_argument('--load-watts', type=float, default=5,
                        help='Assumed TOTAL Pi/accessory watts for display estimate only (default: 5)')
    parser.add_argument('--efficiency', type=float, default=.85,
                        help='Assumed conversion efficiency fraction for display estimate only (default: 0.85)')
    args = parser.parse_args()
    if args.check_updates:
        if args.no_update_check:
            parser.error('--check-updates and --no-update-check cannot be combined')
        print('Installed dashboard: v' + DASHBOARD_VERSION)
        try:
            print(update_message(latest_version())[0])
        except Exception:
            print('Update: check unavailable (offline or GitHub unavailable)')
            return 1
        return 0
    args.update_checker = UpdateChecker(enabled=not (args.no_update_check or args.demo or args.once))
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error('--interval must be at least one second')
    args.runtime_model = RuntimeModel(args.battery_wh, args.load_watts, args.efficiency)
    try:
        args.runtime_model.validate()
    except ValueError as error:
        parser.error(str(error))
    if args.once:
        report, pi = demo_data() if args.demo else (read_status(args.status), pi_metrics())
        print('\n'.join(line for line, _ in rows(report, pi, args.demo, args.runtime_model,
                                               args.update_checker.snapshot())))
    elif not sys.stdout.isatty() or not sys.stdin.isatty():
        parser.error('An interactive terminal is required; use --once for plain text')
    else:
        try:
            curses.wrapper(display, args)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
