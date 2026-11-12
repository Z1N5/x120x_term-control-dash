#!/bin/sh
# Install only. Starting the service or enabling charging is a separate action.
set -eu
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo sh ./install.sh' >&2
    exit 1
fi
if systemctl is-active --quiet x120x_upsd.service; then
    echo 'Stop the existing service first: sudo systemctl stop x120x_upsd.service' >&2
    exit 1
fi
apt-get install -y python3 python3-smbus2 python3-systemd python3-apscheduler raspi-utils
install -d /usr/local/bin /usr/local/etc
install -m 755 x120x_upsd.py /usr/local/bin/x120x_upsd.py
install -m 644 x120x_telegram.py /usr/local/bin/x120x_telegram.py
install -m 755 x120x_dashboard.py /usr/local/bin/x120x-dashboard
install -m 755 x120x_control.py /usr/local/bin/x120x-control
if [ ! -e /usr/local/etc/x120x_upsd.ini ]; then
    install -m 644 x120x_upsd.ini /usr/local/etc/x120x_upsd.ini
else
    echo 'Preserving existing /usr/local/etc/x120x_upsd.ini'
fi
install -m 644 x120x_upsd.service /etc/systemd/system/x120x_upsd.service
python3 /usr/local/bin/x120x_upsd.py --check-config
systemctl daemon-reload
echo 'Installed. The service has NOT been started or enabled by this installer.'
echo 'For monitoring only: sudo systemctl enable --now x120x_upsd.service'
echo 'To enable charging at 80% with two confirmations:'
echo '  sudo x120x-control enable-charging --limit 80'
echo 'To enable shutdown with two confirmations and a chosen runtime, for example 5 minutes:'
echo '  sudo x120x-control enable-shutdown --minutes 5'
echo 'To view: x120x-dashboard'
echo 'Optional Telegram setup (private prompts; never put tokens on the command line):'
echo '  sudo x120x-control enable-telegram'
