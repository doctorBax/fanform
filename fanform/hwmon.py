"""Linux hwmon access. Never writes during discovery."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get('FANFORM_HWMON_ROOT', '/sys/class/hwmon'))
PWM = re.compile(r'^pwm(\d+)$')
FAN = re.compile(r'^fan(\d+)_input$')
TEMP = re.compile(r'^temp(\d+)_input$')


def read(path: Path, default=None):
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return default


def number(path: Path, default=None):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def write(path: Path, val: int):
    if not path.is_file():
        raise ValueError('Unsupported hwmon control')
    path.write_text(f'{int(val)}\n')


def device_key(path: Path):
    canonical = str(path.resolve())
    # Exclude ephemeral hwmon index if backed by a stable device tree path.
    canonical = re.sub(r'/hwmon/hwmon\d+$', '', canonical)
    return hashlib.sha256(canonical.encode()).hexdigest()[:10]


def discover():
    channels, fans, temps = [], [], []
    if not ROOT.is_dir():
        return channels, fans, temps
    for hw in sorted(ROOT.glob('hwmon*')):
        if not hw.is_dir():
            continue
        name = read(hw / 'name', 'unknown')
        key = device_key(hw)
        for attr in sorted(hw.iterdir()):
            pwm = PWM.fullmatch(attr.name)
            fan = FAN.fullmatch(attr.name)
            temp = TEMP.fullmatch(attr.name)
            if pwm and attr.is_file():
                num = pwm.group(1)
                en = hw / f'pwm{num}_enable'
                mode = hw / f'pwm{num}_mode'
                channels.append({'id': f'{key}-pwm{num}', 'name': f'{name} · PWM {num}', 'chip': name,
                                 'path': str(attr), 'enable_path': str(en) if en.exists() else None,
                                 'mode_path': str(mode) if mode.exists() else None,
                                 'value': number(attr), 'enable': number(en), 'mode': number(mode),
                                 'writable': os.access(attr, os.W_OK) and en.exists() and os.access(en, os.W_OK),
                                 'dc_available': mode.exists() and os.access(mode, os.W_OK)})
            elif fan:
                num = fan.group(1)
                fans.append({'id': f'{key}-fan{num}', 'name': read(hw / f'fan{num}_label', f'{name} · Fan {num}'),
                             'chip': name, 'path': str(attr), 'rpm': number(attr)})
            elif temp:
                num = temp.group(1)
                value = number(attr)
                if value is not None and 0 < value < 115000:
                    temps.append({'id': f'{key}-temp{num}', 'name': read(hw / f'temp{num}_label', f'{name} · Temp {num}'),
                                  'chip': name, 'path': str(attr), 'celsius': round(value / 1000, 1)})
    return channels, fans, temps


def snapshot(ch):
    return {'id': ch['id'], 'path': ch['path'], 'enable_path': ch['enable_path'], 'mode_path': ch['mode_path'],
            'value': number(Path(ch['path'])),
            'enable': number(Path(ch['enable_path'])) if ch['enable_path'] else None,
            'mode': number(Path(ch['mode_path'])) if ch['mode_path'] else None}


def restore(entry, emergency=False):
    """Restore snapshot, or leave hardware at full manual output on failure."""
    pwm = Path(entry['path'])
    en = Path(entry['enable_path']) if entry.get('enable_path') else None
    mode = Path(entry['mode_path']) if entry.get('mode_path') else None
    errors = []
    try:
        # Some drivers reject PWM writes in firmware-controlled mode.
        try: write(pwm, 255)
        except OSError: pass
        if en:
            write(en, 1)
        write(pwm, 255)
        if emergency:
            return errors  # full manual output, never an unsafe stored setpoint
        if mode and entry.get('mode') is not None:
            write(mode, entry['mode'])
        if en and entry.get('enable') is not None:
            write(en, entry['enable'])
        if entry.get('enable') == 1 and entry.get('value') is not None:
            write(pwm, entry['value'])
    except (OSError, ValueError) as e:
        errors.append(str(e))
        if not emergency:
            # Failure to restore firmware: prioritize full speed.
            try:
                if en: write(en, 1)
                write(pwm, 255)
            except (OSError, ValueError) as second:
                errors.append(str(second))
    return errors
