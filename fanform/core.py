"""Safe control orchestration and calibration for Fanform."""
from __future__ import annotations
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from . import hwmon

CONFIG = Path(os.environ.get('FANFORM_CONFIG', '/etc/fanform/config.json'))
STATE = Path(os.environ.get('FANFORM_STATE', '/var/lib/fanform/state.json'))
HIGH_TEMP = 75.0
MAX_START_TEMP = 65.0


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()


def load_json(path, fallback):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return fallback


class SafetyError(Exception):
    pass


class StallDetected(SafetyError):
    pass


def recovery_path():
    return STATE.with_name('recovery.json')


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = load_json(CONFIG, {})
        self.cfg.setdefault('port', 8796)
        self.cfg.setdefault('bind', '0.0.0.0')
        self.cfg.setdefault('calibrations', {})
        self.cfg.setdefault('groups', [])
        self.originals = load_json(STATE, {})
        self.recovery = load_json(recovery_path(), {})
        self.conflict = any(subprocess.run(['systemctl','is-active','--quiet',unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 for unit in ('fancontrol.service', 'coolercontrold.service'))
        self.job = {'state': 'idle', 'message': 'Калибровка ещё не запускалась', 'progress': 0}
        self.alarm = None
        self.active = True
        # On service restart, assume previous manual control may have been orphaned.
        if self.originals:
            self.failsafe('Восстановление после незавершённой сессии')
        threading.Thread(target=self._watchdog, daemon=True).start()

    def inventory(self):
        channels, fans, temps = hwmon.discover()
        for c in channels:
            c['calibration'] = self.cfg['calibrations'].get(c['id'])
            c['active'] = c['id'] in self.originals
            c['recoverable'] = c['id'] in self.recovery
        return {'channels': channels, 'fans': fans, 'temperatures': temps,
                'groups': self.cfg['groups'], 'alarm': self.alarm, 'job': dict(self.job), 'conflict': self.conflict}

    def channel(self, cid):
        for c in hwmon.discover()[0]:
            if c['id'] == cid: return c
        raise SafetyError('Канал не найден. Обновите список устройств.')

    def _temperature_check(self, start=False):
        temperatures = hwmon.discover()[2]
        if not temperatures:
            raise SafetyError('Нет температурных датчиков: управление заблокировано')
        maximum = max(t['celsius'] for t in temperatures)
        if maximum >= (MAX_START_TEMP if start else HIGH_TEMP):
            raise SafetyError(f'Температура {maximum}°C: защита остановила операцию')
        return maximum

    def _remember(self, ch):
        if self.conflict:
            raise SafetyError('Обнаружен другой активный сервис управления вентиляторами. Остановите конфликтующий сервис.')
        if ch['id'] in self.recovery:
            raise SafetyError('После аварийного сброса сначала восстановите исходное управление')
        if ch['id'] not in self.originals:
            self.originals[ch['id']] = hwmon.snapshot(ch)
            save_json(STATE, self.originals)

    def _write_pwm(self, ch, duty):
        if not ch['writable']:
            raise SafetyError('Контроллер не разрешает ручное управление')
        self._remember(ch)
        enable = Path(ch['enable_path'])
        if hwmon.number(enable) != 1:
            # Best effort preloading 100% before entering manual mode; some
            # drivers require enable=1 first and reject the preload.
            try: hwmon.write(Path(ch['path']), 255)
            except OSError: pass
            hwmon.write(enable, 1)
            hwmon.write(Path(ch['path']), 255)
        hwmon.write(Path(ch['path']), duty)

    def set_speed(self, cid, percent):
        with self.lock:
            if self.job['state'] == 'running': raise SafetyError('Идёт калибровка')
            ch = self.channel(cid)
            self._temperature_check(start=True)
            if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
                raise SafetyError('Скорость должна быть от 0 до 100%')
            cal = self.cfg['calibrations'].get(cid)
            # Full-speed only unless mapped, if no trustworthy RPM feedback.
            minimum = max(180, int(255 * .7)) if not cal or not cal.get('fans') else cal['safe_pwm']
            duty = round(percent * 255 / 100)
            if duty < minimum:
                raise SafetyError(f'Минимум для этого канала: {round(minimum / 255 * 100)}%')
            try: self._write_pwm(ch, duty)
            except OSError as e:
                self.failsafe(f'Ошибка записи: {e}')
                raise SafetyError(str(e))
            return {'channel': cid, 'pwm': duty}

    def set_mode(self, cid, mode):
        with self.lock:
            if self.job['state'] == 'running': raise SafetyError('Идёт калибровка')
            ch = self.channel(cid)
            if mode not in (0, 1): raise SafetyError('Режим: 0 (DC) или 1 (PWM)')
            if not ch['dc_available'] or not ch['writable']:
                raise SafetyError('Изменение режима на этом разъёме не поддерживается')
            self._temperature_check(start=True)
            current = hwmon.number(Path(ch['mode_path']))
            if current == mode: return {'mode': mode}
            fans = [f for f in hwmon.discover()[1] if f['rpm'] is not None and f['rpm'] > 350]
            if not fans:
                raise SafetyError('Без работающего тахометра переключение режима заблокировано')
            original = hwmon.snapshot(ch)
            previously_managed = cid in self.originals
            try:
                self._write_pwm(ch, 255)
                baseline = {f['id']: f['rpm'] for f in fans}
                hwmon.write(Path(ch['mode_path']), mode)
                time.sleep(2)
                self._temperature_check()
                _, check, _ = hwmon.discover()
                values = {f['id']: f['rpm'] for f in check}
                # No evidence of mapping: cannot automatically conclude this header works.
                for fid, rpm in baseline.items():
                    if values.get(fid) is not None and rpm >= 500 and values[fid] < max(300, rpm * .5):
                        raise SafetyError('RPM резко упали после смены режима')
                self.cfg['calibrations'].pop(cid, None)
                save_json(CONFIG, self.cfg)
                return {'mode': mode, 'note': 'Требуется повторная калибровка'}
            except (OSError, SafetyError):
                restore_value = dict(original)
                if original.get('enable') in (0, 1):
                    restore_value['value'] = 255
                hwmon.restore(restore_value, emergency=False)  # revert the hardware output mode
                if not previously_managed:
                    self.originals.pop(cid, None)
                save_json(STATE, self.originals)
                raise

    def firmware(self, cid):
        with self.lock:
            if self.job['state'] == 'running': raise SafetyError('Идёт калибровка')
            original = self.originals.get(cid) or self.recovery.get(cid)
            if original:
                self._temperature_check(start=True)
                errors = hwmon.restore(original, emergency=False)
                if errors: raise SafetyError('; '.join(errors))
                self.originals.pop(cid, None)
                self.recovery.pop(cid, None)
                save_json(STATE, self.originals)
                save_json(recovery_path(), self.recovery)
                return {'restored': True}
            raise SafetyError('Для этого канала нет сохранённого исходного режима')

    def group_save(self, name, channels):
        with self.lock:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise SafetyError('Укажите название группы до 40 символов')
            known = {c['id'] for c in hwmon.discover()[0] if c['writable']}
            if not isinstance(channels, list) or not channels or any(c not in known for c in channels):
                raise SafetyError('Выберите существующие PWM-каналы')
            item = {'id': secrets.token_hex(4), 'name': name.strip(), 'channels': sorted(set(channels))}
            self.cfg['groups'].append(item)
            save_json(CONFIG, self.cfg)
            return item

    def group_delete(self, gid):
        with self.lock:
            before = len(self.cfg['groups'])
            self.cfg['groups'] = [g for g in self.cfg['groups'] if g['id'] != gid]
            if len(self.cfg['groups']) == before: raise SafetyError('Группа не найдена')
            save_json(CONFIG, self.cfg)
            return {'deleted': True}

    def group_speed(self, gid, percent):
        with self.lock:
            groups = [g for g in self.cfg['groups'] if g['id'] == gid]
            if not groups: raise SafetyError('Группа не найдена')
            # Preflight entire group before first write.
            self._temperature_check(start=True)
            if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
                raise SafetyError('Скорость должна быть от 0 до 100%')
            for cid in groups[0]['channels']:
                ch = self.channel(cid)
                cal = self.cfg['calibrations'].get(cid)
                minimum = cal['safe_pwm'] if cal and cal.get('fans') else 180
                if not ch['writable'] or not isinstance(percent, (int, float)) or percent * 255 / 100 < minimum:
                    raise SafetyError('Один из каналов не поддерживает выбранную скорость')
            try:
                for cid in groups[0]['channels']: self.set_speed(cid, percent)
            except (SafetyError, OSError):
                self.failsafe('Ошибка применения групповой скорости')
                raise
            return {'updated': len(groups[0]['channels'])}

    def start_calibration(self, cid):
        with self.lock:
            if self.job['state'] == 'running': raise SafetyError('Калибровка уже выполняется')
            ch = self.channel(cid)
            if cid in self.originals or cid in self.recovery: raise SafetyError('Верните исходное управление перед калибровкой')
            if not ch['writable']: raise SafetyError('Канал недоступен для управления')
            self._temperature_check(start=True)
            if not any((f['rpm'] or 0) >= 500 for f in hwmon.discover()[1]):
                raise SafetyError('Нет работающих тахометров для автоопределения')
            self.job = {'state': 'running', 'message': 'Подготовка датчиков', 'progress': 0, 'channel': cid}
            threading.Thread(target=self._calibrate, args=(cid,), daemon=True).start()
            return dict(self.job)

    def _measure(self, seconds=3, baseline=None, monitored=None):
        values = {}
        samples = max(1, int(seconds / .5))
        for _ in range(samples):
            self._temperature_check()
            current = {f['id']:f['rpm'] for f in hwmon.discover()[1]}
            for fid in monitored or []:
                rpm = current.get(fid)
                if rpm is None or rpm < max(350, baseline[fid] * .25):
                    raise StallDetected(f'Потеря оборотов: {fid}')
            for fid, rpm in current.items():
                if rpm is not None:
                    values.setdefault(fid, []).append(rpm)
            time.sleep(.5)
        return {fid: round(sum(v) / len(v)) for fid, v in values.items()}

    def _calibrate(self, cid):
        # Whole calibration locks out other writers. Never sends duty 0.
        with self.lock:
            ch = None
            original = None
            emergency = False
            try:
                ch = self.channel(cid)
                self._temperature_check(start=True)
                original = hwmon.snapshot(ch)
                self._write_pwm(ch, 255)
                self.job.update(message='Измерение на полной скорости', progress=8)
                baseline = self._measure(3)
                strong = {f: rpm for f, rpm in baseline.items() if rpm >= 500}
                if not strong: raise SafetyError('Нет тахометров с RPM ≥500')
                self._write_pwm(ch, 210)
                self.job.update(message='Определяю связанные тахометры', progress=22)
                low = self._measure(3, baseline=strong, monitored=list(strong))
                linked = [f for f, rpm in strong.items() if f in low and rpm - low[f] >= max(120, rpm * .12)]
                # Confirm recovery to distinguish workload fluctuations / unrelated fans.
                self._write_pwm(ch, 255)
                recovered = self._measure(3)
                linked = [f for f in linked if f in recovered and recovered[f] >= baseline[f] * .85]
                if not linked: raise SafetyError('Не удалось надёжно сопоставить PWM и RPM (контроль оставлен без изменений)')
                lowest = 210
                sequence = [190, 170, 150, 130, 115, 100, 85, 70, 60]
                for i, duty in enumerate(sequence):
                    self.job.update(message=f'Поиск устойчивого минимума · {round(duty / 255 * 100)}%',
                                    progress=35 + round(56 * i / len(sequence)))
                    self._write_pwm(ch, duty)
                    try:
                        self._measure(3, baseline=strong, monitored=linked)
                    except StallDetected:
                        self._write_pwm(ch, 255)  # immediate recovery; keep last good step
                        break
                    lowest = duty
                # Reserve >= 20/255 above the lowest observed stable point.
                safe = min(255, max(lowest + 20, 85))
                self.cfg['calibrations'][cid] = {'fans': linked, 'safe_pwm': safe,
                    'baseline_rpm': {f: strong[f] for f in linked}, 'tested_mode': hwmon.number(Path(ch['mode_path'])) if ch['mode_path'] else None,
                    'measured_at': int(time.time())}
                save_json(CONFIG, self.cfg)
                self.job.update(state='complete', progress=100, message=f'Найден минимум {round(safe / 255 * 100)}%, тахометров: {len(linked)}')
            except (OSError, SafetyError, ValueError) as e:
                emergency = isinstance(e, (OSError, StallDetected)) or any(word in str(e) for word in ('Температура', 'температурных'))
                self.job.update(state='error', message=str(e), progress=0)
            finally:
                if original:
                    hwmon.restore(original, emergency=emergency)
                    self.originals.pop(cid, None)
                    save_json(STATE, self.originals)
                    if emergency:
                        self.recovery[cid] = original
                        save_json(recovery_path(), self.recovery)

    def failsafe(self, reason):
        with self.lock:
            self.alarm = reason
            self.recovery.update(self.originals)
            save_json(recovery_path(), self.recovery)
            entries = list(self.originals.values())
            for entry in entries:
                hwmon.restore(entry, emergency=True)
            self.originals = {}
            save_json(STATE, self.originals)

    def _watchdog(self):
        while self.active:
            time.sleep(2)
            with self.lock:
                if not self.originals or self.job['state'] == 'running':
                    continue
                try:
                    self._temperature_check()
                    fan_map = {f['id']: f['rpm'] for f in hwmon.discover()[1]}
                    for cid in self.originals:
                        cal = self.cfg['calibrations'].get(cid)
                        if cal and cal['fans']:
                            for fid in cal['fans']:
                                if fan_map.get(fid) is None or fan_map[fid] < 300:
                                    raise SafetyError(f'Потерян RPM: {fid}')
                except (SafetyError, OSError) as e:
                    self.failsafe(str(e))

    def shutdown(self):
        self.active = False
        with self.lock:
            for entry in self.originals.values():
                hwmon.restore(entry, emergency=False)
            self.originals = {}
            save_json(STATE, self.originals)


def emergency_restore():
    originals = load_json(STATE, {})
    recovery = load_json(recovery_path(), {})
    recovery.update(originals)
    save_json(recovery_path(), recovery)
    for entry in originals.values():
        hwmon.restore(entry, emergency=True)
    save_json(STATE, {})


def uninstall_restore():
    originals = load_json(STATE, {})
    originals.update(load_json(recovery_path(), {}))
    for entry in originals.values():
        hwmon.restore(entry, emergency=False)
    save_json(STATE, {})
    save_json(recovery_path(), {})
