import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'recovery/lib'))
import recovery_core as core
spec = importlib.util.spec_from_file_location('wizard_pid', ROOT / 'recovery/wizard.py')
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

class RecoveryPIDTests(unittest.TestCase):
    def test_production_32gb_pid_is_recognized_but_other_families_are_not(self):
        for pid, supported in [('7223', True), ('7023', True), ('7323', False), ('7226', False), ('ffff', False)]:
            with self.subTest(pid=pid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); device = root / '3-1'; device.mkdir()
                (device / 'idVendor').write_text('0955')
                (device / 'idProduct').write_text(pid)
                with patch.object(core, 'SYS_USB_DEVICES', root):
                    found = core.recovery_devices()
                self.assertEqual(found[0]['supported'], supported)

    def test_7223_passes_prompt_and_recheck_without_serial(self):
        device = {'sysfs': '/sys/usb/3-1', 'product_id': '7223', 'serial': ''}
        identity = ('/sys/usb/3-1', '3', '7', '')
        with patch.object(core, 'recovery_devices', return_value=[device]), \
             patch.object(wizard, 'ask', return_value='0'), \
             patch.object(wizard, 'usb_identity', return_value=identity), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.show_recovery(), device)
            wizard.recheck_recovery_identity(identity)

    def test_7223_reaches_vendor_only_after_confirmation_and_usb_checks(self):
        device = {'sysfs': '/sys/usb/3-1', 'product_id': '7223', 'serial': ''}
        identity = ('/sys/usb/3-1', '3', '7', '')
        runner = Mock(log=Path('/tmp/pid-test.log'))
        with patch.object(wizard, 'require_root'), patch.object(wizard, 'print_checks', return_value=True), \
             patch.object(core, 'check_runtime', return_value=[]), patch.object(core, 'validate_prepared'), \
             patch.object(wizard, 'show_recovery', return_value=device), \
             patch.object(wizard, 'usb_identity', return_value=identity), \
             patch.object(wizard, 'ask_erase', return_value=True), \
             patch.object(core, 'recovery_devices', return_value=[device]), \
             patch.object(wizard, 'flash_host_state', return_value=contextlib.nullcontext()), \
             patch.object(core, 'preserve_prepared_initrd', return_value=contextlib.nullcontext()), \
             patch.object(wizard, 'usb_preflight') as preflight, patch.object(wizard, 'pause'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wizard.flash_wizard(Path('/prepared'), runner, fixed_mode='emmc'))
        preflight.assert_called_once_with(identity, runner)
        runner.run.assert_called_once_with(['./flash.sh', '--usb-instance', '3-1', core.BOARD_NAME, 'mmcblk0p1'], Path('/prepared'))
        self.assertIn('"${board_id}" != "3701"', core._BOARD_GUARD)
        self.assertIn('"${board_sku}" != "0004"', core._BOARD_GUARD)

    def test_two_boards_or_foreign_pid_still_blocked(self):
        for devices in ([{'product_id': '7223'}, {'product_id': '7023'}], [{'product_id': '7323'}], []):
            with patch.object(core, 'recovery_devices', return_value=devices):
                with self.assertRaises(core.RecoveryError):
                    wizard.recheck_recovery_identity(('3-1','3','7',''))

if __name__ == '__main__': unittest.main()
