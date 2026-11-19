"""Safe control orchestration and calibration for Fanform."""
from __future__ import annotations
import json
import os
import secrets
from statistics import median
import subprocess
import threading
import time
from pathlib import Path
from . import hwmon

CONFIG = Path(os.environ.get('FANFORM_CONFIG', '/etc/fanform/config.json'))
STATE = Path(os.environ.get('FANFORM_STATE', '/var/lib/fanform/state.json'))
HIGH_TEMP = 75.0
# Use one critical boundary for both preflight and runtime checks. The old
# 65 C preflight threshold blocked otherwise healthy, warm Proxmox hosts even
# though the emergency guard would not trip until 75 C.
MAX_START_TEMP = HIGH_TEMP


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


class TemperatureSafetyError(SafetyError):
    """A critical cooling precondition failed; requires emergency recovery."""
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
        # These setpoints are live-session controls, not automatically replayed after reboot.
        self.targets = {}
        self.drift_counts = {}
        self.conflict = any(subprocess.run(['systemctl','is-active','--quiet',unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 for unit in ('fancontrol.service', 'coolercontrold.service'))
        self.job = {'state': 'idle', 'message': 'Автотест ещё не запускался', 'progress': 0, 'results': []}
        self.calibration_abort = threading.Event()
        self.calibration_thread = None
        self.alarm = None
        self.active = True
        # On service restart, assume previous manual control may have been orphaned.
        if self.originals:
            self.failsafe('Восстановление после незавершённой сессии')
        threading.Thread(target=self._watchdog, daemon=True).start()

    def inventory(self):
        channels, fans, temps = hwmon.discover()
        by_id = {fan['id']: fan for fan in fans}
        for c in channels:
            cal = self.cfg['calibrations'].get(c['id'])
            c['linked_fans'] = [dict(id=fid, name=by_id[fid]['name'], rpm=by_id[fid]['rpm'])
                                for fid in (cal or {}).get('fans', []) if fid in by_id]
            c['calibration'] = self.cfg['calibrations'].get(c['id'])
            c['active'] = c['id'] in self.originals
            c['recoverable'] = c['id'] in self.recovery
            c['target_pwm'] = self.targets.get(c['id'])
            c['control_drift'] = self.drift_counts.get(c['id'], 0)
        return {'channels': channels, 'fans': fans, 'temperatures': temps,
                'cooling_check': self.cooling_check(temps),
                'groups': self.cfg['groups'], 'alarm': self.alarm, 'job': {**self.job, 'results': [dict(result) for result in self.job.get('results', [])]}, 'conflict': self.conflict}

    def channel(self, cid):
        for c in hwmon.discover()[0]:
            if c['id'] == cid: return c
        raise SafetyError('Канал не найден. Обновите список устройств.')

    @staticmethod
    def cooling_check(temperatures):
        """Describe why a new manual operation/calibration may be refused.

        This is read-only. Frontend can display a precise inline explanation
        instead of hiding the hardware sensor that blocked the operation.
        """
        if not temperatures:
            return {'can_start': False, 'reason': 'Не найдены температурные датчики hwmon. Автотест не запущен.',
                    'hottest': None, 'start_limit_c': MAX_START_TEMP, 'abort_limit_c': HIGH_TEMP}
        hottest = max(temperatures, key=lambda t: t['celsius'])
        blocked = hottest['celsius'] >= MAX_START_TEMP
        return {'can_start': not blocked,
                'reason': (f'Автотест не запущен: {hottest["name"]} ({hottest["chip"]}) '
                           f'показывает {hottest["celsius"]} °C при пороге начала {MAX_START_TEMP:g} °C. '
                           'Проверьте этот датчик в списке ниже и показания на самом Proxmox.' if blocked else None),
                'hottest': {'id': hottest['id'], 'name': hottest['name'],
                            'chip': hottest['chip'], 'celsius': hottest['celsius']},
                'start_limit_c': MAX_START_TEMP, 'abort_limit_c': HIGH_TEMP}

    def _temperature_check(self, start=False):
        temperatures = hwmon.discover()[2]
        if not temperatures:
            raise TemperatureSafetyError('Не найдены температурные датчики hwmon. Защитная проверка не пройдена.')
        hottest = max(temperatures, key=lambda t: t['celsius'])
        maximum = hottest['celsius']
        threshold = MAX_START_TEMP if start else HIGH_TEMP
        if maximum >= threshold:
            raise TemperatureSafetyError(f'Температура: {hottest["name"]} ({hottest["chip"]}) '
                              f'показывает {maximum} °C, порог {threshold:g} °C. '
                              'Проверьте показания этого датчика в Proxmox.')
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
        pwm = Path(ch['path'])
        hwmon.write(pwm, duty)
        # Some Super-I/O drivers/EC firmware silently reject or overwrite writes.
        # A successful sysfs write alone does not prove the control is active.
        if hwmon.number(enable) != 1 or hwmon.number(pwm) != duty:
            hwmon.write(enable, 1)
            hwmon.write(pwm, duty)
            if hwmon.number(enable) != 1 or hwmon.number(pwm) != duty:
                raise SafetyError('Контроллер отклонил ручной PWM: BIOS/EC или другой процесс перезаписывает уставку')

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
            try:
                self._write_pwm(ch, duty)
                self.targets[cid] = duty
                self.drift_counts[cid] = 0
            except (OSError, SafetyError) as e:
                if cid in self.originals:
                    self.failsafe(f'Не удалось закрепить PWM: {e}')
                raise SafetyError(str(e))
            return {'channel': cid, 'pwm': duty, 'verified': True}

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
                self.targets[cid] = 255
                self.drift_counts[cid] = 0
                return {'mode': mode, 'note': 'Требуется повторная калибровка'}
            except (OSError, SafetyError):
                restore_value = dict(original)
                if original.get('enable') in (0, 1):
                    restore_value['value'] = 255
                hwmon.restore(restore_value, emergency=False)  # revert the hardware output mode
                if not previously_managed:
                    self.originals.pop(cid, None)
                self.targets.pop(cid, None)
                self.drift_counts.pop(cid, None)
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
                self.targets.pop(cid, None)
                self.drift_counts.pop(cid, None)
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

    def _calibration_preflight(self):
        if self.job['state'] == 'running':
            raise SafetyError('Автотест уже выполняется')
        if self.conflict:
            raise SafetyError('Обнаружен другой сервис управления вентиляторами')
        self._temperature_check(start=True)
        if not any((f['rpm'] or 0) >= 500 for f in hwmon.discover()[1]):
            raise SafetyError('Нет работающих тахометров для автоопределения')

    def start_auto_test(self):
        """One-click calibration of eligible channels, one at a time.

        Never manipulate a channel with active manual or emergency state.
        Read-only channels are displayed but not tested.
        """
        with self.lock:
            self._calibration_preflight()
            candidates = [c for c in hwmon.discover()[0] if c['writable']
                          and c['id'] not in self.originals and c['id'] not in self.recovery]
            if not candidates:
                raise SafetyError('Нет свободных управляемых каналов. Сначала верните ручные каналы в BIOS.')
            self.calibration_abort.clear()
            self.job = {'state': 'running', 'message': 'Подготовка автопривязки тахометров',
                        'progress': 0, 'current': 0, 'total': len(candidates),
                        'results': []}
            self.calibration_thread = threading.Thread(
                target=self._calibrate_all, args=(candidates,), daemon=True)
            self.calibration_thread.start()
            return {**self.job, 'results': []}

    def start_calibration(self, cid):
        """Legacy per-channel endpoint retained for existing API clients."""
        with self.lock:
            self._calibration_preflight()
            ch = self.channel(cid)
            if cid in self.originals or cid in self.recovery:
                raise SafetyError('Верните исходное управление перед калибровкой')
            if not ch['writable']:
                raise SafetyError('Канал недоступен для управления')
            self.calibration_abort.clear()
            self.job = {'state': 'running', 'message': 'Подготовка датчиков',
                        'progress': 0, 'current': 1, 'total': 1, 'results': [], 'channel': cid}
            self.calibration_thread = threading.Thread(target=self._calibrate, args=(cid,), daemon=True)
            self.calibration_thread.start()
            return dict(self.job)

    def _ensure_running(self):
        if self.calibration_abort.is_set() or not self.active:
            raise SafetyError('Автотест прерван: восстановлено управление охлаждением')

    def _measure(self, seconds=3, baseline=None, monitored=None, settle=0):
        """Median RPM sample after settle time. Check safety on every poll."""
        values = {}
        samples = max(1, int(seconds / .5))
        settling = max(0, int(settle / .5))
        for index in range(samples + settling):
            self._ensure_running()
            self._temperature_check()
            current = {f['id']: f['rpm'] for f in hwmon.discover()[1]}
            for fid in monitored or []:
                rpm = current.get(fid)
                if rpm is None or rpm < max(350, baseline[fid] * .25):
                    raise StallDetected(f'Потеря оборотов: {fid}')
            if index >= settling:
                for fid, rpm in current.items():
                    if rpm is not None:
                        values.setdefault(fid, []).append(rpm)
            time.sleep(.5)
        return {fid: round(median(v)) for fid, v in values.items()}

    def _calibrate_channel(self, cid, progress):
        """Map RPM changes to exactly one PWM channel, then measure safe floor.

        Lock is deliberately not held across sleeps, so /api/state can poll progress.
        Manual mutations are separately rejected while job.state == running.
        """
        original = None
        emergency = False
        result = {'channel': cid, 'name': cid, 'status': 'error', 'fans': []}
        try:
            ch = self.channel(cid)
            result['name'] = ch['name']
            self._ensure_running()
            self._temperature_check(start=True)
            original = hwmon.snapshot(ch)
            self._write_pwm(ch, 255)
            progress(8, 'Измеряю RPM на полной мощности')
            baseline = self._measure(3, settle=2)
            # Assisted pairing: measure several response points, not just 255->210.
            # Some fans have a wide 100% plateau or need time to settle.
            strong = {fid: rpm for fid, rpm in baseline.items() if rpm >= 300}
            if not strong:
                raise SafetyError('Не найден работающий тахометр (RPM ≥300); сопоставление невозможно')
            observations = [(255, baseline)]
            for duty, stage in ((215, 17), (175, 25), (150, 31)):
                self._ensure_running()
                self._write_pwm(ch, duty)
                progress(stage, f'Поиск отклика RPM · мощность {round(duty / 255 * 100)}%')
                sample = self._measure(3, baseline=strong, monitored=list(strong), settle=2)
                observations.append((duty, sample))
            self._ensure_running()
            self._write_pwm(ch, 255)
            progress(33, 'Проверяю восстановление оборотов')
            recovered = self._measure(3, baseline=strong, monitored=list(strong), settle=2)
            linked = []
            response = {}
            for fid, high in strong.items():
                series = [point.get(fid) for _, point in observations]
                up = recovered.get(fid)
                if any(value is None for value in series) or up is None:
                    continue
                delta = high - series[-1]
                individual_drops = [series[k] - series[k + 1] for k in range(3)]
                significant_steps = sum(x >= max(20, high * .025) for x in individual_drops)
                monotonic = all(series[k + 1] <= series[k] + max(45, high * .06) for k in range(3))
                recovered_enough = up >= high * .88 and up >= series[-1] + max(45, high * .08)
                matched = (delta >= max(65, high * .10) and significant_steps >= 1
                           and monotonic and recovered_enough)
                response[fid] = {'rpm_at_100': high, 'rpm_at_84': series[1],
                                 'rpm_at_69': series[2], 'rpm_at_59': series[3],
                                 'rpm_recovered': up, 'matched': matched}
                if matched:
                    linked.append(fid)
            # A second reversible pulse rejects transient tachometer noise.
            if linked:
                self._ensure_running()
                progress(33, 'Подтверждаю RPM повторным воздействием')
                self._write_pwm(ch, 150)
                confirmation_low = self._measure(3, baseline=strong, monitored=linked, settle=2)
                self._write_pwm(ch, 255)
                confirmation_high = self._measure(3, baseline=strong, monitored=linked, settle=2)
                confirmed = []
                for fid in linked:
                    low_rpm = confirmation_low.get(fid)
                    high_rpm = confirmation_high.get(fid)
                    baseline_rpm = strong[fid]
                    verified = (low_rpm is not None and high_rpm is not None
                                and baseline_rpm - low_rpm >= max(65, baseline_rpm * .10)
                                and high_rpm - low_rpm >= max(50, baseline_rpm * .08)
                                and high_rpm >= baseline_rpm * .86)
                    response[fid].update(rpm_confirmed_low=low_rpm,
                                         rpm_confirmed_high=high_rpm, matched=verified)
                    if verified:
                        confirmed.append(fid)
                linked = confirmed
            result['response_curve'] = response
            if not linked:
                raise SafetyError('PWM менялся, но ни один RPM не показал устойчивого '
                                  'отклика на 84→69→59% с возвратом к 100%. '
                                  'Проверьте DC/PWM, BIOS/EC и тахометры. Откройте измерения ниже.')
            progress(35, f'Найдено связанных тахометров: {len(linked)}')
            lowest = 210
            sequence = [190, 170, 150, 130, 115, 100, 85, 70, 60]
            for index, duty in enumerate(sequence):
                self._ensure_running()
                progress(35 + round(56 * index / len(sequence)),
                         f'Проверяю нижний предел · {round(duty / 255 * 100)}%')
                self._write_pwm(ch, duty)
                try:
                    self._measure(3, baseline=strong, monitored=linked, settle=1)
                except StallDetected:
                    self._write_pwm(ch, 255)
                    break
                lowest = duty
            self._ensure_running()
            safe = min(255, max(lowest + 20, 85))
            calibration = {'fans': linked, 'safe_pwm': safe,
                           'baseline_rpm': {fid: strong[fid] for fid in linked},
                           'response_curve': {fid: response[fid] for fid in linked},
                           'tested_mode': hwmon.number(Path(ch['mode_path'])) if ch['mode_path'] else None,
                           'measured_at': int(time.time())}
            self.cfg['calibrations'][cid] = calibration
            save_json(CONFIG, self.cfg)
            result.update(status='mapped', fans=linked, safe_pwm=safe,
                          response_curve={fid: response[fid] for fid in linked},
                          message=f'{len(linked)} датчик(а) привязано')
            progress(100, 'Привязка сохранена')
        except (OSError, SafetyError, ValueError) as error:
            emergency = (isinstance(error, (OSError, StallDetected, TemperatureSafetyError))
                         or self.calibration_abort.is_set())
            result.update(status='error', message=str(error), emergency=emergency)
        finally:
            if original:
                errors = hwmon.restore(original, emergency=emergency)
                if errors:
                    emergency = True
                    hwmon.restore(original, emergency=True)
                    result.update(status='error', message='Не удалось восстановить аппаратный режим',
                                  emergency=True)
                self.originals.pop(cid, None)
                self.targets.pop(cid, None)
                self.drift_counts.pop(cid, None)
                save_json(STATE, self.originals)
                if emergency:
                    self.recovery[cid] = original
                    save_json(recovery_path(), self.recovery)
        return result

    def _calibrate(self, cid):
        """Compatibility wrapper for single-channel calibration and unit tests."""
        def progress(percent, message):
            self.job.update(message=message, progress=percent)
        result = self._calibrate_channel(cid, progress)
        self.job.update(state='complete' if result['status'] == 'mapped' else 'error',
                        progress=100 if result['status'] == 'mapped' else 0,
                        message=result['message'], results=[result])

    def _calibrate_all(self, candidates):
        results = []
        total = len(candidates)
        for index, ch in enumerate(candidates):
            if self.calibration_abort.is_set() or not self.active:
                break
            self.job.update(current=index + 1, channel=ch['id'],
                            message=f'{ch["name"]}: подготовка')
            def progress(percent, message):
                self.job.update(progress=min(99, round((index + percent/100) / total * 100)),
                                message=f'{ch["name"]} · {message}')
            result = self._calibrate_channel(ch['id'], progress)
            results.append(result)
            if result['status'] != 'mapped' and ch['id'] in self.cfg['calibrations']:
                # A failed retest must not leave a misleading old RPM-to-PWM link.
                self.cfg['calibrations'].pop(ch['id'], None)
                save_json(CONFIG, self.cfg)
            self.job['results'] = [dict(item) for item in results]
            if result.get('emergency'):
                break

        # Mapping isn't proven when the same RPM sensor follows multiple headers.
        # Reject such relationships instead of showing contradictory slider labels.
        attached = {}
        for item in results:
            if item['status'] == 'mapped':
                for fid in item['fans']:
                    attached.setdefault(fid, []).append(item)
        ambiguous = {item['channel'] for owners in attached.values() if len(owners) > 1
                     for item in owners}
        if ambiguous:
            for item in results:
                if item['channel'] in ambiguous:
                    item.update(status='error', fans=[],
                                message='Неоднозначная привязка: один RPM соответствует нескольким PWM')
                    self.cfg['calibrations'].pop(item['channel'], None)
            save_json(CONFIG, self.cfg)
        complete = sum(item['status'] == 'mapped' for item in results)
        failed = len(results) - complete
        interrupted = len(results) < total
        self.job.update(state='error' if interrupted else 'complete', progress=100,
                        current=len(results), channel=None, results=[dict(item) for item in results],
                        message=(f'Прервано: обработано {len(results)} из {total}' if interrupted
                                 else f'Готово: {complete} привязано, {failed} без соответствия'))

    def failsafe(self, reason):
        if self.job["state"] == "running":
            self.calibration_abort.set()
        with self.lock:
            self.alarm = reason
            self.recovery.update(self.originals)
            save_json(recovery_path(), self.recovery)
            entries = list(self.originals.values())
            for entry in entries:
                hwmon.restore(entry, emergency=True)
            self.originals = {}
            self.targets.clear()
            self.drift_counts.clear()
            save_json(STATE, self.originals)

    def _monitor_once(self):
        """Check manual setpoints and cooling, recover bounded transient interference.

        Never fight another controller indefinitely: after 3 successive overrides,
        fail safe to full fan output and expose the conflict to the web UI.
        """
        with self.lock:
            if not self.originals or self.job['state'] == 'running':
                return
            try:
                self._temperature_check()
                if any(subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                    for unit in ('fancontrol.service', 'coolercontrold.service')):
                    self.conflict = True
                    raise SafetyError('Запущен другой сервис управления вентиляторами')
                channels, fans, _ = hwmon.discover()
                channel_map = {c['id']: c for c in channels}
                fan_map = {f['id']: f['rpm'] for f in fans}
                for cid in self.originals:
                    cal = self.cfg['calibrations'].get(cid)
                    if cal and cal.get('fans'):
                        for fid in cal['fans']:
                            if fan_map.get(fid) is None or fan_map[fid] < 300:
                                raise SafetyError(f'Потерян RPM: {fid}')
                for cid, duty in list(self.targets.items()):
                    ch = channel_map.get(cid)
                    if not ch or not ch['writable']:
                        raise SafetyError(f'Канал управления исчез: {cid}')
                    current = hwmon.number(Path(ch['path']))
                    enable = hwmon.number(Path(ch['enable_path']))
                    if current != duty or enable != 1:
                        count = self.drift_counts.get(cid, 0) + 1
                        self.drift_counts[cid] = count
                        if count >= 3:
                            raise SafetyError(f'PWM {cid} перезаписывается BIOS/EC или другой программой')
                        self._write_pwm(ch, duty)
                    else:
                        self.drift_counts[cid] = 0
            except (SafetyError, OSError, ValueError) as exc:
                self.failsafe(str(exc))

    def _watchdog(self):
        while self.active:
            time.sleep(2)
            self._monitor_once()

    def shutdown(self):
        self.calibration_abort.set()
        self.active = False
        if self.calibration_thread and self.calibration_thread.is_alive():
            self.calibration_thread.join(timeout=5)
        with self.lock:
            for entry in self.originals.values():
                hwmon.restore(entry, emergency=False)
            self.originals = {}
            self.targets.clear()
            self.drift_counts.clear()
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
