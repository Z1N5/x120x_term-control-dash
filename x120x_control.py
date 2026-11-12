#!/usr/bin/env python3
"""Separate two-confirmation opt-ins for charging, shutdown and Telegram alerts."""
import argparse
import configparser
import getpass
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def enable_charging(path, limit, ask=input, apply_service=None):
    if not 50 <= limit <= 100:
        raise ValueError('Charge limit must be between 50 and 100 percent')
    path = Path(path)
    original = path.read_text()
    parser = configparser.ConfigParser()
    parser.read_string(original)
    section = parser['general']
    shutdown_enabled = section.getboolean('automatic_shutdown', fallback=False)
    from x120x_upsd import Settings
    settings = Settings.load(path)
    settings.control_charging = True
    settings.max_charge_capacity = limit
    settings.validate()
    print(f'Configuration: {path}\n'
          f'This will ENABLE automatic battery charging control at {limit}%.\n'
          f'Charging stops at {limit}% and may resume at {limit-3}% (subject to voltage/temperature limits).\n'
          'It will enable/start or restart x120x_upsd on this Pi.\n'
          f'Automatic shutdown remains {"ON" if shutdown_enabled else "OFF"}. Fan settings will not be changed.')
    if ask('Confirmation 1 of 2 - proceed? Type yes: ').strip().lower() != 'yes':
        return False
    phrase = f'ENABLE CHARGING AT {limit}%'
    if ask(f'Confirmation 2 of 2 - type exactly "{phrase}": ').strip() != phrase:
        return False
    return save_settings(path, original, {'control_charging': 'true',
                         'max_charge_capacity': str(limit)}, 'charging', apply_service)


def enable_shutdown(path, minutes, ask=input, apply_service=None):
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError('Battery runtime must be a positive, finite number of minutes')
    path = Path(path)
    original = path.read_text()
    from x120x_upsd import Settings
    settings = Settings.load(path)
    settings.automatic_shutdown = True
    settings.ac_max_downtime = minutes
    settings.min_charge_capacity = 20
    settings.min_voltage = 3.5
    settings.no_power_at_start = 'standard'
    settings.disable_self_protect = False
    settings.shutdown_grace_seconds = 0
    settings.validate()
    duration = f'{minutes:g}'
    print(f'Configuration: {path}\n'
          f'This will ENABLE automatic shutdown after {duration} minutes on battery,\n'
          'or immediately at 20% battery or 3.5V, whichever comes first.\n'
          'There is NO extra grace period. Checks normally run every 2 seconds.\n'
          'The same rules apply when starting the Pi on battery power.\n'
          'External power returning before a shutdown request clears the outage timer.\n'
          'An OS shutdown already requested cannot be reliably cancelled.\n'
          'Charging control and fan settings will not be changed.\n'
          'It will enable/start or restart x120x_upsd on this Pi.\n'
          'If a shutdown condition already applies, the Pi may shut down immediately.')
    if ask('Confirmation 1 of 2 - proceed? Type yes: ').strip().lower() != 'yes':
        return False
    phrase = f'ENABLE SHUTDOWN AFTER {duration} MINUTES'
    if ask(f'Confirmation 2 of 2 - type exactly "{phrase}": ').strip() != phrase:
        return False
    return save_settings(path, original, {
        'automatic_shutdown': 'true', 'ac_max_downtime': duration,
        'min_charge_capacity': '20', 'min_voltage': '3.5',
        'no_power_at_start': 'standard', 'disable_self_protect': 'false',
        'shutdown_grace_seconds': '0'}, 'shutdown', apply_service)


def save_settings(path, original, changes, action, apply_service):
    # Abort if a second editor changed anything while the user was confirming.
    if path.read_text() != original:
        raise RuntimeError('Configuration changed during confirmation; nothing was saved. Retry.')
    # Preserve comments, layout and unrelated sections in the installed configuration.
    lines = original.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip().lower() == '[general]')
    end = next((i for i in range(start+1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    body = ''.join(lines[start+1:end])
    for key, value in changes.items():
        pattern = rf'(?mi)^\s*{key}\s*[:=][^\n]*$'
        if re.search(pattern, body):
            body = re.sub(pattern, f'{key} = {value}', body)
        else:
            body = body.rstrip('\n') + f'\n{key} = {value}\n'
    updated = ''.join(lines[:start+1]) + body + ''.join(lines[end:])
    backup = path.with_name(path.name + '.before-' + action + '-' + str(time.time_ns()))
    shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(updated)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'Saved. Previous settings: {backup}')
    if apply_service:
        apply_service()
    return True


def enable_telegram(path, ask=input, read_secret=getpass.getpass, apply_service=None,
                    credentials_path='/usr/local/etc/x120x-telegram.json'):
    from x120x_upsd import Settings
    from x120x_telegram import TelegramCredentials, private_json
    path = Path(path)
    original = path.read_text()
    settings = Settings.load(path)
    print('This will enable outbound Telegram messages for power loss and restoration.\n'
          'The destination is the numeric chat ID you provide; members of that chat can read alerts.\n'
          'Bot credentials stay in a private file on this Pi, not in the repository.\n'
          'Network failures queue messages for retry without delaying UPS monitoring.\n'
          f'Charging remains {"ON" if settings.control_charging else "OFF"}; '
          f'automatic shutdown remains {"ON" if settings.automatic_shutdown else "OFF"}.\n'
          'This will enable/start or restart the service. No test message is sent by setup.')
    if ask('Confirmation 1 of 2 - proceed? Type yes: ').strip().lower() != 'yes':
        return False
    if ask('Confirmation 2 of 2 - type exactly "ENABLE TELEGRAM ALERTS": ').strip() != 'ENABLE TELEGRAM ALERTS':
        return False
    credentials = TelegramCredentials(read_secret('Bot token (hidden): ').strip(),
                                      read_secret('Destination chat ID (hidden): ').strip())
    credentials.validate()
    def apply():
        private_json(credentials_path, dict(bot_token=credentials.bot_token, chat_id=credentials.chat_id))
        if apply_service:
            apply_service()
    return save_settings(path, original, {'telegram_enabled': 'true',
        'telegram_config_file': str(credentials_path)}, 'telegram', apply)


def test_telegram(path, ask=input, sender=None):
    from x120x_upsd import Settings
    from x120x_telegram import TelegramCredentials, send_message
    credentials = TelegramCredentials.load(Settings.load(path).telegram_config_file)
    if ask('Send ONE Telegram test message to the configured chat? Type yes: ').strip().lower() != 'yes':
        return False
    (send_message if sender is None else sender)(credentials,
                'Raspberry Pi UPS: Telegram power alerts test. No hardware settings were changed.')
    print('Telegram test delivered.')
    return True


def start_service():
    subprocess.run(['systemctl', 'enable', 'x120x_upsd.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'x120x_upsd.service'], check=True)
    print('Settings applied. Check x120x-dashboard for live status.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('enable-charging').add_argument('--limit', type=int, default=80)
    actions.add_parser('enable-shutdown').add_argument('--minutes', type=float, required=True)
    actions.add_parser('enable-telegram')
    actions.add_parser('test-telegram')
    actions.add_parser('telegram-chat-id')
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error('An interactive terminal is required for both confirmations')
    if os.geteuid() != 0:
        parser.error('Run with sudo to edit the installed configuration')
    try:
        path = '/usr/local/etc/x120x_upsd.ini'
        if args.action == 'enable-charging':
            completed = enable_charging(path, args.limit, apply_service=start_service)
        elif args.action == 'enable-shutdown':
            completed = enable_shutdown(path, args.minutes, apply_service=start_service)
        elif args.action == 'enable-telegram':
            completed = enable_telegram(path, apply_service=start_service)
        elif args.action == 'test-telegram':
            completed = test_telegram(path)
        else:
            from x120x_telegram import recent_chat_ids
            token = getpass.getpass('Bot token (hidden; not saved): ').strip()
            ids = recent_chat_ids(token)
            print('Recent chat IDs (local display only): ' + (', '.join(ids) if ids else
                  'None found. Send /start to the bot and retry.'))
            return 0
        if not completed:
            print('Cancelled. No settings were changed.')
        return 0
    except EOFError:
        print('\nCancelled before completing confirmation. No settings were changed.')
        return 0
    except KeyboardInterrupt:
        print('\nInterrupted. If both confirmations were completed, check the saved configuration and service status.')
        return 1
    except (OSError, ValueError, RuntimeError, configparser.Error, subprocess.SubprocessError) as error:
        print(f'Could not complete enablement: {error}', file=sys.stderr)
        return 1
    except Exception:
        # Network exceptions must never disclose token-bearing Telegram URLs.
        print('Telegram operation failed. Check connectivity and private configuration; details withheld.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
