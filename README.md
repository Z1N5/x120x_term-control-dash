# x120x_term-control-dash

X120X UPS monitoring, opt-in control and a terminal dashboard for Raspberry Pi OS Lite.

Modified from [ArjenR/x120x_upsd](https://github.com/ArjenR/x120x_upsd), upstream
commit `2ab69840a628f1b2ea705f03c71c89defede3c41`. Original author: ArjenR and
upstream contributors. Modifications dated 2026-11-12. Distributed under the
original [CC BY-SA 4.0 licence](LICENSE).

This version supports a Pi 5 with a Suptronics/Geekworm X120X UPS, Raspberry Pi
OS Lite, and the official Active Cooler. No desktop, Qt, web server or pip
installation is needed for the standard setup. The service is the only hardware
controller. Any number of read-only terminal dashboards can observe it over SSH.

## Defaults and explicit permissions

- Installation copies files only. It does not start or enable the service.
- The supplied configuration is monitor-only: charging control OFF, automatic
  shutdown OFF. Existing configurations are preserved; missing new control flags
  default to OFF. Monitoring does not change GPIO configuration or charging.
- The charging-enable command requires TWO confirmations. Only after both does
  it save the charge limit and enable/start or restart the service. It saves a
  backup of the configuration first. Cancelling either prompt changes nothing.
- The default charge target is 80%, with a 3 percentage-point gap: stop at
  80%, resume at 77%. Sampling and fuel-gauge accuracy mean this is not an exact
  hard limit. An optional voltage ceiling can also restrict charging.
- Charging and automatic shutdown have separate two-confirmation enable commands.
  Enabling or adjusting either preserves the other setting. Shutdown remains OFF
  until its own enable command is confirmed; warnings alone do not shut down.
- The dashboard has NO settings-changing keys. No fan configuration, boot
  configuration, EEPROM, USB power limit or power-supply settings are changed.
- Explicitly editing `control_charging` in the configuration is a manual override
  of the supplied confirmation workflow. Use the helper for normal enablement.

## Install on your Pi

On the Raspberry Pi, clone the public project and install:

```sh
git clone https://github.com/Z1N5/x120x_term-control-dash.git
cd x120x_term-control-dash
sudo apt-get update
sudo sh ./install.sh
```

The installer does not start the service or enable either control mode.

The installer refuses to overwrite a running x120x_upsd service. If you already
run the upstream daemon, stop that service yourself before installation:

```sh
sudo systemctl stop x120x_upsd.service
```

Stop any running Suptronics `qtx120xTerminal.py`, `qtx120x.py`, `bat.py`, or
`merged*.py` controller too, including copies launched by cron/systemd. The new
service lock prevents duplicate instances of this version; it cannot prevent
an unrelated original script from changing the same GPIO or requesting shutdown.
Do not run both controllers together. If an original script has already
scheduled an OS shutdown, this version does not cancel that separate request.

I2C must already be enabled, with the UPS fuel gauge at bus 1, address `0x36`.
This installer does not change those settings. If GPIO 6 isn't already an input,
the service reports an error rather than silently reconfiguring it.

### Enable 80% charging control

```sh
sudo x120x-control enable-charging --limit 80
```

1. Read the action summary, then type `yes` at confirmation 1.
2. At confirmation 2, type exactly `ENABLE CHARGING AT 80%`.

After both confirmations, the helper saves the configuration and enables/starts
(or restarts) the service. The existing automatic-shutdown setting is preserved
and displayed in the action summary. Run this command on the Raspberry Pi when ready. To choose 85% instead, use `--limit 85`; the
confirmation phrase and resume point change accordingly.

View the dashboard:

```sh
x120x-dashboard
```

Use `q` to quit, `r` to refresh, and arrows or PageUp/PageDown to scroll. It adapts
to smaller SSH terminals. Quitting the dashboard leaves the service running.
On systems that restrict firmware access, `sudo x120x-dashboard` may be needed
for Pi metrics; the dashboard remains read-only.

### Enable automatic shutdown with your chosen battery runtime

After installing this updated package, choose the maximum number of minutes
on battery. For example, for five minutes:

```sh
sudo x120x-control enable-shutdown --minutes 5
```

Replace `5` with your desired positive number of minutes; decimal values are
accepted. This same command can change the runtime later, again requiring both
confirmations. It applies this policy:

- Shut down after the chosen duration on battery, OR
- shut down immediately at **20% battery capacity or 3.5V**, whichever comes first.
- There is **no additional grace period**, even if an old configuration contains
  `shutdown_grace_seconds=300`. The daemon checks conditions every two seconds
  by default, so immediate means on the next successful sample, followed by the
  OS's normal shutdown process.
- External power returning before the shutdown request clears the outage timer.
  An OS shutdown already requested cannot be reliably cancelled.
- The rules also apply when booting on battery. This command selects the standard
  startup policy so old startup overrides cannot suppress these thresholds.
- Charging control, its target and fan settings are preserved.

At the prompts, type `yes`, then exactly `ENABLE SHUTDOWN AFTER 5 MINUTES` (the
number in the phrase matches your chosen runtime). Neither settings nor service
state changes if either confirmation is cancelled. A configuration backup is
saved after confirmation, then the service is enabled/started or restarted.
If the Pi is already on battery and a condition applies, it can shut down as soon
as that service starts. Review the displayed summary before confirming.

For an existing installation, stop the old service before rerunning `install.sh`;
it preserves your installed configuration. Then run the new enable command.
Do not simply edit `automatic_shutdown=true` to migrate an earlier package:
use the helper to apply the new thresholds and clear startup overrides together.

### Start with observation only instead

Leave both control flags false, then explicitly start the service:

```sh
sudo systemctl enable --now x120x_upsd.service
x120x-dashboard
```

Observation mode preserves the hardware's existing charge state. It does not
force charging on or off. Likewise, stopping the service leaves the last GPIO
charge setting in place; stopping management is not the same as restoring the
board's default charging behaviour.

## What the dashboard shows

- UPS battery voltage, capacity, battery bar and charge-enable switch state.
- Charge target and whether automatic charging and shutdown are enabled.
- External power present/lost, outage duration and shutdown conditions.
- Pi input voltage, CPU voltage/current and PMIC rail power estimate.
- CPU temperature, Active Cooler RPM, PWM duty/cooling state when exposed by OS.
- Undervoltage now, undervoltage since boot and current throttling indicators.
- Service health, data freshness and hardware read errors.

`Charging switch ENABLED` means the board has permission to charge; it does not
measure charging current. The single external-power signal cannot distinguish
an AC outage from a failed/unplugged power adapter. PMIC rail watts are an
estimate, not wall power or total Pi/SSD consumption. Unsupported metrics are
shown as `Unavailable`, not zero. A fan reading of zero is a valid stopped-fan
reading and can be normal below the OS temperature threshold.

Demo mode uses labelled sample data, without hardware access:

```sh
python3 x120x_dashboard.py --demo
python3 x120x_dashboard.py --demo --once
```

## Optional Telegram power alerts

Telegram alerts are **disabled by default** and outbound-only. They cannot
receive commands or change charging, shutdown or fan settings. Enabling alerts
preserves the existing charging and shutdown policies.

1. Create a dedicated bot with [Telegram BotFather](https://t.me/BotFather) using
   `/newbot`. Keep the token private; never paste it into GitHub, issues or chat.
2. Open the bot in Telegram and send `/start`. For a group, add the bot and send
   a command to it there. For a channel, the bot must have permission to post.
3. To discover recent destination chat IDs without putting a token in your shell
   history, run on the Pi:

   ```sh
   sudo x120x-control telegram-chat-id
   ```

   Enter the token at the hidden prompt. The command queries the bot's recent
   updates and prints only numeric chat IDs locally. It does not save the token
   or send a message. Use a dedicated bot without an existing webhook/other
   polling application; updates may be unavailable otherwise. If several IDs
   appear, identify the intended destination before enabling alerts.
4. Configure and enable alerts:

   ```sh
   sudo x120x-control enable-telegram
   ```

   Confirm `yes`, then `ENABLE TELEGRAM ALERTS`. Enter the bot token and numeric
   destination chat ID at the two hidden prompts. Secrets are written only after
   confirmation, to `/usr/local/etc/x120x-telegram.json`, mode **0600**, owned by
   the service user (normally root). They are not stored in the public INI file,
   dashboard status JSON, logs, command-line arguments or Git repository.
   The service is then enabled/started or restarted. No setup test is sent.
5. Optionally send one real test message:

   ```sh
   sudo x120x-control test-telegram
   ```

   It sends only after a separate `yes` confirmation. All automated test-suite
   deliveries are mocked; they never contact Telegram.

Messages report **external power lost / running on battery** and **external
power restored**, with the event time in UTC, battery percentage and voltage.
Restore notices include outage duration. Notices use the generic label
`Raspberry Pi UPS`; the machine hostname is not sent. Members of the configured
chat can read the notices. The signal cannot distinguish a utility outage from
an unplugged or failed adapter.

Only changes trigger alerts; repeated polls do not spam. The first sample
establishes a baseline, so a new installation already on battery does not invent
an outage time or send a false loss event. The last observed power state is saved
with the outbox to detect changes across service/Pi restarts. It cannot reconstruct
multiple transitions that happened while the Pi was off.

Delivery runs in a separate bounded worker: no network request is made by the
UPS monitoring loop. Errors retry with backoff (including Telegram rate limits).
A private, persistent outbox at `/var/lib/x120x-ups/telegram-outbox.json` retains up
to 64 notices, oldest first; overflow drops oldest messages and increments the
visible dropped counter. Its parent directory is mode **0700**, its file is
**0600**. systemd creates the directory. Foreground use must provide a similarly
private directory. The dashboard shows delivery state and pending/dropped counts,
never message contents or credentials. A failed notification setup or worker
does not stop charging/shutdown protection.

If the router or internet goes down too, messages wait for connectivity and for
the Pi to be running. Stored messages can be delivered after a reboot, with their
original event timestamps. Shutdown never waits for message delivery. Delivery is
best effort: Telegram may accept a message just before a connection fails, so a
retry can duplicate it. A changed bot token or destination deliberately discards
that previous recipient's queue instead of replaying private history to a new
recipient. Disabling alerts leaves the private outbox in place; delete it locally
if those pending messages should not be sent on re-enablement.

To disable alerts, set `telegram_enabled=false` in the installed configuration
and restart the service. To rotate credentials or change destination, rerun the
double-confirmed setup. GitHub secret scanning and ignore rules are safeguards,
not a substitute for keeping these files outside the checkout.

[Official Telegram bot setup](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
· [Bot API](https://core.telegram.org/bots/api#sendmessage)

## Active Cooler settings

Your official Active Cooler already uses OS temperature-based PWM control and
has a tachometer for RPM. This version observes it; it never overrides its fan
pins or writes to fan sysfs controls. A separate cell-mounted sensor would be
needed to measure battery temperature; CPU temperature is not battery temperature.

Raspberry Pi supports changing the fan curve using `dtparam=fan_temp0` through
`fan_temp3`, associated `_speed` values (0–255 PWM), and `_hyst` values in
`/boot/firmware/config.txt`, followed by a reboot. Temperature and hysteresis
values are in millidegrees Celsius. These settings can be planned separately
if you want different noise/cooling behaviour. No edits are included or applied.

References:
- [Official Active Cooler](https://www.raspberrypi.com/products/active-cooler/)
- [Official fan configuration parameters](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)

## Official 45W supply and SSD

The [official supply specifications](https://www.raspberrypi.com/products/45w-power-supply/)
include 5.1V at up to 5A (25.5W at that profile). Its 45W rating applies at other
USB-C PD profiles. The connected device draws the current it needs, within the
negotiated profile. A 45W label alone does not mean more power than the official
27W supply can provide at the Pi's 5V profile. Available SSD power also depends
on the UPS model, wiring, USB/PCIe adapter and other loads. The dashboard does not
claim to identify the supply or measure SSD power. Undervoltage indicators can
help diagnose problems, but cannot prove all SSD power issues are absent.

## How the upstream issues were fixed

The independent worker threads were replaced by one supervised sampling and
decision loop. It reads real UPS data, evaluates the policy, publishes an atomic
JSON report and only then sends a watchdog heartbeat. Hardware errors produce an
error status and a non-zero exit; systemd can restart the service. The watchdog
also handles a hung loop. There are no unmonitored charge/protection threads.

- Warm-up is a time condition, so crossing the threshold permits charging on the
  next cycle. Battery protection and outage tracking continue during warm-up.
- Outage time starts at detection regardless of whether the battery needs charge.
- Both special battery-start policies have defined state and live monitoring.
  `run_till_minimums` ignores the outage timeout but checks configured minimums.
  `run_till_protect` uses emergency thresholds only. Both revert to standard
  behaviour once external power is restored.
- With automatic shutdown explicitly enabled, the runtime limit and battery
  minimums trigger an OS shutdown immediately on the first qualifying sample.
  There is no second grace period. Returning external power before a request
  resets outage tracking; shutdown already handed to the OS is not cancelled.
- Outage start time is atomically saved on tmpfs. A restart on the same boot
  resumes elapsed battery time; a new boot discards it. Thresholds are evaluated
  against fresh readings after restart. Old grace deadlines are not inherited.
  If the service is stopped, it cannot issue shutdowns while stopped; a later
  restart resumes saved outage time. An updated startup policy is respected.
- Charging uses the vendor-documented active-low GPIO 16 output. In observation
  mode it is only read. OS fan control is never overridden.
- JSON readers cannot see partial writes. Old, stopped, failed and cross-boot
  reports are clearly marked unverified.
- Configuration validation rejects invalid thresholds and periods. The legacy
  `json_report_period` is accepted but reports now follow `poll_seconds`.
  `pid_file` is replaced by a single-instance lock. Cron journal reporting is
  retained without extra hardware-reading threads. Optional DHT sensors remain
  supported but need separately installed Adafruit packages and an external
  resistor. A configured sensor failing to read blocks automated charging.

This is a substantial refactor of the small upstream daemon, rather than a
minimal patch. The upstream commit ID above identifies its starting point.

## Configuration and troubleshooting

Configuration: `/usr/local/etc/x120x_upsd.ini`.
Default status: `/run/x120x-ups/status.json`.
Service logs:

```sh
systemctl status x120x_upsd.service
journalctl -u x120x_upsd.service -n 50 --no-pager
sudo python3 /usr/local/bin/x120x_upsd.py --check-config
```

Existing configurations are kept. If you previously set `json_report_file` to
`/run` (the original example), change it to a filename such as
`/run/x120x-ups/status.json` before starting. If you deliberately retain another
status path, pass it to `x120x-dashboard --status /your/status.json`.
Charging limits should be reviewed against your installed cells and UPS model.
Automatic shutdown is OFF until enabled separately. Use the double-confirmed
`x120x-control enable-shutdown --minutes 5` command (with your chosen runtime).
Enabling charging does not change your existing automatic-shutdown setting.
To stop automated charging management, explicitly set `control_charging=false`
and restart the service. This preserves the current hardware charge switch state.

## Verification and limits

Run the hardware-free regression suite from the extracted folder:

```sh
python3 -m unittest discover -s tests -v
```

Tests exercise observation-only behaviour, charging thresholds and hysteresis,
warm-up completion, both battery-start policies, full-battery outage timing,
recovery/cancellation, restart persistence, read failures, dashboard parsers,
data freshness, and both confirmation gates. All shutdown and GPIO actions in
these tests are mocked. The live curses demo was also exercised at 80×24 and
42×12 terminal sizes.

Development verification uses simulated hardware; real Pi hardware has not been validated. On-device
validation of I2C, GPIO polarity and observed charging response is still needed.
Begin on external power and check the displayed states before relying on it.

## Public access and private deployment data

This is a public project: anyone can view, copy and fork it under the included
licence. Only the owner and explicitly authorised collaborators can push to the
canonical repository. Fork owners can commit to their own copies and propose
pull requests; a pull request does not itself change the canonical branch.

Do not put credentials, private service URLs, device usernames/hostnames, personal
email addresses or real deployment logs in the repository. Keep private material
outside the checkout, in a separately secured local directory or a private
repository. A public repository cannot contain a genuinely private subfolder.
The ignore rules are a convenience, not an access control: forced adds or already
tracked files can still publish ignored content. Inspect staged changes before
pushing. GitHub credentials belong in a credential manager. Core UPS monitoring needs no
API keys; optional Telegram notifications use a separate private bot token. Public upstream, licence and documentation links remain
for attribution and installation.
