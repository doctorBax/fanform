"""Global automated calibration must publish progress and assign RPM per channel."""
import threading
import unittest
from pathlib import Path
from unittest import mock
from test_core import FakeHwmon
from fanform import core, hwmon


class BatchCalibrationTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def add_second_header(self):
        (self.hw/'pwm2').write_text('255')
        (self.hw/'pwm2_enable').write_text('2')
        (self.hw/'pwm2_mode').write_text('1')

    def test_one_click_maps_each_rpm_to_correct_slider(self):
        self.add_second_header()
        original_write = hwmon.write
        def simulate(path, duty):
            original_write(path, duty)
            if path.name in ('pwm1', 'pwm2'):
                fan = 'fan1_input' if path.name == 'pwm1' else 'fan2_input'
                base = 1500 if path.name == 'pwm1' else 1200
                rpm = round(base * duty/255) if duty >= 115 else 200
                (self.hw/fan).write_text(str(rpm))
        with mock.patch.object(hwmon, 'write', side_effect=simulate), mock.patch.object(core.time, 'sleep', lambda _: None):
            start = self.control.start_auto_test()
            self.assertEqual(start['state'], 'running')
            self.control.calibration_thread.join(timeout=3)
        self.assertFalse(self.control.calibration_thread.is_alive())
        self.assertEqual(self.control.job['state'], 'complete')
        self.assertEqual(self.control.job['progress'], 100)
        self.assertEqual(len(self.control.job['results']), 2)
        self.assertTrue(all(row['status'] == 'mapped' for row in self.control.job['results']))
        channels = {c['id'].split('-')[-1]: c for c in self.control.inventory()['channels']}
        self.assertEqual(len(channels['pwm1']['linked_fans']), 1)
        self.assertEqual(len(channels['pwm2']['linked_fans']), 1)
        self.assertIn('fan1', channels['pwm1']['linked_fans'][0]['id'])
        self.assertIn('fan2', channels['pwm2']['linked_fans'][0]['id'])
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)
        self.assertEqual(hwmon.number(self.hw/'pwm2_enable'), 2)

    def test_inventory_is_pollable_during_calibration(self):
        gate = threading.Event()
        entered = threading.Event()
        def measure(*args, **kwargs):
            entered.set()
            gate.wait(3)
            return {'fake': 1500}
        with mock.patch.object(self.control, '_measure', side_effect=measure):
            self.control.start_auto_test()
            self.assertTrue(entered.wait(1), 'calibration never started measuring')
            polled = threading.Event()
            threading.Thread(target=lambda: (self.control.inventory(), polled.set()), daemon=True).start()
            try:
                self.assertTrue(polled.wait(1), 'api state was blocked by calibration')
                self.assertEqual(self.control.job['state'], 'running')
                self.assertGreater(self.control.job['progress'], 0)
            finally:
                gate.set()
                self.control.calibration_thread.join(3)
        self.assertFalse(self.control.calibration_thread.is_alive())

    def test_duplicate_tachometer_is_not_bound_twice(self):
        self.add_second_header()
        original_write = hwmon.write
        def simulate(path, duty):
            original_write(path, duty)
            if path.name in ('pwm1', 'pwm2'):
                (self.hw/'fan1_input').write_text(str(round(1500*duty/255)) if duty >= 115 else '200')
        with mock.patch.object(hwmon, 'write', side_effect=simulate), mock.patch.object(core.time, 'sleep', lambda _: None):
            self.control.start_auto_test()
            self.control.calibration_thread.join(3)
        self.assertEqual(len(self.control.job['results']), 2)
        self.assertTrue(all(row['status'] == 'error' for row in self.control.job['results']))
        self.assertFalse(self.control.cfg['calibrations'])

    def test_temperature_safety_interrupts_batch(self):
        self.add_second_header()
        original_write=hwmon.write
        def heat(path, duty):
            original_write(path,duty)
            if path.name=='pwm1' and duty == 215:
                (self.hw/'temp1_input').write_text('80000')
        with mock.patch.object(hwmon,'write',side_effect=heat),mock.patch.object(core.time,'sleep',lambda _:None):
            self.control.start_auto_test()
            self.control.calibration_thread.join(3)
        self.assertEqual(self.control.job['state'], 'error')
        self.assertEqual(self.control.job['current'], 1)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)
        self.assertEqual(hwmon.number(self.hw/'pwm2_enable'), 2)
        self.assertEqual(len(self.control.job['results']), 1)

    def test_active_channels_are_not_touched_by_global_test(self):
        self.add_second_header()
        cid=next(ch['id'] for ch in hwmon.discover()[0] if ch['id'].endswith('pwm1'))
        self.control.set_speed(cid, 90)
        with mock.patch.object(core.time, 'sleep', lambda _: None):
            self.control.start_auto_test()
            self.control.calibration_thread.join(3)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 230)
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 1)

if __name__ == '__main__': unittest.main()
