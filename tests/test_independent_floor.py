"""Pairing must survive an inconclusive minimum-RPM floor probe."""
import unittest
from pathlib import Path
from unittest import mock

from test_core import FakeHwmon
from fanform import hwmon


class IndependentFloorTests(unittest.TestCase):
    setUp = FakeHwmon.setUp

    def test_confirmed_pwm_to_fan2_pair_persists_when_floor_unstable(self):
        channel = hwmon.discover()[0][0]
        cid = channel['id']
        fans = {f['id'].split('-')[-1]: f['id'] for f in hwmon.discover()[1]}
        fan1, fan2 = fans['fan1'], fans['fan2']
        # User-observed PWM2/Fan2: 1597 -> 1496 -> 1221 -> 1017 -> 1582.
        phases = [
            (1388, 1597), (1373, 1496), (1364, 1221), (1360, 1017),
            (1330, 1582), (1355, 1030), (1385, 1590),
        ]
        measurements = []
        for left, right in phases:
            measurements.append((
                {fan1: left, fan2: right},
                {'stable_fans': [fan1, fan2], 'unstable_fans': [],
                 'elapsed_seconds': 18, 'max_seconds': 50, 'timed_out': False}))
        measurements.append((
            {fan1: 1362, fan2: 950},
            {'stable_fans': [fan1], 'unstable_fans': [fan2],
             'elapsed_seconds': 45, 'max_seconds': 45, 'timed_out': True}))
        with mock.patch.object(self.control, '_measure_stable', side_effect=measurements):
            result = self.control._calibrate_channel(cid, lambda *args: None)

        self.assertEqual(result['status'], 'mapped')
        self.assertEqual(result['fans'], [fan2])
        self.assertFalse(result['floor_verified'])
        self.assertIn('не стабилизировались', result['floor_note'])
        cal = self.control.cfg['calibrations'][cid]
        self.assertEqual(cal['fans'], [fan2])
        self.assertEqual(cal['safe_pwm'], 215)
        self.assertFalse(cal['floor_verified'])
        self.assertEqual(hwmon.number(self.hw/'pwm1_enable'), 2)
        self.assertEqual(hwmon.number(self.hw/'pwm1'), 255)
        self.assertEqual(len(measurements), 8)


if __name__ == '__main__':
    unittest.main()
