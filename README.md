# Fanform · cooling control for Proxmox

A self-hosted, dependency-free (Python stdlib) web panel for Linux hwmon fans. Responsive, authenticated dashboard; CLI controlled with ↑ ↓ Enter or digits; group control, per-channel calibration, 3-pin DC mode **only when supported by the Linux driver**.

**Status: MVP 0.1. Experimental. Hardware support is not guaranteed.** No PWM writes on install or discovery. Testing requires an explicit action.

## One-command installation on Proxmox / Debian

On the Proxmox host, paste this **single command** into a terminal:

```bash
curl -fsSLo /tmp/fanform-install.sh https://raw.githubusercontent.com/doctorBax/fanform/main/install.sh && sudo bash /tmp/fanform-install.sh
```

No extraction, manual dependency installation, or configuration editing is needed The installer:

- checks the host (Debian/Proxmox + systemd) and rejects conflicts with existing cooling daemons;
- installs missing `python3`, `git`, and CA certificates using `apt`;
- clones the repository and runs tests before changing the installed service;
- generates and displays a random admin password on the first installation;
- automatically selects an available port starting at `8796` (without changing an existing port);
- configures and starts systemd service, then displays the web URL and CLI command.

The installation changes **no fan output settings**. Calibration and voltage/PWM control require separate explicit interaction.

For an offline/local installation from this archive, extract it and run `sudo bash fanform/install.sh` (this path is for development and is *not* the advertised one-command GitHub install).

The web panel listens on all host interfaces and uses HTTP; use it **only on a trusted LAN**. Do not expose it directly to the Internet. Since the service needs hardware access, it runs as root. Review the installer before running it as root.

`sudo fanformctl` opens the interactive CLI. Also available:

```bash
sudo fanformctl --status
sudo fanformctl --port 8797
sudo fanformctl --auto-update on   # only when installed from a git origin
sudo fanformctl --auto-update off
sudo fanformctl --reset-password
sudo fanformctl --emergency
sudo fanformctl --uninstall
```

Automatic updates are disabled by default; a systemd timer can update only if the install comes from a real Git repository. Run with trusted supply-chain practices; a hosted repository can publish unreviewed changes. **Uninstall removes settings and calibration records too.**

## One-button automatic calibration and RPM mapping

The dashboard contains **one `Автотест всех вентиляторов` button**. It runs a single
batch job over every available, writable PWM/DC channel, one channel at a time.
Channels currently in manual control or in emergency recovery are deliberately
skipped; restore them to firmware first if they also need calibration. Read-only
sensors remain visible but are not operated.

Fanform uses an independent, reversible repeated-response pairing test (100%
→ 84% → 69% → 59% → 100% → 59% → 100%). Each step waits for tachometer
settling and captures median RPM. Unrelated tachometers are not treated as a
stall in the unknown-mapping stage. RPM must react and recover twice before
it is automatically associated with that PWM channel. Failed tests keep
per-sensor readings in the UI for hardware diagnosis. As with Fan Control,
automatic RPM detection is limited to information actually exposed by the
controller/driver; hardware may ignore Linux PWM values despite write success.

The button starts a background job and exposes live per-channel progress through
`GET /api/state`. The web UI shows the overall progress bar, currently tested
channel, and results as they arrive, without blocking polling. The old
`POST /api/calibrate` endpoint remains available for legacy clients; the new
`POST /api/calibrate/all` starts the batch.

For each channel the test measures RPM at full output, temporarily lowers output
for correlation, verifies that any matching RPM rises again, then cautiously tests
the minimum sustained output and saves a margin above it. The confirmed RPM
sensor(s) and their **live RPM readings appear on the corresponding slider card**.
Ambiguous RPM-to-PWM mappings are discarded, rather than presented as fact.
No manual mode is left active following a completed test.

The obsolete 65°C preflight cutoff has been removed: the start check now uses the same 75°C emergency threshold as runtime monitoring. There is no option to disable emergency recovery for critical temperature or stalled RPM.

The UI does not show routine temperature alert banners or popup messages. Actual
temperatures remain visible as sensor readings. **The backend emergency thermal
and stall checks remain enabled**: hardware safety overrides convenience, and
a test aborts automatically on hazardous conditions. Never run calibration under
heavy load or without adequate redundant cooling. Some boards do not allow
software fan control despite exposing tachometer sensors.

**DC mode** is offered only where the Linux controller exposes a writable
`pwmN_mode` (0 = DC, 1 = PWM), and it requires an explicit selection. A 3-pin fan
alone does not prove the motherboard can regulate that header's voltage.

## Limitations

- Some motherboards expose tachometers without any writable fan control. They are displayed as read-only.
- Fan/PWM mapping can be ambiguous with shared headers or correlated speed changes.
- The emergency mechanism depends on Linux userspace running and a functioning hwmon driver. For CPUs and critical cooling, **leave BIOS/UEFI fan curves enabled** unless you have verified the recovery behavior on the actual hardware.
- There is no automatic temperature-based software fan curve in this MVP. The panel controls manually or hands control back to firmware.
- Do not run alongside `fancontrol`, another fan daemon, or overlapping hardware-control services.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Unit tests use fake temporary hwmon paths and never touch real fan headers.

## Stack

Linux hwmon, Python 3 standard library, vanilla HTML/CSS/JS, systemd. No npm, Docker, telemetry, or cloud.

## Multi-step assisted mapping

The single autotest button now checks tachometer response across several PWM
levels (255, 215, 175, 150) and checks recovery at 255 before mapping a
sensor. A speed sensor must respond consistently across multiple measurements;
small fluctuations on unrelated sensors do not create a mapping. After
pairing, the existing minimum-speed calibration runs as before, and the
RPM response curve is stored in calibration data.

This is a Fan Control-style approach, not a copy of proprietary code. No RPM
response can also mean that BIOS/EC overrides the control, an input-only
sensor, or a fan that cannot be matched reliably. An unconfirmed pairing is
reported as such rather than guessed.

### Assisted RPM pairing diagnostics

Assisted pairing waits for tachometer stabilization, uses median RPM samples
across several PWM levels, and repeats a confirmation pulse before saving
RPM/PWM associations. Per-channel RPM response curves are visible under
**Измерения RPM** after autocalibration, including failed mappings. A 3-pin DC
fan that cannot change RPM in PWM mode is reported without guessing a mapping.
Electrical PWM/DC mode is never changed automatically.
