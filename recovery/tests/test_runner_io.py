import contextlib
import importlib.util
import io
import os
from pathlib import Path
import selectors
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class InterruptedSelector(selectors.DefaultSelector):
    def __init__(self):
        super().__init__()
        self.interrupted = False

    def select(self, timeout=None):
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return super().select(timeout)


class RunnerIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('wizard_io_test', ROOT / 'wizard.py')
        cls.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.w)

    def test_waiting_for_stop_keeps_draining_large_output(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd) as stdin, tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(os.close, write_fd)
            runner = self.w.Runner(Path(tmp) / 'log', quiet=True)
            runner.destructive = True
            command = [sys.executable, '-c',
                       'import sys,pathlib; sys.stdout.write("x"*2097152); sys.stdout.flush(); pathlib.Path("finished").touch()']
            with patch.object(self.w.selectors, 'DefaultSelector', InterruptedSelector), \
                 patch.object(self.w.sys, 'stdin', stdin), \
                 patch('builtins.input', side_effect=AssertionError('blocking input prevents log draining')), \
                 contextlib.redirect_stdout(io.StringIO()):
                runner.run(command, Path(tmp))
            self.assertTrue((Path(tmp) / 'finished').is_file())
            self.assertIn('[exit=0]', runner.log.read_text())
            self.assertGreater(runner.log.stat().st_size, 2097152)

    def test_explicit_stop_terminates_active_command(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'STOP\n')
        os.close(write_fd)
        with os.fdopen(read_fd) as stdin, tempfile.TemporaryDirectory() as tmp:
            runner = self.w.Runner(Path(tmp) / 'log', quiet=True)
            runner.destructive = True
            with patch.object(self.w.selectors, 'DefaultSelector', InterruptedSelector), \
                 patch.object(self.w.sys, 'stdin', stdin), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run([sys.executable, '-c', 'import time; print("started",flush=True); time.sleep(30)'], Path(tmp))

    def test_partial_stop_eof_and_blank_do_not_authorize_cancel(self):
        prompt = self.w.StopConfirmation()
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd) as stdin, patch.object(self.w.sys, 'stdin', stdin):
            with contextlib.redirect_stdout(io.StringIO()):
                prompt.request()
                os.write(write_fd, b'STO')
                self.assertIsNone(prompt.poll())
                os.close(write_fd)
                self.assertFalse(prompt.poll())
        self.assertFalse(prompt.active)

if __name__ == '__main__':
    unittest.main()
