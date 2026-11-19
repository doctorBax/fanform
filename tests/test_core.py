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
        (self.hw/'temp1_input').write_text('70000')
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
        with self.assertRaises(core.SafetyError):
            self.control.set_speed(cid,90)
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
            if path.name=='pwm1' and duty == 210:
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
