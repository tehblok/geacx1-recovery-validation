from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import recovery_core as core


class NetworkWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.l4t = Path(self.tmp.name)
        self.script = self.l4t / 'tools/kernel_flash/l4t_network_flash.func'
        self.script.parent.mkdir(parents=True)
        self.source = ROOT / 'vendor' / core.VENDOR_NAME / 'Linux_for_Tegra/tools/kernel_flash/l4t_network_flash.func'
        shutil.copyfile(self.source, self.script)
        self.original = self.source.read_bytes()

    def run_wait(self, fail_count, timeout=None):
        # Source the exact vendor functions, replacing only ping/sleep for a
        # deterministic test. No USB, networking or flash command is called.
        code = '''source "$1"
unset timeout
if [ -n "$3" ]; then timeout="$3"; fi
attempts=0
# Use a global limit: arguments to the function are the target address.
limit="$2"
ping_flash_device() { attempts=$((attempts + 1)); [ "$attempts" -gt "$limit" ]; }
sleep() { :; }
wait_for_flash_ssh "::1"
printf '\\nATTEMPTS=%s\\n' "$attempts"
'''
        return subprocess.run(['bash', '-c', code, 'test', str(self.script), str(fail_count), str(timeout or '')],
                              text=True, capture_output=True, timeout=10)

    def test_slow_enumeration_after_70_failures_succeeds(self):
        core.extend_initrd_network_wait(self.l4t)
        result = self.run_wait(70)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ATTEMPTS=71', result.stdout)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_unreachable_target_still_times_out_and_explicit_limit_is_kept(self):
        core.extend_initrd_network_wait(self.l4t)
        for timeout in (None, 3):
            result = self.run_wait(1000, timeout)
            self.assertEqual(result.returncode, 105, result.stdout + result.stderr)

    def test_different_vendor_version_is_rejected_without_mutation(self):
        self.script.write_text(self.script.read_text() + '\n# modified vendor\n')
        before = self.script.read_bytes()
        with self.assertRaises(core.RecoveryError):
            core.extend_initrd_network_wait(self.l4t)
        self.assertEqual(self.script.read_bytes(), before)

if __name__ == '__main__':
    unittest.main()
