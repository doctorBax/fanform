import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fanform import hwmon, core

class FakeHwmon(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hw = self.root/'hwmon'/'hwmon0'; self.hw.mkdir(parents=True)
        for name, value in {'name':'nct6775','pwm1':'255','pwm1_enable':'2','pwm1_mode':'1',
                            'fan1_input':'1500','fan2_input':'1200','temp1_input':'40000'}.items():
            (self.hw/name).write_text(value)
        self.patch_root = mock.patch.object(hwmon,'ROOT',self.hw.parent)
        self.patch_config = mock.patch.object(core,'CONFIG',self.root/'etc'/'config.json')
        self.patch_state = mock.patch.object(core,'STATE',self.root/'var'/'state.json')
        self.patch_watchdog = mock.patch.object(core.Controller,'_watchdog',lambda _self: None)
        for p in (self.patch_root,self.patch_config,self.patch_state,self.patch_watchdog):
            p.start();self.addCleanup(p.stop)
        self.control=core.Controller()

    def test_discovery_does_not_mutate(self):
        ch, fans, temps=hwmon.discover()
        self.assertEqual(len(ch),1)
        self.assertEqual(fans[0]['rpm'],1500)
        self.assertEqual(temps[0]['celsius'],40.0)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),255)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)
        self.assertTrue(ch[0]['dc_available'])

    def test_unmapped_channel_rejects_low_speed(self):
        cid=hwmon.discover()[0][0]['id']
        with self.assertRaises(core.SafetyError):self.control.set_speed(cid,15)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

    def test_temperature_guard_prevents_write(self):
        cid=hwmon.discover()[0][0]['id']
        (self.hw/'temp1_input').write_text('80000')
        with self.assertRaises(core.SafetyError):self.control.set_speed(cid,90)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

    def test_no_temperature_guard(self):
        cid=hwmon.discover()[0][0]['id']
        (self.hw/'temp1_input').unlink()
        with self.assertRaises(core.SafetyError):self.control.start_calibration(cid)

    def test_manual_and_firmware_restore(self):
        cid=hwmon.discover()[0][0]['id']
        self.control.set_speed(cid,90)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),1)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),230)
        self.control.firmware(cid)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

    def test_emergency_sets_full(self):
        cid=hwmon.discover()[0][0]['id']
        (self.hw/'pwm1_enable').write_text('1')
        (self.hw/'pwm1').write_text('220')
        self.control.set_speed(cid,80)
        self.control.failsafe('simulate')
        self.assertEqual(hwmon.number(self.hw/'pwm1'),255)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),1)

    def test_group_creation(self):
        cid=hwmon.discover()[0][0]['id']
        group=self.control.group_save('HDD', [cid,cid])
        self.assertEqual(group['channels'],[cid])
        self.control.group_speed(group['id'],90)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),230)
        self.control.group_delete(group['id'])
        self.assertFalse(self.control.cfg['groups'])

    def test_calibration_maps_rpm_and_floor(self):
        cid=hwmon.discover()[0][0]['id']
        original_write=hwmon.write
        def simulated_write(path, value):
            original_write(path,value)
            if path.name=='pwm1':
                rpm=round(1500*value/255) if value >= 115 else 200
                (self.hw/'fan1_input').write_text(str(rpm))
        with mock.patch.object(hwmon,'write',side_effect=simulated_write), mock.patch.object(core.time,'sleep',lambda *_:None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'],'complete')
        cal=self.control.cfg['calibrations'][cid]
        self.assertEqual(len(cal['fans']),1)
        self.assertIn('fan1',cal['fans'][0])
        self.assertEqual(cal['safe_pwm'],135)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

    def test_calibration_refuses_no_correlated_rpm(self):
        cid=hwmon.discover()[0][0]['id']
        with mock.patch.object(core.time,'sleep',lambda *_:None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'],'error')
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

if __name__=='__main__':unittest.main()

class AdditionalSafetyTests(unittest.TestCase):
    setUp = FakeHwmon.setUp
    def test_emergency_recovery_to_firmware(self):
        cid=hwmon.discover()[0][0]['id']
        self.control.set_speed(cid,85)
        self.control.failsafe('test overheat')
        self.assertIn(cid,self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),255)
        result = self.control.set_speed(cid,90)
        self.assertTrue(result['verified'])
        self.assertNotIn(cid,self.control.recovery)
        self.control.firmware(cid)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)
        self.assertNotIn(cid,self.control.recovery)

    def test_dc_switch_and_revert(self):
        cid=hwmon.discover()[0][0]['id']
        with mock.patch.object(core.time,'sleep',lambda _:None):
            response=self.control.set_mode(cid,0)
        self.assertEqual(response['mode'],0)
        self.assertEqual(hwmon.number(self.hw/'pwm1_mode'),0)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),1)
        self.control.firmware(cid)
        self.assertEqual(hwmon.number(self.hw/'pwm1_mode'),1)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)

    def test_group_rejects_out_of_range(self):
        cid=hwmon.discover()[0][0]['id']
        g=self.control.group_save('Chassis',[cid])
        for bad in (-1, 0, 101, True):
            with self.assertRaises(core.SafetyError):
                self.control.group_speed(g['id'],bad)


    def test_sysfs_requires_manual_mode_before_writing_pwm(self):
        cid = hwmon.discover()[0][0]['id']
        saved = hwmon.write
        def guarded(path, duty):
            if path.name == 'pwm1' and hwmon.number(self.hw/'pwm1_enable') != 1:
                raise OSError('PWM locked while firmware is active')
            return saved(path, duty)
        with mock.patch.object(hwmon, 'write', side_effect=guarded):
            self.control.set_speed(cid,90)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),1)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),230)

    def test_high_temperature_during_calibration_fails_safe(self):
        cid=hwmon.discover()[0][0]['id']
        saved=hwmon.write
        def heat(path, duty):
            saved(path,duty)
            if path.name=='pwm1' and duty == 215:
                (self.hw/'temp1_input').write_text('80000')
        with mock.patch.object(hwmon,'write',side_effect=heat),mock.patch.object(core.time,'sleep',lambda *_:None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'],'error')
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),1)
        self.assertEqual(hwmon.number(self.hw/'pwm1'),255)
        self.assertIn(cid,self.control.recovery)


class ManualSetpointTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_setpoint_is_exposed_and_readback_verified(self):
        cid = hwmon.discover()[0][0]['id']
        result = self.control.set_speed(cid, 90)
        ch = self.control.inventory()['channels'][0]
        self.assertTrue(result['verified'])
        self.assertEqual(ch['target_pwm'], 230)
        self.assertEqual(ch['value'], 230)
        self.assertEqual(ch['control_drift'], 0)

    def test_external_pwm_change_is_reapplied(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 90)
        (self.hw/'pwm1').write_text('255')
        (self.hw/'pwm1_enable').write_text('2')
        self.control._monitor_once()
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 230)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 1)
        self.assertEqual(self.control.inventory()['channels'][0]['control_drift'], 1)
        self.control._monitor_once()
        self.assertEqual(self.control.inventory()['channels'][0]['control_drift'], 0)

    def test_repeated_external_override_fails_safe_not_endless_fight(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 90)
        for _ in range(3):
            (self.hw/'pwm1').write_text('255')
            self.control._monitor_once()
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 1)
        self.assertIn(cid, self.control.recovery)
        self.assertIn('перезаписывается', self.control.alarm)
        self.assertEqual(self.control.targets, {})

    def test_firmware_clears_manual_target(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 90)
        self.control.firmware(cid)
        self.assertIsNone(self.control.inventory()['channels'][0]['target_pwm'])

    def test_temperature_abort_still_active(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 90)
        (self.hw/'temp1_input').write_text('79000')
        self.control._monitor_once()
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)
        self.assertIn('температура', self.control.alarm.lower())


class CoolingDiagnosticsTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_inventory_reports_exact_blocking_sensor(self):
        (self.hw/'temp1_input').write_text('78000')
        status = self.control.inventory()['cooling_check']
        self.assertFalse(status['can_start'])
        self.assertIn('nct6775', status['reason'])
        self.assertIn('78.0 °C', status['reason'])
        self.assertIn('75 °C', status['reason'])
        self.assertEqual(status['hottest']['celsius'], 78.0)
        with self.assertRaisesRegex(core.SafetyError, 'nct6775.*78.0.*75'):
            self.control.start_auto_test()
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)

    def test_warm_host_below_critical_limit_can_change_setpoint(self):
        cid = hwmon.discover()[0][0]['id']
        (self.hw/'temp1_input').write_text('70000')
        self.assertTrue(self.control.inventory()['cooling_check']['can_start'])
        response = self.control.set_speed(cid, 90)
        self.assertTrue(response['verified'])
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 1)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 230)

    def test_exact_critical_boundary_blocks_preflight(self):
        (self.hw/'temp1_input').write_text('75000')
        self.assertFalse(self.control.inventory()['cooling_check']['can_start'])
        with self.assertRaises(core.TemperatureSafetyError):
            self.control.start_auto_test()
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)

    def test_inventory_ok_when_within_guard(self):
        status = self.control.inventory()['cooling_check']
        self.assertTrue(status['can_start'])
        self.assertIsNone(status['reason'])

    def test_no_temperature_sensors_gives_actionable_diagnostic(self):
        (self.hw/'temp1_input').unlink()
        status = self.control.inventory()['cooling_check']
        self.assertFalse(status['can_start'])
        self.assertIn('Не найдены', status['reason'])
        with self.assertRaisesRegex(core.SafetyError, 'Не найдены'):
            self.control.start_auto_test()

class AssistedMappingTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_fan_with_full_speed_plateau_is_mapped(self):
        cid = hwmon.discover()[0][0]['id']
        original = hwmon.write
        def simulate(path, duty):
            original(path, duty)
            if path.name == 'pwm1':
                rpm = 1450 if duty >= 205 else round(1450*duty/205)
                (self.hw/'fan1_input').write_text(str(rpm))
        with mock.patch.object(hwmon, 'write', side_effect=simulate), mock.patch.object(core.time, 'sleep', lambda _:None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'], 'complete')
        calibration = self.control.cfg['calibrations'][cid]
        self.assertEqual(len(calibration['fans']), 1)
        self.assertEqual(len(calibration['response_curve']), 1)
        entry = next(iter(calibration['response_curve'].values()))
        self.assertEqual(entry['rpm_at_100'], entry['rpm_at_84'])
        self.assertTrue(entry['matched'])

    def test_unrelated_fan_noise_does_not_create_false_link(self):
        cid = hwmon.discover()[0][0]['id']
        original = hwmon.write
        def simulate(path,duty):
            original(path,duty)
            if path.name=='pwm1':
                (self.hw/'fan1_input').write_text('1450' if duty != 175 else '1410')
        with mock.patch.object(hwmon, 'write', side_effect=simulate), mock.patch.object(core.time, 'sleep', lambda _:None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'], 'error')
        self.assertFalse(self.control.cfg['calibrations'])


class RepeatConfirmationTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_single_step_reaction_confirms_mapping(self):
        cid = hwmon.discover()[0][0]['id']
        old_write = hwmon.write
        def change(path, duty):
            old_write(path, duty)
            if path.name=='pwm1':
                (self.hw/'fan1_input').write_text(str(1500 if duty>=205 else round(1500*duty/205)))
        with mock.patch.object(hwmon,'write',side_effect=change), mock.patch.object(core.time,'sleep',lambda _: None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'],'complete')
        details = self.control.job['results'][0]['response_curve']
        record = next(iter(details.values()))
        self.assertTrue(record['matched'])
        self.assertIn('rpm_confirmed_low', record)
        self.assertIn('rpm_confirmed_high', record)

    def test_unresponsive_sensor_is_not_linked(self):
        cid=hwmon.discover()[0][0]['id']
        with mock.patch.object(core.time,'sleep',lambda _: None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'],'error')
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'),2)


class AssistedPairingTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_response_analysis_rejects_unrelated_fan(self):
        data = [(255, {'a': 1200, 'b': 1300}), (215, {'a': 1050, 'b': 1300}),
                (175, {'a': 800, 'b': 1305}), (150, {'a': 650, 'b': 1295})]
        linked, details = self.control._analyse_pairing(
            data, {'a': 1210, 'b': 1310},
            {'a': 650, 'b': 1300}, {'a': 1220, 'b': 1310})
        self.assertEqual(linked, ['a'])
        self.assertTrue(details['a']['matched'])
        self.assertFalse(details['b']['matched'])

    def test_response_analysis_requires_two_cycles(self):
        data = [(255, {'a': 1100}), (215, {'a': 950}),
                (175, {'a': 700}), (150, {'a': 600})]
        linked, details = self.control._analyse_pairing(
            data, {'a': 1090}, {'a': 1090}, {'a': 1090})
        self.assertEqual(linked, [])
        self.assertFalse(details['a']['matched'])

    def test_probe_does_not_abort_due_to_unrelated_slow_sensor(self):
        cid = hwmon.discover()[0][0]['id']
        saved = hwmon.write
        def hardware(path, duty):
            saved(path, duty)
            if path.name == 'pwm1':
                (self.hw/'fan1_input').write_text(str(round(1500*duty/255) if duty >= 115 else 200))
        (self.hw/'fan2_input').write_text('300')
        with mock.patch.object(hwmon, 'write', side_effect=hardware), \
             mock.patch.object(core.time, 'sleep', lambda *_: None):
            self.control._calibrate(cid)
        self.assertEqual(self.control.job['state'], 'complete')
        self.assertIn('fan1', self.control.cfg['calibrations'][cid]['fans'][0])

    def test_auto_pair_keeps_evidence_if_nothing_reacts(self):
        cid = hwmon.discover()[0][0]['id']
        with mock.patch.object(core.time, 'sleep', lambda *_: None):
            self.control._calibrate(cid)
        item = self.control.job['results'][0]
        self.assertEqual(item['status'], 'error')
        self.assertTrue(item['response_curve'])
        self.assertFalse(any(r.get('matched') for r in item['response_curve'].values()))


class RecoveryWithoutManualUnblockTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_manual_setpoint_auto_restores_saved_firmware_after_emergency(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 85)
        self.control.failsafe('simulated transient')
        self.assertIn(cid, self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)
        result = self.control.set_speed(cid, 90)
        self.assertTrue(result['verified'])
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 230)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 1)
        self.assertNotIn(cid, self.control.recovery)
        self.assertEqual(self.control.originals[cid]['enable'], 2)
        self.control.firmware(cid)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)

    def test_no_override_if_critical_temperature_still_present(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 85)
        self.control.failsafe('hot')
        (self.hw/'temp1_input').write_text('80000')
        with self.assertRaises(core.TemperatureSafetyError):
            self.control.set_speed(cid, 90)
        self.assertIn(cid, self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)

    def test_explicit_firmware_recovery_not_blocked_by_temperature(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 85)
        self.control.failsafe('test')
        (self.hw/'temp1_input').write_text('80000')
        result = self.control.firmware(cid)
        self.assertTrue(result['restored'])
        self.assertNotIn(cid, self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)

    def test_failed_firmware_restore_preserves_recovery(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 85)
        self.control.failsafe('test')
        saved = hwmon.restore
        def fail_non_emergency(entry, emergency=False):
            if not emergency:
                return ['simulated EC failure']
            return saved(entry, emergency=emergency)
        with mock.patch.object(hwmon, 'restore', side_effect=fail_non_emergency):
            with self.assertRaisesRegex(core.SafetyError, 'simulated EC failure'):
                self.control.set_speed(cid, 90)
        self.assertIn(cid, self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)

    def test_auto_test_retries_recoverable_channel(self):
        cid = hwmon.discover()[0][0]['id']
        self.control.set_speed(cid, 85)
        self.control.failsafe('test')
        with mock.patch.object(core.threading, 'Thread') as fake_thread:
            result = self.control.start_auto_test()
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['total'], 1)
        self.assertNotIn(cid, self.control.recovery)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)
        fake_thread.return_value.start.assert_called_once()
