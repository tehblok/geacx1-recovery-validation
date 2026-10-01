import importlib.util
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock, call

P = Path(__file__).resolve().parents[1]

class WizardSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('wizard', P / 'wizard.py')
        cls.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.w)

    def test_confirmation_is_explicit(self):
        self.assertTrue(self.w.confirmation_matches('ERASE GEACX1 32GB', 'emmc'))
        for s in ('', 'yes', 'да', 'ERASE GEACX1 64GB', 'erase geacx1 32gb'):
            self.assertFalse(self.w.confirmation_matches(s, 'emmc'))
        self.assertFalse(self.w.confirmation_matches('ERASE GEACX1 32GB', 'qspi'))

    def test_recognizes_actionable_errors(self):
        for text, expected in [('mount.nfs: access denied', 'NFS'), ('USB write timeout', 'USB'),
                               ('No space left on device', 'мест'), ('не хватает свободного места', 'мест'),
                               ('контрольная сумма SHA-256 не совпала', 'Пакеты'), ('путь содержит кириллицу', 'Путь'),
                               ('signature verification failed', 'Secure Boot')]:
            self.assertIn(expected, '\n'.join(self.w.diagnose(text)))

    def test_runner_propagates_failure(self):
        with tempfile.TemporaryDirectory() as d:
            runner = self.w.Runner(Path(d)/'test.log', quiet=True)
            with self.assertRaises(self.w.core.RecoveryError):
                runner.run(['/bin/sh', '-c', 'echo diagnostic; exit 7'], Path(d))
            self.assertIn('diagnostic',runner.log.read_text())

    def test_cancelled_confirmation_does_not_run(self):
        with patch('builtins.input', return_value='yes'):
            self.assertFalse(self.w.ask_erase('emmc'))

    def test_unknown_mode_never_confirms(self):
        with self.assertRaises(ValueError):
            self.w.confirmation_matches('ERASE GEACX1 32GB','unknown')

    def test_usb_replug_changes_approved_identity_without_serial(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p/'busnum').write_text('1')
            (p/'devnum').write_text('4')
            device = {'sysfs':d, 'serial':''}
            old = self.w.usb_identity(device)
            (p/'devnum').write_text('5')
            self.assertNotEqual(old, self.w.usb_identity(device))

    def test_flash_cancel_never_runs_vendor(self):
        runner = Mock(log=Path('/tmp/test.log'))
        device = {'sysfs':'/sys/usb/1-1','product_id':'7023'}
        with patch.object(self.w,'require_root'), patch.object(self.w,'print_checks',return_value=True), \
             patch.object(self.w.core,'check_runtime',return_value=[]), \
             patch.object(self.w.core,'validate_prepared'), patch.object(self.w,'select_mode',return_value='emmc'), \
             patch.object(self.w,'show_recovery',return_value=device), patch.object(self.w,'usb_identity',return_value=('1-1','1','2','')), \
             patch.object(self.w,'ask_erase',return_value=False), patch.object(self.w,'pause'):
            self.w.flash_wizard(Path('/tmp/l4t'),runner)
        runner.run.assert_not_called()

    def test_failure_resets_destructive_state_and_restores_context(self):
        runner = Mock(log=Path('/tmp/test.log'))
        runner.run.side_effect = self.w.core.RecoveryError('USB failed')
        device = {'sysfs':'/sys/usb/1-1','product_id':'7023'}
        restored=[]
        @contextlib.contextmanager
        def host(_):
            try: yield
            finally: restored.append(True)
        with patch.object(self.w,'require_root'), patch.object(self.w,'print_checks',return_value=True), \
             patch.object(self.w.core,'check_runtime',return_value=[]), \
             patch.object(self.w.core,'validate_prepared'), patch.object(self.w,'select_mode',return_value='emmc'), \
             patch.object(self.w,'show_recovery',return_value=device), patch.object(self.w,'usb_identity',return_value=('1-1','1','2','')), \
             patch.object(self.w,'ask_erase',return_value=True), patch.object(self.w.core,'recovery_devices',return_value=[device]), \
             patch.object(self.w,'flash_host_state',host), \
             patch.object(self.w.core,'preserve_prepared_initrd',return_value=contextlib.nullcontext()) as preserve:
            with self.assertRaises(self.w.core.RecoveryError):
                self.w.flash_wizard(Path('/tmp/l4t'),runner)
        self.assertEqual(restored,[True])
        self.assertFalse(runner.destructive)
        self.assertIn('--usb-instance',runner.run.call_args.args[0])
        preserve.assert_called_once_with(Path('/tmp/l4t'))

    def test_backup_host_state_restores_nfs_and_flash_state_on_error(self):
        for was_active in (False, True):
            with self.subTest(nfs_active=was_active):
                runner = Mock()
                restored = []
                @contextlib.contextmanager
                def flash_context(_runner):
                    try:
                        yield
                    finally:
                        restored.append(True)
                with patch.object(self.w.subprocess, 'run', return_value=Mock(returncode=0 if was_active else 3)), \
                     patch.object(self.w, 'flash_host_state', flash_context):
                    with self.assertRaisesRegex(RuntimeError, 'simulated vendor error'):
                        with self.w.backup_host_state(runner):
                            raise RuntimeError('simulated vendor error')
                self.assertEqual(restored, [True])
                expected = [] if was_active else [
                    call(['systemctl', 'start', 'nfs-kernel-server.service'], self.w.BASE),
                    call(['systemctl', 'stop', 'nfs-kernel-server.service'], self.w.BASE),
                ]
                self.assertEqual(runner.run.call_args_list, expected)

    def test_guided_cancel_after_prepare_never_starts_flash(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared), \
             patch.object(self.w, 'choose', side_effect=['1', '0']), \
             patch.object(self.w, 'flash_wizard') as flash:
            self.w.guided_recovery(Path('/tmp/vendor'), runner)
        flash.assert_not_called()

    def test_guided_flow_uses_fixed_emmc_mode(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared), \
             patch.object(self.w, 'choose', side_effect=['1', '1']), \
             patch.object(self.w, 'flash_wizard') as flash:
            self.w.guided_recovery(Path('/tmp/vendor'), runner)
        flash.assert_called_once_with(prepared, runner, fixed_mode='emmc')

    def test_guided_both_never_starts_nvme_when_emmc_not_successful(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared), \
             patch.object(self.w, 'choose', side_effect=['3', '1']), \
             patch.object(self.w, 'flash_wizard', return_value=False) as flash:
            self.w.guided_recovery(Path('/tmp/vendor'), runner)
        flash.assert_called_once_with(prepared, runner, fixed_mode='emmc')

    def test_guided_both_requires_separate_transition_before_nvme(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared), \
             patch.object(self.w, 'choose', side_effect=['3', '1', '0']), \
             patch.object(self.w, 'flash_wizard', return_value=True) as flash:
            self.w.guided_recovery(Path('/tmp/vendor'), runner)
        flash.assert_called_once_with(prepared, runner, fixed_mode='emmc')

    def test_guided_both_runs_nvme_only_after_separate_transition(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared), \
             patch.object(self.w, 'choose', side_effect=['3', '1', '1']), \
             patch.object(self.w, 'flash_wizard', side_effect=[True, True]) as flash:
            self.assertTrue(self.w.guided_recovery(Path('/tmp/vendor'), runner))
        self.assertEqual(flash.call_args_list, [
            call(prepared, runner, fixed_mode='emmc'),
            call(prepared, runner, fixed_mode='nvme'),
        ])

    def test_fixed_emmc_flash_does_not_open_advanced_mode_menu(self):
        runner = Mock(log=Path('/tmp/test.log'))
        device = {'sysfs':'/sys/usb/1-1','product_id':'7023'}
        with patch.object(self.w,'require_root'), patch.object(self.w,'print_checks',return_value=True), \
             patch.object(self.w.core,'check_runtime',return_value=[]), \
             patch.object(self.w.core,'validate_prepared'), \
             patch.object(self.w,'select_mode',side_effect=AssertionError('advanced menu opened')), \
             patch.object(self.w,'show_recovery',return_value=device), \
             patch.object(self.w,'usb_identity',return_value=('1-1','1','2','')), \
             patch.object(self.w,'ask_erase',return_value=False), patch.object(self.w,'pause'):
            self.w.flash_wizard(Path('/tmp/l4t'), runner, fixed_mode='emmc')
        runner.run.assert_not_called()

    def test_nvme_network_failure_stops_before_usb_prompt(self):
        runner = Mock(log=Path('/tmp/test.log'))
        with patch.object(self.w, 'require_root'), \
             patch.object(self.w, 'print_checks', return_value=True), \
             patch.object(self.w.core, 'check_runtime', return_value=[]), \
             patch.object(self.w.core, 'validate_prepared'), \
             patch.object(self.w, 'confirm_nvme_requirements', return_value=True), \
             patch.object(self.w.core, 'require_initrd_network',
                          side_effect=self.w.core.RecoveryError('IPv6 disabled')), \
             patch.object(self.w, 'show_recovery') as recovery:
            with self.assertRaisesRegex(self.w.core.RecoveryError, 'IPv6'):
                self.w.flash_wizard(Path('/tmp/l4t'), runner, fixed_mode='nvme')
        recovery.assert_not_called()

    def test_invalid_saved_work_is_blocked_before_flash(self):
        runner = Mock(log=Path('/tmp/test.log'))
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'last-work.json'
            saved.write_text('{"l4t":"/broken/Linux_for_Tegra"}')
            with patch.object(self.w, 'LAST_WORK', saved), \
                 patch.object(self.w.core, 'validate_prepared', side_effect=self.w.core.RecoveryError('marker missing')), \
                 patch.object(self.w, 'flash_wizard') as flash:
                with self.assertRaisesRegex(self.w.core.RecoveryError, 'marker missing'):
                    self.w.continue_prepared(runner)
            flash.assert_not_called()

    def test_guided_uses_simple_preparation_without_path_prompts(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'prepare_wizard', return_value=prepared) as prepare, \
             patch.object(self.w, 'choose', side_effect=['1', '0']), \
             patch.object(self.w, 'flash_wizard'):
            self.w.guided_recovery(Path('/tmp/vendor'), runner)
        prepare.assert_called_once_with(Path('/tmp/vendor'), runner, pause_after=False, simple=True)

    def test_continue_prepared_asks_target_and_can_resume_nvme(self):
        runner = Mock(log=Path('/tmp/test.log'))
        prepared = Path('/tmp/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'saved_work_path', return_value=prepared), \
             patch.object(self.w.core, 'validate_prepared'), \
             patch.object(self.w, 'select_guided_target', return_value='nvme'), \
             patch.object(self.w, 'choose', return_value='1'), \
             patch.object(self.w, 'flash_wizard', return_value=True) as flash:
            self.assertTrue(self.w.continue_prepared(runner))
        flash.assert_called_once_with(prepared, runner, fixed_mode='nvme')

    def test_status_does_not_say_ready_when_action_failed(self):
        output = io.StringIO()
        def fail():
            raise self.w.core.RecoveryError('SHA-256 mismatch')
        with contextlib.redirect_stdout(output):
            with self.assertRaises(self.w.core.RecoveryError):
                self.w.run_with_status('Проверка', fail)
        self.assertNotIn('Готово', output.getvalue())
        self.assertIn('ошибк', output.getvalue())

    def test_status_waits_for_readonly_worker_then_reraises_interrupt(self):
        class InterruptedThread:
            alive = True
            joins = 0
            def start(self):
                pass
            def is_alive(self):
                return self.alive
            def join(self, _timeout=None):
                self.joins += 1
                if self.joins == 1:
                    raise KeyboardInterrupt
                self.alive = False
        fake = InterruptedThread()
        output = io.StringIO()
        with patch.object(self.w.threading, 'Thread', return_value=fake), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                self.w.run_with_status('Проверка', lambda: None)
        self.assertFalse(fake.alive)
        self.assertNotIn('Готово', output.getvalue())

    def test_second_interrupt_does_not_reset_pending_stop_answer(self):
        prompt = self.w.StopConfirmation()
        with patch.object(self.w.sys, 'stdin', Mock()), contextlib.redirect_stdout(io.StringIO()):
            prompt.request()
            prompt.buffer = b'STO'
            prompt.request()
        self.assertTrue(prompt.active)
        self.assertEqual(prompt.buffer, b'STO')

    def test_checksum_failure_is_saved_in_host_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            def initial_report(_vendor, _work, report):
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('Итог: проверки компьютера пройдены\n')
                return True
            with patch.object(self.w, 'BASE', base), \
                 patch.object(self.w, 'write_host_report', side_effect=initial_report), \
                 patch.object(self.w.core, 'verify_manifest', side_effect=self.w.core.RecoveryError('SHA-256 mismatch')):
                with self.assertRaises(self.w.core.RecoveryError):
                    self.w.check_computer_and_bundle(Path('/vendor'), Path('/work'))
            reports = list((base / 'logs').glob('check-*.txt'))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text()
            self.assertIn('Контрольные суммы: ОШИБКА', text)
            self.assertIn('повторно скач', text)

    def test_cancelled_checksum_check_does_not_claim_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            def initial_report(_vendor, _work, report):
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('Итог: проверки компьютера пройдены\n')
                return True
            with patch.object(self.w, 'BASE', base), \
                 patch.object(self.w, 'write_host_report', side_effect=initial_report), \
                 patch.object(self.w, 'run_with_status', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.w.check_computer_and_bundle(Path('/vendor'), Path('/work'))
            report = next((base / 'logs').glob('check-*.txt'))
            text = report.read_text()
            self.assertIn('проверка отменена', text.lower())
            self.assertIn('целостность не подтверждена', text.lower())
            self.assertNotIn('ОШИБКА', text)
            self.assertNotIn('скач', text.lower())

    def test_host_report_is_readable_and_has_next_action(self):
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / 'host-report.txt'
            checks = [
                {'ok': True, 'name': 'Ubuntu', 'detail': '24.04 amd64'},
                {'ok': False, 'name': 'Свободное место', 'detail': '22.0 GiB'},
                {'ok': False, 'name': 'Безопасный путь', 'detail': '/media/Диск/прошивка'},
            ]
            with patch.object(self.w.core, 'host_checks', return_value=checks), \
                 patch.object(self.w.core, 'inspect_bundle', return_value={'release':'R39.2.0'}):
                ok = self.w.write_host_report(Path('/tmp/vendor'), Path('/tmp/work'), report)
            text = report.read_text()
            self.assertFalse(ok)
            self.assertIn('Свободное место', text)
            self.assertIn('Следующее действие', text)
            self.assertIn('80 ГиБ', text)
            self.assertIn('меню 6', text)
            self.assertIn('латин', text)
            self.assertNotIn("{'ok':", text)

    def test_restore_cancel_never_runs_nvidia(self):
        runner = Mock(log=Path('/tmp/backup.log'))
        metadata = {'scope': 'emmc', 'board_spec': '3701-500-0004-A.0-production-00-geacx1-32gb-jp72-', 'files': {}}
        device = {'sysfs':'/sys/usb/1-1', 'product_id':'7023'}
        with patch.object(self.w, 'require_root'), \
             patch.object(self.w, 'ask', side_effect=['/backup', 'wrong phrase']), \
             patch.object(self.w, 'run_with_status', side_effect=lambda _label, action: action()), \
             patch.object(self.w.backup, 'validate_snapshot', return_value=metadata), \
             patch.object(self.w, 'backup_prepared_path', return_value=Path('/l4t')), \
             patch.object(self.w.shutil, 'disk_usage', return_value=Mock(free=100 * 1024**3)), \
             patch.object(self.w.core, 'require_initrd_network'), \
             patch.object(self.w, 'show_recovery', return_value=device), \
             patch.object(self.w, 'usb_identity', return_value=('1-1','1','2','')), \
             patch.object(self.w.backup, 'build_command', return_value=['official', '-r']), \
             patch.object(self.w, 'pause'):
            self.assertFalse(self.w.restore_full_backup(runner))
        runner.run.assert_not_called()

    def test_restore_rechecks_snapshot_and_usb_then_runs_official_argv(self):
        runner = Mock(log=Path('/tmp/backup.log'))
        metadata = {'scope': 'emmc_nvme', 'board_spec': '3701-500-0004-A.0-production-00-geacx1-32gb-jp72-', 'files': {}}
        device = {'sysfs':'/sys/usb/1-1', 'product_id':'7023'}
        @contextlib.contextmanager
        def context(*_args):
            yield Path('/staged')
        with patch.object(self.w, 'require_root'), \
             patch.object(self.w, 'ask', side_effect=['/backup', 'RESTORE GEACX1 BACKUP']), \
             patch.object(self.w, 'run_with_status', side_effect=lambda _label, action: action()), \
             patch.object(self.w.backup, 'validate_snapshot', return_value=metadata) as validate, \
             patch.object(self.w, 'backup_prepared_path', return_value=Path('/l4t')), \
             patch.object(self.w.shutil, 'disk_usage', return_value=Mock(free=100 * 1024**3)), \
             patch.object(self.w.core, 'require_initrd_network'), \
             patch.object(self.w, 'show_recovery', return_value=device), \
             patch.object(self.w, 'usb_identity', return_value=('1-1','1','2','')), \
             patch.object(self.w, 'usb_preflight') as usb_check, \
             patch.object(self.w.core, 'validate_prepared'), \
             patch.object(self.w.backup, 'build_command', return_value=['official', '-e', 'mmcblk0:nvme0n1', '-r', 'board']), \
             patch.object(self.w.backup, 'isolated_images', context), \
             patch.object(self.w, 'backup_host_state', context), \
             patch.object(self.w.core, 'preserve_prepared_initrd', context), \
             patch.object(self.w, 'pause'):
            self.assertTrue(self.w.restore_full_backup(runner))
        self.assertEqual(validate.call_count, 2)
        usb_check.assert_called_once_with(('1-1', '1', '2', ''), runner)
        runner.run.assert_called_once_with(['official', '-e', 'mmcblk0:nvme0n1', '-r', 'board'], Path('/l4t'))
        self.assertFalse(runner.destructive)

    def test_backup_menu_routes_verify_without_root_or_usb(self):
        runner = Mock()
        with patch.object(self.w, 'choose', side_effect=['2', '0']), \
             patch.object(self.w, 'verify_full_backup') as verify:
            self.w.backup_menu(runner)
        verify.assert_called_once_with()

    def test_backup_reuses_saved_environment_without_path_prompt(self):
        runner = Mock()
        prepared = Path('/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'LAST_WORK', Mock(is_file=Mock(return_value=True))), \
             patch.object(self.w, 'saved_work_path', return_value=prepared), \
             patch.object(self.w, '_validate_backup_prepared', return_value=prepared) as validate, \
             patch.object(self.w, 'ask', side_effect=AssertionError('path prompt opened')):
            self.assertEqual(self.w.backup_prepared_path(runner), prepared)
        validate.assert_called_once_with(prepared)

    def test_backup_can_prepare_environment_without_flashing_board(self):
        runner = Mock()
        prepared = Path('/prepared/Linux_for_Tegra')
        with patch.object(self.w, 'LAST_WORK', Mock(is_file=Mock(return_value=False))), \
             patch.object(self.w, 'choose', return_value='1'), \
             patch.object(self.w, 'vendor_path', return_value=Path('/vendor')), \
             patch.object(self.w, 'prepare_wizard', return_value=prepared) as prepare, \
             patch.object(self.w, '_validate_backup_prepared', return_value=prepared), \
             patch.object(self.w, 'show_recovery') as recovery:
            self.assertEqual(self.w.backup_prepared_path(runner), prepared)
        prepare.assert_called_once_with(Path('/vendor'), runner, pause_after=False, simple=True)
        recovery.assert_not_called()

if __name__ == '__main__':
    unittest.main()
