#!/usr/bin/env python3
"""X120X terminal dashboard; settings use a separately privileged, confirmed helper."""
import argparse
import curses
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

DEFAULT_STATUS = '/run/x120x-ups/status.json'
DASHBOARD_VERSION = '1.4.0'
REPOSITORY_URL = 'https://github.com/Z1N5/x120x_term-control-dash'
VERSION_URL = 'https://raw.githubusercontent.com/Z1N5/x120x_term-control-dash/main/VERSION'
SETTINGS_HELPER = '/usr/local/bin/x120x-control'


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
    try:
        from x120x_fan import curve_status
        result['fan_curve_status']=curve_status(root=root)
    except ImportError:
        result['fan_curve_status']='Install x120x_fan.py to view curve settings'
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


def local_time_text(timestamp):
    if not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp):
        return 'Not scheduled / not sent yet'
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    except (OSError,OverflowError,ValueError):
        return 'Unavailable'


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
        ('Fan curve:         ' + pi.get('fan_curve_status','OS managed'), 'normal'),
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
        ('Fan settings:      Raspberry Pi OS managed', 'normal'),
        ('Telegram alerts:   ' + report.get('telegram_state', 'disabled') +
         ' | queued: ' + str(report.get('telegram_pending', 0)) +
         ' | dropped: ' + str(report.get('telegram_dropped', 0)), 'normal'),
    ])
    if 'telegram_heartbeat_enabled' not in report:
        lines.append(('Telegram heartbeat: Unknown (update the service for this feature)', 'normal'))
    elif report['telegram_heartbeat_enabled']:
        interval=report.get('telegram_heartbeat_hours')
        hours=f'{interval:g}' if isinstance(interval,(int,float)) else '?'
        lines.append((f'Telegram heartbeat: ON - every {hours}h, first at '+
                      report.get('telegram_heartbeat_start','?')+' (Pi local time)', 'normal'))
        lines.append(('Next heartbeat:    '+local_time_text(report.get('telegram_heartbeat_next'))+
                      ('' if usable else ' (UNVERIFIED)'), 'normal'))
        lines.append(('Last heartbeat sent: '+local_time_text(report.get('telegram_heartbeat_last_sent')), 'normal'))
        if report.get('telegram_enabled') is False:
            lines.append(('Heartbeat paused: Telegram alerts are disabled.', 'warn'))
    else:
        lines.append(('Telegram heartbeat: OFF', 'normal'))
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
                  ('q: quit  s: settings  r: refresh  u: updates  arrows/PageUp/PageDown: scroll', 'normal')])
    return lines


def demo_data():
    return (dict(health='ok', stale=False, age_seconds=0, current_voltage=4.075,
                 current_capacity=80.2, charger_present=True, charging_enabled=False,
                 max_capacity=80, control_charging=True, automatic_shutdown=False,
                 min_capacity=20, min_voltage=3.5, outage_limit_seconds=300,
                 telegram_enabled=False,telegram_heartbeat_enabled=False,
                 telegram_heartbeat_hours=24,telegram_heartbeat_start='09:00',
                 battery_temperature=None, shutdown_recommended='', shutdown_initiated=False),
            dict(input_voltage=5.102, cpu_volts=.857, cpu_amps=1.563, pmic_watts=2.529,
                 cpu_temp=50.5, fan_rpm=2655, fan_pwm_percent=30, fan_cooling_state=1,
                 undervoltage_now=False, undervoltage_history=False, throttled_now=False))


def settings_command(minutes, is_root=None):
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError('Enter a positive number of minutes')
    if is_root is None:
        is_root = os.geteuid() == 0
    # Fixed helper and action, validated numeric argument. Never a shell command.
    command = [SETTINGS_HELPER, 'set-shutdown-time', '--minutes', f'{minutes:g}']
    return command if is_root else ['sudo', '--'] + command


def run_settings_helper(minutes, runner=subprocess.run):
    """Helper alone owns config writes and BOTH confirmations, including with sudo."""
    return runner(settings_command(minutes), check=False).returncode


def settings_screen(screen, title, lines):
    height, width = screen.getmaxyx()
    screen.erase()
    for y, text in enumerate([title, ''] + lines):
        if y >= height-1 or width < 2:
            break
        safe = ''.join(char if char.isprintable() else ' ' for char in text)
        try:
            screen.addnstr(y, 0, safe, width-1, curses.A_BOLD if y == 0 else 0)
        except curses.error:
            pass
    screen.refresh()


def settings_menu(screen, report, demo=False):
    """Return a validated time, or None. Merely opening this menu changes nothing."""
    screen.timeout(-1)
    editing = False
    typed = ''
    error = ''
    seconds = report.get('outage_limit_seconds')
    current = seconds/60 if isinstance(seconds, (int, float)) and seconds > 0 else None
    try:
        while True:
            height, width = screen.getmaxyx()
            too_small = height < 12 or width < 40
            if too_small:
                lines = ['Enlarge the terminal for settings.', 'Enter or Esc: return']
            elif demo:
                lines = ['Demo mode: settings changes are disabled.', 'Enter, q or Esc: return']
            elif not editing:
                lines = ['1  Change shutdown time',
                         '2  Fan temperatures and speeds (reboot required)',
                         '3  Telegram heartbeat schedule',
                         '4  Send Telegram test message',
                         'Current limit: ' + (f'{current:g} minutes' if current is not None else 'unavailable / disabled'),
                         'Settings apply to this Pi.',
                         'Battery cutoffs and other controls are preserved.',
                         'Two confirmations; admin permission may be requested.',
                         '1: time  2: fan  3: heartbeat  4: test  Esc: return']
            else:
                lines = ['New maximum time on battery, in minutes.',
                         'Examples: 5, 30, 120; decimal minutes accepted.',
                         'No setting changes until BOTH confirmations pass.',
                         'Minutes: ' + typed, error, 'Enter: review change    Esc: cancel']
            settings_screen(screen, 'Settings' + (' [DEMO]' if demo else ''), lines)
            key = screen.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key == 27 or (not editing and key in (ord('q'), ord('Q'))):
                return None
            if too_small:
                if key in (10, 13, curses.KEY_ENTER):
                    return None
                continue
            if demo:
                if key in (10, 13, curses.KEY_ENTER):
                    return None
                continue
            if not editing:
                if key==ord('2'):
                    return 'fan'
                if key==ord('3'):
                    return 'heartbeat'
                if key==ord('4'):
                    return 'telegram-test'
                if key in (10, 13, curses.KEY_ENTER, ord('1')):
                    editing = True
                continue
            if key in (curses.KEY_BACKSPACE, 127, 8):
                typed = typed[:-1]
                error = ''
            elif key in (10, 13, curses.KEY_ENTER):
                try:
                    minutes = float(typed)
                    settings_command(minutes)  # Shared finite/positive validation.
                    if current is not None and minutes == current:
                        return None
                    return minutes
                except ValueError:
                    error = 'Enter a positive number, for example 120.'
            elif 0 <= key < 128 and chr(key) in '0123456789.eE+-' and len(typed) < 24:
                typed += chr(key)
                error = ''
    finally:
        screen.timeout(100)


def open_settings(screen, report, demo=False):
    try:
        minutes = settings_menu(screen, report, demo)
    except KeyboardInterrupt:
        return 'Settings cancelled; no change saved.'
    if minutes is None:
        return 'Settings closed; no change saved.'
    if minutes=='fan':
        return open_fan_settings(screen)
    if minutes=='heartbeat':
        return open_heartbeat_settings(screen,report)
    if minutes=='telegram-test':
        return open_telegram_test(screen)
    if not Path(SETTINGS_HELPER).is_file():
        return 'Settings helper is missing. Install the updated x120x_control.py first.'
    # Return the terminal to normal for secure sudo and the helper's two prompts.
    # The independent UPS service continues monitoring throughout these prompts.
    curses.def_prog_mode()
    curses.endwin()
    try:
        print('\nDashboard settings: review the change below. Cancel at either confirmation.\n', flush=True)
        result = run_settings_helper(minutes)
        message = ('Shutdown time applied; refreshing service status.' if result == 0 else
                   'Settings cancelled; no change saved.' if result == 2 else
                   'Settings helper failed; review the output and service status.')
        print('\n' + message, flush=True)
        input('Press Enter to return to the dashboard...')
        return message
    except (KeyboardInterrupt, EOFError):
        return 'Settings interrupted; check service status if both confirmations were completed.'
    except OSError:
        return 'Could not start settings helper; check that it and sudo are installed.'
    finally:
        curses.reset_prog_mode()
        screen.clearok(True)
        screen.timeout(100)


def fan_settings_command(curve,is_root=None):
    if is_root is None:
        is_root=os.geteuid()==0
    if curve=='os':
        command=[SETTINGS_HELPER,'restore-os-fan']
    else:
        curve.validate()
        command=[SETTINGS_HELPER,'set-fan-curve','--temperatures',','.join(f'{v:g}' for v in curve.temperatures),
                 '--speeds',','.join(f'{v:g}' for v in curve.speeds),
                 '--hysteresis',','.join(f'{v:g}' for v in curve.hysteresis)]
    return command if is_root else ['sudo','--']+command


def fan_settings_menu(screen):
    from x120x_fan import FanCurve,proposed_curve,FAN_PRESETS
    initial,source=proposed_curve()
    values=list(initial.temperatures)+list(initial.speeds)+[initial.hysteresis[0]]
    fields=[f'Stage {i+1} temperature (C, 30 to 75)' for i in range(4)]+[
            f'Stage {i+1} speed (%, 20 to 100)' for i in range(4)]+[
            'Step-down hysteresis for all stages (C, 1 to 10)']
    index=0; typed=''; error=''; mode_selected=False
    screen.timeout(-1)
    try:
        while True:
            height,width=screen.getmaxyx()
            small=height<10 or width<40
            lines=(['Enlarge terminal for fan settings.','Enter or Esc: cancel'] if small else
                   ['1  OS settings - no project override',
                    '2  Balanced - recommended starting point',
                    '3  Cooler - earlier, stronger airflow',
                    '4  Quiet - later, gentler airflow',
                    '5  Custom - edit temperatures and speeds',
                    'Select to review; two confirmations before saving.',
                    'Reboot required. Enter: Balanced    Esc: cancel'] if not mode_selected else
                   [source,f'Field {index+1} of 9: {fields[index]}',
                    f'Value [{values[index]:g}]: '+typed,
                    'Temperatures increase; speeds do not decrease.',
                    'Stage 4 must reach 100% by 75C or below.',
                    error,'Enter: next/review    Esc: cancel'])
            settings_screen(screen,'OS-managed fan curve - reboot required',lines)
            key=screen.getch()
            if key==27:
                return None
            if key==curses.KEY_RESIZE:
                continue
            if small:
                if key in (10,13,curses.KEY_ENTER):
                    return None
                continue
            if not mode_selected:
                if key==ord('1'):
                    return 'os'
                presets={ord('2'):'balanced',ord('3'):'cooler',ord('4'):'quiet',
                         10:'balanced',13:'balanced',curses.KEY_ENTER:'balanced'}
                if key in presets:
                    return FAN_PRESETS[presets[key]]
                if key==ord('5'):
                    mode_selected=True
                continue
            if key in (curses.KEY_BACKSPACE,127,8):
                typed=typed[:-1]; error=''
            elif key in (10,13,curses.KEY_ENTER):
                try:
                    value=float(typed) if typed else values[index]
                    lower,upper=((30,75) if index<4 else (20,100) if index<8 else (1,10))
                    if not math.isfinite(value) or not lower<=value<=upper:
                        raise ValueError(f'Enter a value from {lower} to {upper}.')
                    if 0<index<4 and value<=values[index-1]:
                        raise ValueError('Temperature must exceed the previous stage.')
                    if 4<index<8 and value<values[index-1]:
                        raise ValueError('Speed must not decrease from the previous stage.')
                    if index==7 and value!=100:
                        raise ValueError('The final stage must be 100%.')
                    values[index]=value
                    if index<8:
                        index+=1; typed=''; error=''
                        continue
                    curve=FanCurve(tuple(values[:4]),tuple(values[4:8]),(values[8],)*4)
                    curve.validate()
                    return curve
                except ValueError as problem:
                    error=str(problem)
                    if index==8:
                        index=0
                    typed=''
            elif 0<=key<128 and chr(key) in '0123456789.-' and len(typed)<16:
                typed+=chr(key); error=''
    finally:
        screen.timeout(100)


def open_fan_settings(screen):
    try:
        curve=fan_settings_menu(screen)
    except ImportError:
        return 'Fan module missing. Install the updated x120x_fan.py first.'
    except KeyboardInterrupt:
        return 'Fan settings cancelled; no change saved.'
    if curve is None:
        return 'Fan settings closed; no change saved.'
    if not Path(SETTINGS_HELPER).is_file():
        return 'Settings helper missing. Install the updated x120x_control.py first.'
    curses.def_prog_mode(); curses.endwin()
    try:
        result=subprocess.run(fan_settings_command(curve),check=False).returncode
        message=('Fan settings saved; reboot required to apply. No reboot requested.' if result==0 else
                 'No fan change applied.' if result==2 else 'Fan settings helper failed; review the output.')
        print('\n'+message,flush=True)
        input('Press Enter to return to the dashboard...')
        return message
    except (KeyboardInterrupt,EOFError):
        return 'Fan settings interrupted; review saved status if both confirmations completed.'
    except OSError:
        return 'Could not start the fan settings helper.'
    finally:
        curses.reset_prog_mode(); screen.clearok(True); screen.timeout(100)


def heartbeat_settings_command(request,is_root=None):
    if is_root is None:
        is_root=os.geteuid()==0
    command=[SETTINGS_HELPER,'set-telegram-heartbeat']
    if request=='off':
        command+=['--disable']
    else:
        from x120x_telegram import heartbeat_time,validate_heartbeat_hours
        hours=validate_heartbeat_hours(request['hours'])
        start=heartbeat_time(request['start'])
        command+=['--hours',str(hours),'--start',start]
    return command if is_root else ['sudo','--']+command


def heartbeat_settings_menu(screen,report):
    from x120x_telegram import heartbeat_time,validate_heartbeat_hours
    interval=report.get('telegram_heartbeat_hours',24)
    start=report.get('telegram_heartbeat_start','09:00')
    try:
        interval=validate_heartbeat_hours(interval)
    except ValueError:
        interval=24
    try:
        start=heartbeat_time(start)
    except ValueError:
        start='09:00'
    phase=0; typed=''; error=''
    screen.timeout(-1)
    try:
        while True:
            height,width=screen.getmaxyx()
            small=height<10 or width<40
            if small:
                lines=['Enlarge the terminal for settings.','Enter or Esc: cancel']
            elif phase==0:
                lines=['1  Enable / change heartbeat schedule','2  Disable heartbeat only',
                       '1 = hourly; 24 = once daily. Pi local time.',
                       'Power-loss / restoration alerts are preserved.',
                       'Two confirmations before saving. Esc: cancel']
            elif phase==1:
                lines=['Interval: whole hours from 1 to 24.',f'Hours [{interval:g}]: '+typed,
                       '1 = once per hour; 24 = once per day.',error,'Enter: next    Esc: cancel']
            else:
                lines=['First message time: 24-hour Pi local clock.',f'First time [{start}]: '+typed,
                       'Pi clock now: '+datetime.now().astimezone().strftime('%H:%M %Z'),
                       'Example: 09:00. If passed, first is tomorrow.',error,'Enter: review    Esc: cancel']
            settings_screen(screen,'Telegram heartbeat',lines)
            key=screen.getch()
            if key==27:
                return None
            if key==curses.KEY_RESIZE:
                continue
            if small:
                if key in (10,13,curses.KEY_ENTER):
                    return None
                continue
            if phase==0:
                if key==ord('2'):
                    return 'off'
                if key in (ord('1'),10,13,curses.KEY_ENTER):
                    phase=1
                continue
            if key in (curses.KEY_BACKSPACE,127,8):
                typed=typed[:-1]; error=''
            elif key in (10,13,curses.KEY_ENTER):
                try:
                    if phase==1:
                        interval=validate_heartbeat_hours(int(typed) if typed else interval)
                        phase=2; typed=''; error=''
                    else:
                        return dict(hours=interval,start=heartbeat_time(typed if typed else start))
                except ValueError:
                    error='Use a whole number 1-24.' if phase==1 else 'Use HH:MM, for example 09:00.'
            elif 0<=key<128 and chr(key) in ('0123456789' if phase==1 else '0123456789:') and len(typed)<8:
                typed+=chr(key); error=''
    finally:
        screen.timeout(100)


def open_heartbeat_settings(screen,report):
    try:
        request=heartbeat_settings_menu(screen,report)
    except ImportError:
        return 'Telegram module missing; install the updated x120x_telegram.py.'
    except KeyboardInterrupt:
        return 'Heartbeat settings cancelled; no change saved.'
    if request is None:
        return 'Heartbeat settings closed; no change saved.'
    if not Path(SETTINGS_HELPER).is_file():
        return 'Settings helper missing; install the updated x120x_control.py.'
    curses.def_prog_mode(); curses.endwin()
    try:
        result=subprocess.run(heartbeat_settings_command(request),check=False).returncode
        message=('Heartbeat setting saved; service restarted.' if result==0 else
                 'Heartbeat change cancelled.' if result==2 else 'Heartbeat setup failed; review the output.')
        print('\n'+message,flush=True)
        input('Press Enter to return to the dashboard...')
        return message
    except (KeyboardInterrupt,EOFError):
        return 'Heartbeat settings interrupted; check status if both confirmations completed.'
    except OSError:
        return 'Could not start the heartbeat settings helper.'
    finally:
        curses.reset_prog_mode(); screen.clearok(True); screen.timeout(100)


def telegram_test_command(is_root=None):
    if is_root is None:
        is_root=os.geteuid()==0
    command=[SETTINGS_HELPER,'test-telegram']
    return command if is_root else ['sudo','--']+command


def open_telegram_test(screen):
    if not Path(SETTINGS_HELPER).is_file():
        return 'Settings helper missing; install the updated x120x_control.py.'
    curses.def_prog_mode(); curses.endwin()
    try:
        print('\nTelegram test sends ONE message after confirmation. No setting or service will change.\n',flush=True)
        result=subprocess.run(telegram_test_command(),check=False).returncode
        message=('Telegram test delivered.' if result==0 else
                 'Telegram test cancelled; no message sent.' if result==2 else
                 'Telegram test failed; check connectivity and private configuration.')
        print('\n'+message,flush=True)
        input('Press Enter to return to the dashboard...')
        return message
    except (KeyboardInterrupt,EOFError):
        return 'Telegram test interrupted; a confirmed message may already have been sent.'
    except OSError:
        return 'Could not start Telegram test helper.'
    finally:
        curses.reset_prog_mode(); screen.clearok(True); screen.timeout(100)


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
    settings_notice = ''
    while True:
        args.update_checker.refresh()
        if time.monotonic() >= refresh_at:
            report, pi = demo_data() if args.demo else (read_status(args.status), pi_metrics())
            content = rows(report, pi, args.demo, args.runtime_model, args.update_checker.snapshot())
            if settings_notice:
                content.insert(3, (settings_notice, 'warn'))
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
            footer = f'v{DASHBOARD_VERSION} | s settings | q quit | u updates | scroll {offset+1}/{len(content)}'
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
        elif key in (ord('s'), ord('S')):
            settings_notice = ('Settings disabled in view-only mode.' if args.view_only else
                               open_settings(screen, report, args.demo))
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
    parser.add_argument('--view-only', action='store_true', help='Disable the settings menu; monitoring only')
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
