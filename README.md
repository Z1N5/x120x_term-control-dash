# x120x_term-control-dash

X120X UPS monitoring, opt-in control and a terminal dashboard for Raspberry Pi OS Lite.

Modified from [ArjenR/x120x_upsd](https://github.com/ArjenR/x120x_upsd), upstream
commit `2ab69840a628f1b2ea705f03c71c89defede3c41`. Original author: ArjenR and
upstream contributors. Modifications dated 2026-11-12. Distributed under the
original [CC BY-SA 4.0 licence](LICENSE).

This version supports a Pi 5 with a Suptronics/Geekworm X120X UPS, Raspberry Pi
OS Lite, and the official Active Cooler. No desktop, Qt, web server or pip
installation is needed for the standard setup. The service is the only hardware
controller. Any number of terminal dashboards can observe it over SSH; settings
changes use a separate privileged helper and explicit double confirmation.

## Defaults and explicit permissions

- Installation copies files only. It does not start or enable the service.
- The supplied configuration is monitor-only: charging control OFF, automatic
  shutdown OFF. Existing configurations are preserved; missing new control flags
  default to OFF. Before explicit input preparation, observation does not change
  GPIO configuration or charging. The dashboard itself never changes GPIO.
- The charging-enable command requires TWO confirmations. Only after both does
  it save the charge limit and enable/start or restart the service. It saves a
  backup of the configuration first. Cancelling either prompt changes nothing.
- The default charge target is 80%, with a 3 percentage-point gap: stop at
  80%, resume at 77%. Sampling and fuel-gauge accuracy mean this is not an exact
  hard limit. An optional voltage ceiling can also restrict charging.
- Charging and automatic shutdown have separate two-confirmation enable commands.
  Enabling or adjusting either preserves the other setting. Shutdown remains OFF
  until its own enable command is confirmed; warnings alone do not shut down.
- The dashboard menu changes charge target, shutdown time or the Pi 5 fan curve through a
  privileged helper, after two confirmations. Fan changes modify only a managed
  block in the boot configuration and require a reboot. EEPROM, USB power limits
  and power-supply settings are not changed. Use `--view-only` to disable the menu.
- Explicitly editing `control_charging` in the configuration is a manual override
  of the supplied confirmation workflow. Use the helper for normal enablement.

## Choose the saved battery charge target (v1.5.0)

Press **s → 5 (Battery charge target)**. Choose **80%, 85%, 90%, 100%** or
**Custom** (a whole percentage from 50 to 100). The current saved target is shown;
opening the menu or cancelling does not change it. Selecting the current target
returns without a restart. The shipped default remains 80%; existing installations
keep their configured value until the user changes it.

The privileged helper displays the old/new target and charging-control state,
then requires both `yes` and the exact phrase, for example:

```text
SET CHARGE TARGET TO 100%
```

Only the saved target changes. Charging control remains ON or OFF as before;
saving 100% does **not** implicitly enable it. Shutdown time, low-battery cutoffs,
Telegram, heartbeat, fan and GPIO input permissions are preserved. A backup is
saved before the UPS service restarts; boot enablement and elapsed outage time
are preserved. If charging control is already ON, the new target can immediately
change whether charging is permitted. A lower target does not actively discharge
the pack. Charging may resume three percentage points below the target, so a
100% target uses a 97% resume point. Existing voltage/temperature limits continue
to apply, and gauge accuracy means that a displayed target is not a precise
physical limit or a guarantee that the pack will reach it.

The same double-confirmed change is available from a terminal:

```sh
sudo x120x-control set-charge-target --limit 100
```

Install v1.5.0 using the update steps below to add both this menu and its helper.

## Passive battery health / backup performance (v1.5.0)

The dashboard separates **battery charge**, **estimated battery runtime**, the
**shutdown timer**, and **observed backup performance**. The X120X gauge does not
provide measured full-charge capacity or a battery state-of-health percentage.
The new section therefore says `Battery health %: Not measurable with this UPS
gauge` and reports a performance trend instead. It cannot diagnose an individual
cell or certify that the batteries are healthy.

Passive recording starts with the monitoring service. It uses normal battery
readings only: there are no automatic discharge tests, GPIO writes, network calls,
configuration changes or changes to charging/shutdown decisions. It does not
adjust the existing runtime estimate or extend the shutdown timer.

- A recorded run must contain at least **15 minutes** of continuously observed
  battery use and a drop of at least **10 percentage points**. A run ends when
  external power returns or the service stops cleanly. The final battery sample
  is used; readings after charging resumes are excluded.
- Sampling gaps longer than the greater of 60 seconds or three polling intervals,
  reversed/duplicate monitoring times, invalid readings, charge rebounds above
  two percentage points and single-sample drops above five points discard the
  active segment. Recording can begin again with fresh valid readings. Small
  rebounds are ignored rather than counted as extra capacity.
- The **first three comparable runs** form a fixed baseline. Their starting and
  ending charge must each be within five percentage points of the first run.
  The comparison uses the **latest three later runs** within that same range.
  Other qualifying runs remain in the history but do not affect that comparison.
  If the latest recorded run uses a different range (for example after changing
  the charge target), the display returns to Learning instead of reusing an old
  positive trend for that different range. The original baseline stays intact.
- Until all six observations exist, the trend says **Learning**. It compares
  median observed minutes per percentage point: under 80% of baseline is labelled
  **Shorter backup performance - possible decline or higher load**; above 125%
  is **Longer backup performance**; otherwise it is **Similar backup performance**.
  These are conservative display heuristics, not measured health percentages.
  CPU/SSD workload, temperature, gauge accuracy and battery ageing all affect them.

The main screen shows recorded-run counts, baseline/comparison progress, the
reference charge range, the last recorded run and the current battery-run progress.
Do not deliberately drain the batteries just to populate this section. The
existing low-battery cutoffs and timer continue to apply throughout normal outages.

History is saved only when a qualifying run ends, in
`/var/lib/x120x-ups/battery-performance.json` (0600, inside the private 0700 service
state directory). It holds at most 60 recent run summaries plus the fixed baseline;
no bot keys, chat IDs or hostnames are stored. Completed records survive reboot.
An unfinished segment is lost after an abrupt stop; different boots are never
joined into a fictional continuous run. An orderly shutdown can save a qualifying
segment. Storage errors disable this optional recording and are logged without
stopping UPS protection. Corrupt history is preserved for inspection instead of
silently replaced. After replacing batteries, archive/reset this history with the
service stopped so the new cells do not inherit the old baseline.

For an existing installation, keep external power connected and quit the dashboard
with `q`, then update both the service and dashboard:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo systemctl stop x120x_upsd.service
sudo sh ./install.sh
sudo systemctl reset-failed x120x_upsd.service
sudo systemctl start x120x_upsd.service
x120x-dashboard
```

Existing configuration, charging/shutdown permissions, Telegram credentials and
fan settings are preserved. No reboot or additional confirmation is required
for passive recording; changes to controls still use their existing confirmations.

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
This installer does not change I2C, boot or EEPROM settings. Pi 5 GPIOs can return
to `none` mode on reboot. The power-sense input must therefore be prepared on
service startup, but only with explicit permission.

### Prepare power monitoring once, including future reboots

```sh
sudo x120x-control enable-monitoring
```

Confirm `yes`, then exactly `ENABLE POWER MONITORING`. This saves
`initialise_power_input=true` and enables/starts or restarts the service.
On each subsequent service start, GPIO 6 is configured as an input with a pull-up
(`pinctrl set 6 ip pu`), matching the vendor's power-loss input. No output level is
driven on GPIO 6. This step does not configure GPIO 16, change fan settings or
alter charging, shutdown or Telegram enablement; those existing settings are
preserved and shown in the summary.

The charging, shutdown and Telegram enable commands also disclose and authorise
this input preparation within their own two-confirmation workflows. Existing
installations made before this fix should run `enable-monitoring` once after
updating to save permission without changing their existing policies.

If the pin is not an input and permission is absent, the service refuses to guess
the power state. Installing the update alone does not grant this permission.

GPIO 16 can initially appear as `no ... | --` (`none`) on Pi 5. Monitor-only mode
accepts this and displays the charging switch as `Unknown`; it does not configure
the pin. After the double-confirmed charging opt-in, the policy configures GPIO 16
according to the charge limits. An unknown initial switch in the 77–80% hysteresis
band is conservatively initialised to disabled until the resume threshold is met.

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
for Pi metrics. Viewing readings never changes settings; the settings menu
requires its own two confirmations even when running as root.

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

Leave both charging/shutdown control flags false, then authorise power-input
preparation and start monitoring with its two confirmations:

```sh
sudo x120x-control enable-monitoring
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

## Settings menu: shutdown time

From **v1.1.0**, press **s** in the dashboard to open settings. Select
**Change shutdown time** (Enter or 1), enter the new maximum minutes on battery,
and press Enter. Positive decimal minutes are accepted; Esc cancels. Entering
the existing value returns without saving or restarting anything.

The dashboard then opens the trusted settings helper in the same terminal. You
do not type a separate terminal command. A normal user may see the standard sudo
password prompt; the dashboard does not receive or save that password. Root users
still have to complete both confirmations:

1. Review the current and proposed time, shutdown enablement and battery cutoffs.
   Type `yes` at confirmation 1.
2. Type the exact phrase shown at confirmation 2, for example
   `SET SHUTDOWN TIMER TO 120 MINUTES`.

Neither cancellation saves a file or restarts the service. After both pass, the
helper backs up and atomically updates the configuration, then restarts the
service. Press Enter to return to the live dashboard and verify the new limit.
If restart fails, review the message: the configuration may already have been
saved, and the dashboard will show the actual service state.

This action changes **only** the maximum-time setting. Existing battery cutoffs
(including the usual 20% / 3.5V), charging, Telegram, input-initialisation permission,
startup policy and boot enablement are preserved. If shutdown is OFF it stays OFF.
The helper does not enable shutdown or reset other users' customised thresholds.
The UPS service keeps monitoring while you review the menu and prompts. Elapsed
outage time is preserved across the subsequent restart: choosing a shorter time
than has already elapsed on battery can request shutdown immediately.

Demo mode cannot apply settings. Very small terminals must be enlarged before
editing. To disable all dashboard settings actions, use:

```sh
x120x-dashboard --view-only
```

For existing installations, update **both** the dashboard and control helper:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo install -m 755 x120x_control.py /usr/local/bin/x120x-control
sudo install -m 755 x120x_dashboard.py /usr/local/bin/x120x-dashboard
x120x-dashboard
```

These installation commands do not restart the UPS service. Only a double-confirmed
settings change restarts it. The control helper also exposes the same action for
terminal users as `sudo x120x-control set-shutdown-time --minutes 120`.

## Dashboard version and update checks

The dashboard displays its embedded version at the top and in the terminal footer.
This identifies the running dashboard file, not merely the Git checkout's state.
The first numbered dashboard publication is **v1.0.0**.

Interactive mode checks the public GitHub `VERSION` file in a background thread
on startup and every six hours. Press **u** to request another check (with a
10-second cooldown). It displays whether the numbered publication is current,
a newer version is available, or the check could not be completed. A failed check
never claims the installation is up to date. Version matching is not a checksum
verification of local changes or a check of the daemon's installed files.

Only a short public version string is fetched. No GitHub authentication, bot
credentials or device information are sent. No code is fetched or executed, no
files are written, and nothing is installed or restarted automatically. Network
problems cannot block the dashboard or UPS service.

```sh
x120x-dashboard --version
x120x-dashboard --check-updates
x120x-dashboard --no-update-check
```

`--check-updates` prints installed/update status and exits without hardware
access. `--no-update-check` prevents dashboard GitHub requests, including via the
**u** key. Demo and `--once` modes are offline by default. Normal mode checks do
not change UPS settings.

To install only the latest dashboard, keeping the service running:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo install -m 755 x120x_dashboard.py /usr/local/bin/x120x-dashboard
x120x-dashboard
```

For maintainers: increment both `DASHBOARD_VERSION` in `x120x_dashboard.py` and
`VERSION` together for each numbered publication. The version synchronisation
test enforces that they agree. The version file tracks numbered publications on
`main`; unversioned commits do not produce a newer-version notice. There is no
Git dependency at runtime and the update checker does not run `git pull`.

The bottom of the dashboard links to this project and credits the public source
projects [ArjenR/x120x_upsd](https://github.com/ArjenR/x120x_upsd) and
[Suptronics/x120x](https://github.com/suptronics/x120x).

**Created by z1n5 — [zing.co.uk](https://zing.co.uk).**

## Estimated battery runtime versus shutdown timer

The dashboard shows two independent values:

- **Est. battery runtime** (on battery) or **Est. battery backup** (on mains): a
  rough estimate of energy available above the configured percentage reserve.
- **Shutdown timer limit / remaining**: the actual maximum-time-on-battery policy.
  A two-hour battery estimate does not extend a five-minute shutdown timer.

The display-only estimate defaults to two 3000mAh, 3.7V cells (22.2Wh nominal), an
**assumed total load of 5W** and **assumed conversion efficiency of 85%**. The
assumptions appear next to the estimate; they are not measurements. It uses the
current battery percentage, even when that is above the configured charge target.
At 80% charge with a 20% reserve, the model gives about 2h 15m. Durations are rounded
to five minutes to avoid implying measurement precision.

```text
hours = nominal_pack_Wh × max(0, current_percent - reserve_percent) / 100
        × assumed_efficiency / assumed_total_load_W
```

The 3.5V limit may be reached before the percentage reserve, so actual usable time
can be shorter. Battery age, fuel-gauge error, temperature and changing SSD/CPU
loads are not modelled. If the configured percentage or voltage threshold is
already reached, the estimate is zero. Missing, stale or failed service data gives
`Unavailable`. This is not a measured time-to-empty or a guarantee of backup time.
It does not use the PMIC rail estimate as total system power.

To change only the displayed assumptions:

```sh
x120x-dashboard --battery-wh 22.2 --load-watts 5 --efficiency 0.85
```

These options do not write configuration or affect charging, shutdown, GPIO or
Telegram. Use a measured total-load figure if available; do not substitute the
incomplete PMIC reading for total Pi/SSD power.

For an existing installation, this feature can be updated without stopping the
UPS service. After restoring external power and reconnecting to the Pi:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo install -m 755 x120x_dashboard.py /usr/local/bin/x120x-dashboard
x120x-dashboard
```

Only the dashboard is replaced by those commands; no service restart is needed.

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

### Scheduled Telegram heartbeats and an immediate test

From **v1.4.0**, the main screen shows heartbeat enablement, interval, first
clock time, next scheduled send and last successful heartbeat delivery. It uses
the same private bot credentials/chat as power alerts, and is **OFF by default**.

Press **s → 3** for the heartbeat menu:

1. Choose **Enable / change heartbeat schedule**.
2. Enter a whole number **1–24**: 1 is once per hour, 6 is every six hours,
   and 24 is once per day.
3. Enter the first-message time as **HH:MM** in 24-hour **Pi local time**.
   The menu displays the Pi's current clock and timezone. If the time has passed
   when the schedule is activated, its first occurrence is tomorrow.
4. Review the action and confirm `yes`, then the exact phrase shown, for example
   `ENABLE TELEGRAM HEARTBEAT EVERY 6 HOURS AT 09:00`.

The configuration is backed up and saved, then the service restarts. Other UPS,
fan and Telegram alert settings and service boot enablement are preserved.
Telegram alerts must already be configured/enabled. Choosing **Disable heartbeat
only** requires `yes` and `DISABLE TELEGRAM HEARTBEAT`; power alerts continue.

After the first message, intervals 1–23 follow elapsed hours continuously, not a
clock-time list restarted at midnight. A daily interval of 24 keeps the chosen
local time across daylight-saving changes. A nonexistent local clock time moves
forward through the DST gap; a repeated clock time is used once. Future schedules
and pending messages are saved privately and survive service/Pi restarts. A
schedule change resets the first occurrence; a restart alone resumes the saved
schedule. Missed intervals coalesce to one current health sample rather than a
burst of old messages. Correct system time/timezone is required.

Heartbeat messages report the sample time, external-power/battery state, battery
percentage/voltage, and charging/shutdown enablement. They confirm that a healthy
monitoring sample was taken at that time; a delayed delivery is not proof the
service is still running now. Network delivery stays on the background Telegram
worker. Power events are selected ahead of queued heartbeats, and a routine
heartbeat cannot evict a power alert from a full queue. Only the newest queued
heartbeat is kept during network outages. An already in-flight request cannot be
recalled when disabling; normal best-effort/duplicate-delivery caveats apply.
The dashboard never shows bot tokens, chat IDs or message contents.

Press **s → 4** for **Send Telegram test message**. After administrator permission
if required, confirm `yes` to send exactly one immediate test message, then press
Enter to return. It does not alter scheduling, restart services, or change any
hardware/UPS settings. It works without enabling periodic heartbeats. Cancelling
the test sends nothing. Test-suite deliveries are mocked, not real messages.

Updating from earlier versions requires the service code as well as the UI:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo systemctl stop x120x_upsd.service
sudo sh ./install.sh
sudo systemctl reset-failed x120x_upsd.service
sudo systemctl start x120x_upsd.service
x120x-dashboard
```

The installer preserves the installed configuration and credentials. Update while
external power is connected. Your existing control settings are used on restart;
new heartbeat settings remain OFF until explicitly confirmed. No reboot is needed.

For terminal users, the same confirmed actions are available as:

```sh
sudo x120x-control set-telegram-heartbeat --hours 6 --start 09:00
sudo x120x-control set-telegram-heartbeat --disable
sudo x120x-control test-telegram
```

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

## Active Cooler settings: OS control or a custom curve

From **v1.2.0**, press **s**, then **2** for fan temperature and speed settings.
The menu supports the **built-in Raspberry Pi 5 fan connector / official Active
Cooler**. It does not configure a separate GPIO fan or bypass OS thermal control.

From **v1.3.0**, the list offers presets first, then a custom editor:

1. **OS settings**: remove this project's override and use the existing Raspberry
   Pi OS configuration. Earlier OS/custom fan settings outside the managed block
   remain; this is not a factory reset. Confirm `yes`, then
   `RESTORE OS FAN CONTROL`.
2. **Balanced** (recommended starting point): close to the starter curve described
   below, with full output at the highest stage.
3. **Cooler**: start airflow earlier and use higher duty at the lower stages.
4. **Quiet**: start the lower stages later and use gentler duty at moderate loads.
5. **Custom**: edit the four temperatures, four PWM percentages and shared
   hysteresis individually. Press Enter to retain a proposed field value or Esc
   to cancel.

| Preset | Stage temperatures (C) | Stage PWM duty (%) | Hysteresis |
|---|---|---|---|
| Balanced | 50 / 60 / 67.5 / 75 | 30 / 50 / 70 / 100 | 5C |
| Cooler | 45 / 55 / 65 / 75 | 35 / 55 / 75 / 100 | 5C |
| Quiet | 55 / 62 / 68 / 75 | 30 / 40 / 65 / 100 | 5C |

These are project presets, not claims about firmware factory values. The names
express intended behaviour; actual noise, RPM and temperatures depend on the
hardware and load. No preset is installed automatically. Enter at the list selects
Balanced for review. Choosing a preset skips field-by-field editing but still
shows the complete curve and requires **both** `yes` and `APPLY FAN CURVE` before
saving. Custom uses the same confirmations. The dashboard identifies a saved
curve as Balanced, Cooler, Quiet or Custom by its actual parameter values.

Here, **override means a custom automatic curve**, not a fixed manual speed.
The OS still changes fan output with CPU temperature in both modes. Percentage
means PWM duty, not a guaranteed RPM percentage. The tachometer displays actual
RPM separately. With 5C hysteresis, a stage entered at 50C can step down once the
temperature falls to roughly 45C. The wizard uses saved project values when
available; otherwise it clearly labels its starter values as proposals, not
readings of the current OS defaults.

Project validation permits temperatures from 30C to 75C, speeds from 20% to 100%,
and hysteresis from 1C to 10C. Thresholds must increase, speeds must not decrease,
and the final stage must be 100% by 75C or below. These are conservative project
constraints, not claims that firmware accepts no other values. CPU thermal
protection and OS fan management remain enabled. The helper checks that it is
running on a Pi 5 before saving a custom curve and refuses known separate GPIO
fan overlays.

After BOTH confirmations, the helper backs up `/boot/firmware/config.txt` and
atomically updates only its labelled managed fan block. Earlier boot lines and
other settings are retained. Saved project values take precedence over earlier
fan parameters for Pi 5. It uses `[all]` / `[pi5]` filters and ends overlay scope
before the base fan curve parameters. It does not drive fan GPIO pins directly,
write fan sysfs controls, restart the UPS service, change UPS/Telegram settings,
or automatically reboot. Cancelling either confirmation writes nothing.

**A reboot is required.** Save work and run `sudo reboot` when ready. The dashboard
compares saved curve values with kernel-exposed active trip points and PWM levels
when available. It reports a mismatch/pending reboot or unavailable active data,
rather than claiming saved settings are active. After removing an override, the
helper's reboot notice still applies even though no project curve remains saved.
A separate sensor attached to the cells would be needed for battery temperature;
CPU temperature is not battery temperature.

Update an existing installation without stopping UPS monitoring:

```sh
cd ~/x120x_term-control-dash
git pull --ff-only
sudo install -m 644 x120x_fan.py /usr/local/bin/x120x_fan.py
sudo install -m 755 x120x_control.py /usr/local/bin/x120x-control
sudo install -m 755 x120x_dashboard.py /usr/local/bin/x120x-dashboard
x120x-dashboard
```

Installing these files does not apply a fan curve. Demo and `--view-only` modes
cannot apply fan changes. Advanced users can use the same confirmed helper:

```sh
sudo x120x-control set-fan-curve --temperatures 50,60,67.5,75 --speeds 30,50,70,100 --hysteresis 5
sudo x120x-control restore-os-fan
```

References:
- [Official Active Cooler](https://www.raspberrypi.com/products/active-cooler/)
- [Official fan parameters](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [Boot configuration and reboot requirement](https://www.raspberrypi.com/documentation/computers/config_txt.html)

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
  mode it is only read. The UPS controller never drives the fan; the separate
  double-confirmed fan menu can select an OS-managed boot curve.
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
