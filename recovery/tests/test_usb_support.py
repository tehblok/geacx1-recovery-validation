import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))


class USBSupportTests(unittest.TestCase):
    def setUp(self):
        import usb_support
        self.usb = usb_support
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.devices = self.root / 'devices'
        self.devices.mkdir()
        self.global_control = self.root / 'autosuspend'
        self.global_control.write_text('2\n')

    def device(self, name='1-2', vendor='0955', speed='480', number='4'):
        path = self.devices / name
        (path / 'power').mkdir(parents=True)
        for key, value in {'idVendor': vendor, 'idProduct': '7023', 'speed': speed,
                           'busnum': '1', 'devnum': number, 'power/control': 'auto'}.items():
            (path / key).write_text(value)
        return path

    def test_speed_is_link_rate_and_hub_is_reported(self):
        path = self.device('1-2.3')
        report = self.usb.describe_connection(path)
        self.assertIn('480', report)
        self.assertIn('Мбит/с', report)
        self.assertIn('хаб', report)
        self.assertIn('не скорость записи', report)

    def test_slow_and_missing_speed_do_not_claim_good_cable(self):
        path = self.device(speed='12')
        self.assertIn('низкая', self.usb.describe_connection(path))
        (path / 'speed').unlink()
        self.assertIn('не удалось', self.usb.describe_connection(path))

    def test_power_settings_restored_on_failure_other_devices_untouched(self):
        target = self.device()
        other = self.device('1-3', '1234')
        with self.assertRaisesRegex(RuntimeError, 'vendor failed'):
            with self.usb.keep_usb_awake(self.devices, self.global_control, lambda _: None):
                self.assertEqual(self.global_control.read_text(), '-1')
                self.assertEqual((target / 'power/control').read_text(), 'on')
                self.assertEqual((other / 'power/control').read_text(), 'auto')
                raise RuntimeError('vendor failed')
        self.assertEqual(self.global_control.read_text(), '2')
        self.assertEqual((target / 'power/control').read_text(), 'auto')

    def test_replacement_device_is_not_changed_by_cleanup(self):
        target = self.device()
        with self.usb.keep_usb_awake(self.devices, self.global_control, lambda _: None):
            (target / 'devnum').write_text('5')
            (target / 'power/control').write_text('replacement-value')
        self.assertEqual((target / 'power/control').read_text(), 'replacement-value')
        self.assertEqual(self.global_control.read_text(), '2')

    def test_unavailable_power_controls_are_visible(self):
        path = self.device()
        (path / 'power/control').unlink()
        notices = []
        with self.usb.keep_usb_awake(self.devices, self.root / 'missing', notices.append):
            pass
        self.assertTrue(any('не удалось' in line.lower() for line in notices))

    def test_concurrent_power_policy_change_is_not_overwritten(self):
        target = self.device()
        notices = []
        with self.usb.keep_usb_awake(self.devices, self.global_control, notices.append):
            self.global_control.write_text('5')
            (target / 'power/control').write_text('auto')
        self.assertEqual(self.global_control.read_text(), '5')
        self.assertEqual((target / 'power/control').read_text(), 'auto')
        self.assertTrue(any('другим' in line for line in notices))


class USBPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('wizard_usb_test', ROOT / 'wizard.py')
        cls.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.w)

    def test_replug_during_preflight_stops_before_vendor_command(self):
        runner = Mock()
        identity = ('/sys/usb/1-2', '1', '4', '')
        device = {'sysfs': identity[0], 'product_id': '7023'}
        with patch.object(self.w.core, 'recovery_devices', return_value=[device]), \
             patch.object(self.w, 'usb_identity', side_effect=[identity, (identity[0], '1', '5', '')]), \
             patch.object(self.w.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(self.w.core.RecoveryError):
                self.w.usb_preflight(identity, runner, samples=2, interval=0)
        runner.run.assert_not_called()

    def test_stable_device_is_logged_without_writing_to_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.w.Runner(Path(tmp) / 'test.log', quiet=True)
            identity = (tmp, '1', '4', '')
            with patch.object(self.w, 'recheck_recovery_identity') as recheck, \
                 patch.object(self.w.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                self.w.usb_preflight(identity, runner, samples=3, interval=0)
            self.assertEqual(recheck.call_count, 3)
            self.assertIn('USB', runner.log.read_text())
            self.assertNotIn('\n$ ', runner.log.read_text())


if __name__ == '__main__':
    unittest.main()
