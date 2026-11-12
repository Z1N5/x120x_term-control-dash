#!/usr/bin/env python3
"""Read-only X120X terminal dashboard. No GPIO writes or shutdown commands."""
import argparse
import curses
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

DEFAULT_STATUS = '/run/x120x-ups/status.json'


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


def rows(report, pi, demo=False):
    """Text plus semantic colour; shared by curses and --once."""
    lines = [('X120X UPS / RASPBERRY PI 5' + (' [DEMO]' if demo else ''), 'title')]
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
    lines.append(('Battery runtime limit: ' + (fmt(limit / 60, ' minutes', 2)
                  if isinstance(limit, (int, float)) else 'Disabled / unavailable'), 'normal'))
    lines.append(('Low-battery cutoff: ' + fmt(report.get('min_capacity'), '%', 0) + ' or ' +
                  fmt(report.get('min_voltage'), ' V', 2) + ' (immediate if shutdown ON)', 'normal'))
    remaining_runtime = report.get('battery_runtime_remaining_seconds')
    if remaining_runtime is not None:
        lines.append(('Battery runtime remaining: ' + fmt(remaining_runtime, ' seconds', 0), 'warn'))
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
                  ('q: quit  r: refresh  arrows/PageUp/PageDown: scroll', 'normal')])
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
        if time.monotonic() >= refresh_at:
            report, pi = demo_data() if args.demo else (read_status(args.status), pi_metrics())
            content = rows(report, pi, args.demo)
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
            footer = f'Read-only | q quit | scroll {offset+1}/{len(content)}'
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
    parser.add_argument('--status', default=DEFAULT_STATUS)
    parser.add_argument('--demo', action='store_true', help='Use clearly labelled sample data; no hardware access')
    parser.add_argument('--once', action='store_true', help='Print one plain-text report')
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error('--interval must be at least one second')
    if args.once:
        report, pi = demo_data() if args.demo else (read_status(args.status), pi_metrics())
        print('\n'.join(line for line, _ in rows(report, pi, args.demo)))
    elif not sys.stdout.isatty() or not sys.stdin.isatty():
        parser.error('An interactive terminal is required; use --once for plain text')
    else:
        try:
            curses.wrapper(display, args)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
