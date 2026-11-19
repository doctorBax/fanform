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

## Calibration procedure

1. At low workload, click **Автотест** on one writable channel. Other settings are locked during calibration.
2. Fanform sets that channel to maximum, measures tachometers, drops to 210/255 briefly, and checks which tachometers slow down and recover.
3. If at least one tachometer reliably follows that channel, it lowers the setting in steps, while checking both temperatures and RPM. It does **not** deliberately stop the fan.
4. It records the lowest observed stable setting + 20/255 margin (at least 85/255). It restores the original control settings after calibration.
5. Until successful tachometer calibration, manual controls are limited to at least 180/255 (~71%) and mode-switching should only be attempted with a working tachometer.

Safeguards: no calibration with max reported temperature ≥65°C; test abort at ≥75°C or on insufficient RPM; missing temperature readings prohibit new control. When a previously mapped fan reports <300 RPM, emergency recovery sets the channel to 100%; if the firmware had an automatic mode that was captured, it remains an option for recovery. Linux or a full host hang cannot be rescued by a userspace daemon. Thermal readings and tachometer mapping are not equivalent to guaranteed cooling capacity.

**DC mode** is available only if the controller provides a writable `pwmN_mode` (Linux documents 0=DC, 1=PWM). This toggles the motherboard's hardware output mode, not an arbitrary voltage. The software cannot guarantee that the header and 3-pin fan support safe voltage regulation. Switch modes only with hardware knowledge, and recalibrate afterward. The detection is correlational, not perfect physical tracing.

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
