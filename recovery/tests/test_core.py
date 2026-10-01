import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))

import recovery_core as core


def _elf_aarch64() -> bytes:
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    data[18:20] = (183).to_bytes(2, "little")
    return bytes(data)


def _rootfs_tar(path: Path, *, ubuntu="24.04", machine=183, extra=None):
    with tarfile.open(path, "w:bz2") as archive:
        os_release = f'ID=ubuntu\nVERSION_ID="{ubuntu}"\n'.encode()
        info = tarfile.TarInfo("etc/os-release")
        info.size = len(os_release)
        archive.addfile(info, io.BytesIO(os_release))

        elf = bytearray(_elf_aarch64())
        elf[18:20] = machine.to_bytes(2, "little")
        info = tarfile.TarInfo("bin/bash")
        info.mode = 0o755
        info.size = len(elf)
        archive.addfile(info, io.BytesIO(elf))

        for info, content in extra or []:
            if content is not None:
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content) if content is not None else None)


class RootfsValidationTests(unittest.TestCase):
    def test_archive_validation_reads_compressed_archive_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "rootfs.tbz2"
            _rootfs_tar(archive)
            with mock.patch.object(core.tarfile, "open", wraps=tarfile.open) as opened:
                self.assertEqual(core.validate_rootfs(archive)["architecture"], "aarch64")
                self.assertEqual(opened.call_count, 1)

    def test_rootfs_identity_rejects_custom_modified_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "external-rootfs.tbz2"
            _rootfs_tar(archive)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with mock.patch.object(core, "ROOTFS_SHA256", digest):
                core.verify_rootfs_identity(archive)
                extra = tarfile.TarInfo("etc/systemd/system/added.service")
                _rootfs_tar(archive, extra=[(extra, b"changed")])
                self.assertEqual(core.validate_rootfs(archive)["version"], "24.04")
                with self.assertRaisesRegex(core.RecoveryError, "SHA-256 rootfs"):
                    core.verify_rootfs_identity(archive)

    def test_accepts_root_dot_entry_and_os_release_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "rootfs.tbz2"
            with tarfile.open(archive, "w:bz2") as output:
                root = tarfile.TarInfo("./")
                root.type = tarfile.DIRTYPE
                output.addfile(root)
                release = b'ID=ubuntu\nVERSION_ID="24.04"\n'
                target = tarfile.TarInfo("usr/lib/os-release")
                target.size = len(release)
                output.addfile(target, io.BytesIO(release))
                link = tarfile.TarInfo("etc/os-release")
                link.type = tarfile.SYMTYPE
                link.linkname = "../usr/lib/os-release"
                output.addfile(link)
                bash = tarfile.TarInfo("usr/bin/bash")
                payload = _elf_aarch64()
                bash.size = len(payload)
                output.addfile(bash, io.BytesIO(payload))

            self.assertEqual(core.validate_rootfs(archive)["version"], "24.04")

    def test_accepts_ubuntu_2404_aarch64_archive_and_absolute_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "rootfs.tbz2"
            link = tarfile.TarInfo("bin/sh")
            link.type = tarfile.SYMTYPE
            link.linkname = "/bin/bash"
            _rootfs_tar(archive, extra=[(link, None)])

            result = core.validate_rootfs(archive)

            self.assertEqual(result["version"], "24.04")
            self.assertEqual(result["architecture"], "aarch64")

    def test_rejects_non_aarch64_and_wrong_ubuntu(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong_arch = Path(tmp) / "wrong-arch.tbz2"
            wrong_os = Path(tmp) / "wrong-os.tbz2"
            _rootfs_tar(wrong_arch, machine=62)
            _rootfs_tar(wrong_os, ubuntu="22.04")

            with self.assertRaisesRegex(core.RecoveryError, "aarch64"):
                core.validate_rootfs(wrong_arch)
            with self.assertRaisesRegex(core.RecoveryError, "24.04"):
                core.validate_rootfs(wrong_os)

    def test_rejects_traversal_special_files_and_link_pivot(self):
        cases = []
        traversal = tarfile.TarInfo("../../etc/shadow")
        cases.append((traversal, b"x"))
        device = tarfile.TarInfo("dev/evil")
        device.type = tarfile.CHRTYPE
        cases.append((device, None))
        link = tarfile.TarInfo("usr")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/pivot"
        child = tarfile.TarInfo("usr/bin/evil")

        with tempfile.TemporaryDirectory() as tmp:
            for number, member in enumerate(cases):
                archive = Path(tmp) / f"unsafe-{number}.tbz2"
                _rootfs_tar(archive, extra=[member])
                with self.assertRaisesRegex(core.RecoveryError, "Небезопасный"):
                    core.validate_rootfs(archive)

            archive = Path(tmp) / "pivot.tbz2"
            _rootfs_tar(archive, extra=[(link, None), (child, b"x")])
            with self.assertRaisesRegex(core.RecoveryError, "symlink"):
                core.validate_rootfs(archive)

    def test_rejects_hardlink_outside_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "hardlink.tbz2"
            hardlink = tarfile.TarInfo("usr/bin/tool")
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = "../../outside"
            _rootfs_tar(archive, extra=[(hardlink, None)])
            with self.assertRaisesRegex(core.RecoveryError, "hardlink"):
                core.validate_rootfs(archive)

    def test_rejects_duplicate_normalized_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "duplicate.tbz2"
            duplicate = tarfile.TarInfo("./etc/os-release")
            with self.assertRaisesRegex(core.RecoveryError, "повтор"):
                _rootfs_tar(archive, extra=[(duplicate, b"changed")])
                core.validate_rootfs(archive)

    def test_normalizes_all_absolute_links_and_rejects_escape_or_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "usr/lib").mkdir(parents=True)
            (root / "opt/vendor").mkdir(parents=True)
            (root / "usr/lib/vendor").symlink_to("/opt/vendor")
            core._normalize_and_check_symlinks(root)
            rewritten = os.readlink(root / "usr/lib/vendor")
            self.assertFalse(rewritten.startswith("/"))
            self.assertTrue(core._inside(root, root / "usr/lib/vendor"))

            (root / "bad").symlink_to("../../outside")
            with self.assertRaisesRegex(core.RecoveryError, "предел"):
                core._normalize_and_check_symlinks(root)
            (root / "bad").unlink()
            (root / "a").symlink_to("b")
            (root / "b").symlink_to("a")
            with self.assertRaisesRegex(core.RecoveryError, "цикл"):
                core._normalize_and_check_symlinks(root)


class CommandTests(unittest.TestCase):
    def test_root_environment_uses_effective_root_not_user_supplied_name(self):
        with mock.patch.object(core.os, "geteuid", return_value=0):
            env = core.sanitized_env({"USER": "desktop-user", "HOME": "/home/user"})
        self.assertEqual(env["USER"], "root")
        self.assertEqual(env["LOGNAME"], "root")
        self.assertEqual(env["HOME"], core.pwd.getpwuid(0).pw_dir)

    def test_initrd_network_blocks_disabled_ipv6_and_closed_active_ufw(self):
        with mock.patch.object(core.Path, "read_text", return_value="1\n"):
            with self.assertRaisesRegex(core.RecoveryError, "IPv6"):
                core.require_initrd_network()
        with mock.patch.object(core.Path, "read_text", return_value="0\n"), \
             mock.patch.object(core.shutil, "which", return_value="/usr/sbin/ufw"), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout="Status: active\n", stderr="")) as run:
            with self.assertRaisesRegex(core.RecoveryError, "UFW"):
                core.require_initrd_network()
            run.return_value.stdout = "Status: active\n2049 ALLOW IN Anywhere\n"
            core.require_initrd_network()
            run.return_value.stdout = "Status: inactive\n"
            core.require_initrd_network()

    def test_dependencies_add_tools_missing_from_nvidia_prerequisites(self):
        calls = []
        with mock.patch.object(core, "check_runtime", return_value=[]):
            core.install_host_dependencies(Path("/vendor"), lambda argv, cwd: calls.append((argv, cwd)))
        self.assertIn("l4t_flash_prerequisites.sh", calls[0][0][-1])
        self.assertEqual(calls[1][0][-4:], ["bzip2", "xz-utils", "fdisk", "kmod"])
        self.assertEqual(calls[2][0], ["python3", "-c", "import yaml; import usb.core"])
        with mock.patch.object(core, "check_runtime", return_value=["bzip2"]):
            with self.assertRaisesRegex(core.RecoveryError, "bzip2"):
                core.install_host_dependencies(Path("/vendor"), lambda *_: None)

    def test_commands_are_literal_argv_and_never_spoof_identity(self):
        l4t = Path("/safe/Linux_for_Tegra")
        expected = {
            "emmc": ["./flash.sh", "geacx1-32gb-jp72", "mmcblk0p1"],
            "qspi": ["./flash.sh", "geacx1-32gb-jp72-qspi", "internal"],
            "nvme": [
                "./tools/kernel_flash/l4t_initrd_flash.sh",
                "-c", "tools/kernel_flash/flash_l4t_external.xml",
                "-p", "-c bootloader/generic/cfg/flash_t234_qspi.xml --no-systemimg",
                "--network", "usb0", "--showlogs",
                "--external-device", "nvme0n1p1",
                "geacx1-32gb-jp72", "external",
            ],
        }
        for mode, want in expected.items():
            self.assertEqual(core.build_command(l4t, mode), want)
        flattened = " ".join(sum(expected.values(), []))
        self.assertNotIn("BOARDID", flattened)
        self.assertNotIn("BOARDSKU", flattened)
        with self.assertRaises(core.RecoveryError):
            core.build_command(l4t, "usb")

    def test_sanitized_env_drops_all_flash_identity_overrides(self):
        env = {
            "PATH": "/usr/bin", "BOARDID": "3701", "BOARDSKU": "0004",
            "FAB": "x", "BOARDREV": "x", "FUSELEVEL": "x", "CHIPREV": "x",
            "CHIP_SKU": "x", "RAMCODE": "x", "SKIPUID": "x",
            "ROOTFS_AB": "1", "SKIP_EEPROM_CHECK": "1", "LD_PRELOAD": "/evil",
            "BASH_ENV": "/evil", "ENV": "/evil",
        }
        with mock.patch.object(core.os, "geteuid", return_value=1000):
            self.assertEqual(
                core.sanitized_env(env),
                {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            )


class BundleTests(unittest.TestCase):
    def test_module_dependencies_detect_case_loss_and_missing_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp)
            (modules / "kernel").mkdir()
            (modules / "kernel/xt_RATEEST.ko").write_bytes(b"upper")
            (modules / "modules.dep").write_text(
                "kernel/xt_RATEEST.ko:\nkernel/xt_rateest.ko: kernel/xt_RATEEST.ko\n"
            )
            self.assertEqual(core.missing_module_dependencies(modules), ["kernel/xt_rateest.ko"])
            (modules / "modules.dep").write_text("kernel/xt_RATEEST.ko: kernel/absent.ko\n")
            self.assertEqual(core.missing_module_dependencies(modules), ["kernel/absent.ko"])

    def _vendor(self, base: Path, release="R39 (release), REVISION: 2.0") -> Path:
        vendor = base / "vendor"
        l4t = vendor / "Linux_for_Tegra"
        board = vendor / core.BOARD_DIR
        kernel = vendor / core.KERNEL_DIR
        for relative in core.REQUIRED_L4T:
            target = l4t / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(release if relative == "nv_tegra/nv_tegra_release" else "x")
        for relative in core._MARKER_FILES:
            if relative.startswith("tools/") and not (l4t / relative).exists():
                (l4t / relative).parent.mkdir(parents=True, exist_ok=True)
                (l4t / relative).write_text("tool")
        network = Path(__file__).parents[1] / "vendor" / core.VENDOR_NAME / "Linux_for_Tegra/tools/kernel_flash/l4t_network_flash.func"
        (l4t / "tools/kernel_flash/l4t_network_flash.func").write_bytes(network.read_bytes())
        (l4t / "rootfs").mkdir()
        (l4t / "bootloader").mkdir(exist_ok=True)
        (l4t / "bootloader/extlinux.conf").write_text(
            "TIMEOUT 30\nDEFAULT primary\nLABEL primary\n"
            " LINUX /boot/Image\n INITRD /boot/initrd\n APPEND ${cbootargs}\n"
        )
        for relative in core.REQUIRED_BOARD:
            target = board / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"board")
        for relative in core.REQUIRED_KERNEL:
            target = kernel / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"kernel")
        modules = kernel / "modules/6.8.12-1021-tegra/modules.dep"
        modules.parent.mkdir(parents=True)
        modules.write_text("nvgpu.ko:\n")
        (modules.parent / "nvgpu.ko").write_bytes(b"module")
        (modules.parent / "build").symlink_to('/home/vendor/kernel-build')
        fan = vendor / core.FAN_PACKAGE
        fan.parent.mkdir(parents=True, exist_ok=True)
        fan.write_bytes(b"fan-package")
        return vendor

    def test_inspect_bundle_reports_version_and_empty_rootfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = core.inspect_bundle(self._vendor(Path(tmp)))
            self.assertEqual(report["release"], "R39.2.0")
            self.assertTrue(report["rootfs_empty"])
            self.assertTrue(any("пуст" in item.lower() for item in report["messages"]))

    def test_inspect_bundle_rejects_bad_release_and_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = self._vendor(Path(tmp), "R38 (release), REVISION: 4.0")
            with self.assertRaisesRegex(core.RecoveryError, "R39.2.0"):
                core.inspect_bundle(vendor)
            (vendor / "Linux_for_Tegra/nv_tegra/nv_tegra_release").write_text(
                "R39 (release), REVISION: 2.0"
            )
            (vendor / core.KERNEL_DIR / "boot/Image").unlink()
            with self.assertRaisesRegex(core.RecoveryError, "boot/Image"):
                core.inspect_bundle(vendor)

    def test_prepare_uses_fresh_workspace_order_and_installs_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vendor = self._vendor(base)
            rootfs = base / "rootfs.tbz2"
            _rootfs_tar(rootfs)
            original_rootfs = rootfs.read_bytes()
            work = base / "new-work"
            calls = []

            def run(argv, cwd):
                calls.append((argv, cwd))
                if argv[0] == "tar":
                    snapshot = Path(argv[3])
                    self.assertNotEqual(snapshot, rootfs)
                    self.assertEqual(snapshot.read_bytes(), original_rootfs)
                    self.assertEqual(snapshot.stat().st_mode & 0o777, 0o400)
                    self.assertEqual(work.stat().st_mode & 0o777, 0o700)
                    root = cwd / "rootfs"
                    (root / "etc").mkdir(parents=True, exist_ok=True)
                    (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
                    (root / "bin").mkdir(parents=True, exist_ok=True)
                    (root / "bin/bash").write_bytes(_elf_aarch64())
                    (root / "usr/lib").mkdir(parents=True, exist_ok=True)
                    (root / "lib").symlink_to("usr/lib")
                elif argv[0] == "./apply_binaries.sh":
                    # Actual R39.2 dpkg installs the RT package and changes DEFAULT.
                    config = cwd / "rootfs/boot/extlinux/extlinux.conf"
                    config.parent.mkdir(parents=True, exist_ok=True)
                    config.write_text(
                        "DEFAULT real-time\nLABEL primary\n LINUX /boot/Image\n INITRD /boot/initrd\n"
                        "LABEL real-time\n LINUX /boot/Image.real-time\n INITRD /boot/initrd\n"
                    )
                    (cwd / "rootfs/boot/Image.real-time").write_bytes(b"incompatible-rt-kernel")
                elif argv[0] == "dpkg-deb":
                    root = Path(argv[-1])
                    files = {
                        "lib/systemd/system/rb-jetson-service-fan.service": b"service",
                        "etc/rb/rb-jetson-service-fan/config.json": b"{}",
                        "opt/rb/rb-jetson-service-fan/fanctl.py": b"#!/usr/bin/python3\n",
                        "opt/rb/rb-jetson-service-fan/fan_gpio_ctrl.sh": b"#!/bin/bash\n",
                        "opt/rb/rb-jetson-service-fan/md5sums": b"hashes",
                    }
                    for relative, payload in files.items():
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(payload)

            fan_hash = hashlib.sha256(b"fan-package").hexdigest()
            def progress(message):
                if message == "Создание чистой рабочей копии BSP…":
                    replacement = base / "replacement.tbz2"
                    _rootfs_tar(replacement, ubuntu="22.04")
                    replacement.replace(rootfs)

            with mock.patch.object(core.os, "geteuid", return_value=0), \
                 mock.patch.object(core, "ROOTFS_SHA256", hashlib.sha256(rootfs.read_bytes()).hexdigest()), \
                 mock.patch.object(core, "FAN_PACKAGE_SHA256", fan_hash), \
                 mock.patch.object(core.backup_restore, "harden_prepared_tools") as harden:
                l4t = core.prepare(vendor, rootfs, work, run, progress)
                harden.assert_called_once_with(l4t)

            self.assertEqual([c[0][0] for c in calls], ["tar", "./apply_binaries.sh", "dpkg-deb", "./tools/l4t_update_initrd.sh"])
            self.assertEqual(calls[0][0][:3], ["tar", "--numeric-owner", "-xpf"])
            self.assertEqual((l4t / "kernel/Image").read_bytes(), b"kernel")
            self.assertTrue((l4t / "rootfs/lib/modules/6.8.12-1021-tegra/modules.dep").is_file())
            build_link = l4t / "rootfs/lib/modules/6.8.12-1021-tegra/build"
            self.assertFalse(os.path.isabs(os.readlink(build_link)))
            self.assertTrue(core._inside(l4t / "rootfs", build_link))
            guard = (l4t / "geacx1-32gb-jp72.conf").read_text()
            self.assertIn('${board_id}" != "3701', guard)
            self.assertIn('${board_sku}" != "0004', guard)
            self.assertNotIn("BOARDID=", guard)
            qspi = (l4t / "geacx1-32gb-jp72-qspi.conf").read_text()
            self.assertIn('source "${LDK_DIR}/geacx1-32gb-jp72.conf"', qspi)
            self.assertIn('EMMC_CFG="flash_t234_qspi.xml"', qspi)
            self.assertIn("NO_ROOTFS=1", qspi)
            fan_service = l4t / "rootfs/etc/systemd/system/multi-user.target.wants/rb-jetson-service-fan.service"
            self.assertTrue(fan_service.is_symlink())
            self.assertTrue((l4t / "rootfs/lib").is_symlink())
            self.assertTrue((l4t / "rootfs/opt/rb/rb-jetson-service-fan/fanctl.py").stat().st_mode & 0o111)
            helper = (l4t / "rootfs/opt/rb/rb-jetson-service-fan/fan_gpio_ctrl.sh").read_text()
            self.assertNotIn("sudo", helper)
            self.assertNotIn("chmod", helper)
            self.assertIn('0|1', helper)
            marker = core.validate_prepared(l4t)
            self.assertEqual(marker["release"], "R39.2.0")
            extlinux = l4t / "rootfs/boot/extlinux/extlinux.conf"
            boot_config = extlinux.read_text()
            self.assertIn("DEFAULT primary", boot_config)
            self.assertNotIn("Image.real-time", boot_config)
            self.assertFalse((l4t / "rootfs/boot/Image.real-time").exists())
            # flash.sh appends cmdline/FDT while creating the image; reuse must work.
            extlinux.write_text(boot_config.replace("APPEND ${cbootargs}",
                                  "APPEND ${cbootargs} root=/dev/mmcblk0p1\n FDT /boot/dtb/vendor.dtb"))
            core.validate_prepared(l4t)
            valid_config = extlinux.read_text()
            for bad_config in (
                valid_config.replace("DEFAULT primary", "DEFAULT real-time"),
                valid_config.replace("LINUX /boot/Image", "LINUX /boot/Image.real-time"),
                valid_config.replace("INITRD /boot/initrd", "INITRD /boot/other-initrd"),
                valid_config + "\nDEFAULT primary\n",
                valid_config + "\nLABEL primary\n LINUX /boot/Image\n INITRD /boot/initrd\n",
                valid_config + "\nLABEL real-time\n LINUX /boot/Image.real-time\n",
            ):
                extlinux.write_text(bad_config)
                with self.assertRaisesRegex(core.RecoveryError, "extlinux"):
                    core.validate_prepared(l4t)
            extlinux.write_text(valid_config)
            initrds = [l4t / "bootloader/l4t_initrd.img", l4t / "rootfs/boot/initrd"]
            originals = [p.read_bytes() for p in initrds]
            for failure in (None, RuntimeError("vendor failed"), KeyboardInterrupt()):
                try:
                    with core.preserve_prepared_initrd(l4t):
                        for p in initrds:
                            p.write_bytes(b"legitimately-rebuilt-by-nvidia")
                        if failure is not None:
                            raise failure
                except (RuntimeError, KeyboardInterrupt) as exc:
                    self.assertIs(exc, failure)
                self.assertEqual([p.read_bytes() for p in initrds], originals)
                self.assertEqual(list(l4t.parent.glob('.geacx1-initrd-*')), [])
                core.validate_prepared(l4t)
            fan_target = os.readlink(fan_service)
            fan_service.unlink()
            with self.assertRaisesRegex(core.RecoveryError, "вентилятор"):
                core.validate_prepared(l4t)
            fan_service.symlink_to(fan_target)

            module = l4t / "rootfs/lib/modules/6.8.12-1021-tegra/nvgpu.ko"
            module.write_bytes(b"corrupted")
            with self.assertRaisesRegex(core.RecoveryError, "nvgpu.ko"):
                core.validate_prepared(l4t)
            module.write_bytes(b"module")

            image = (l4t / "kernel/Image").read_bytes()
            (l4t / "kernel/Image").write_bytes(b"tampered")
            with self.assertRaisesRegex(core.RecoveryError, "Контрольная сумма"):
                core.validate_prepared(l4t)
            (l4t / "kernel/Image").write_bytes(image)
            (l4t / "p3701.conf.common").write_text("tampered")
            with self.assertRaisesRegex(core.RecoveryError, "p3701.conf.common"):
                core.validate_prepared(l4t)
            (l4t / "p3701.conf.common").write_bytes(b"board")
            layout = l4t / "bootloader/generic/cfg/flash_t234_qspi.xml"
            layout.write_text("tampered")
            with self.assertRaisesRegex(core.RecoveryError, "flash_t234_qspi.xml"):
                core.validate_prepared(l4t)

    def test_prepare_refuses_non_root_and_existing_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vendor = self._vendor(base)
            rootfs = base / "rootfs.tbz2"
            _rootfs_tar(rootfs)
            with mock.patch.object(core.os, "geteuid", return_value=1000):
                with self.assertRaisesRegex(core.RecoveryError, "root"):
                    core.prepare(vendor, rootfs, base / "new", lambda *_: None, lambda _: None)
            existing = base / "existing"
            existing.mkdir()
            with mock.patch.object(core.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(core.RecoveryError, "нов"):
                    core.prepare(vendor, rootfs, existing, lambda *_: None, lambda _: None)

    def test_prepare_rejects_nonempty_vendor_rootfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vendor = self._vendor(base)
            (vendor / "Linux_for_Tegra/rootfs/stale-system.img").write_bytes(b"stale")
            rootfs = base / "rootfs.tbz2"
            _rootfs_tar(rootfs)
            with mock.patch.object(core.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(core.RecoveryError, "пуст"):
                    core.prepare(vendor, rootfs, base / "new", lambda *_: None, lambda _: None)


class ManifestTests(unittest.TestCase):
    def test_manifest_verifies_and_rejects_mismatch_or_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            payload = bundle / "payload.bin"
            payload.write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            (bundle / "SHA256SUMS").write_text(f"{digest}  payload.bin\n")
            self.assertEqual(core.verify_manifest(bundle)[0]["ok"], True)
            payload.write_bytes(b"changed")
            with self.assertRaisesRegex(core.RecoveryError, "SHA-256"):
                core.verify_manifest(bundle)
            (bundle / "SHA256SUMS").write_text(f"{digest}  ../outside\n")
            with self.assertRaisesRegex(core.RecoveryError, "путь"):
                core.verify_manifest(bundle)


class DownloadTests(unittest.TestCase):
    class Response:
        def __init__(self, payload, *, status, headers, fail_after=False, url=core.ROOTFS_URL):
            self.payload = payload
            self.status = status
            self.headers = headers
            self.fail_after = fail_after
            self.url = url
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def getcode(self):
            return self.status

        def geturl(self):
            return self.url

        def read(self, _):
            if not self.sent:
                self.sent = True
                return self.payload
            if self.fail_after:
                self.fail_after = False
                raise OSError("reset")
            return b""

        def read1(self, size):
            return self.read(size)

    def test_slow_download_stops_without_publishing_or_retrying(self):
        # A resumed file must not be counted as bytes downloaded in this session.
        for offset in (0, 90000):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as tmp:
                clock = [0.0]
                response = self.Response(b'x', status=206 if offset else 200,
                    headers={'Content-Length': str(100000-offset),
                             'Content-Range': f'bytes {offset}-99999/100000'})
                read = response.read
                def slow_read(size):
                    clock[0] += 31
                    return read(size)
                response.read1 = slow_read
                response.read = slow_read
                dest = Path(tmp) / core.ROOTFS_NAME
                part = dest.with_name(dest.name + '.part')
                part.write_bytes(b'p' * offset)
                with mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
                     mock.patch.object(core.time, 'sleep'), \
                     mock.patch.object(core.urllib.request, 'urlopen', return_value=response) as request:
                    with self.assertRaisesRegex(core.RecoveryError, '2 часа'):
                        core.download_rootfs(dest, lambda _: None)
                self.assertFalse(dest.exists())
                self.assertEqual(part.stat().st_size, offset + 1)
                self.assertEqual(request.call_count, 1)

    def test_download_deadline_includes_elapsed_transfer_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = [0.0]
            response = self.Response(b'x', status=200, headers={'Content-Length': '1'})
            read = response.read
            def late_read(size):
                clock[0] = 7201
                return read(size)
            response.read1 = late_read
            response.read = late_read
            dest = Path(tmp) / core.ROOTFS_NAME
            with mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
                 mock.patch.object(core.urllib.request, 'urlopen', return_value=response):
                with self.assertRaisesRegex(core.RecoveryError, '2 часа'):
                    core.download_rootfs(dest, lambda _: None)
            self.assertFalse(dest.exists())

    def test_range_ignored_after_retry_does_not_inflate_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = [0.0]
            first = self.Response(b'a' * 90000, status=200,
                                  headers={'Content-Length': '100000'}, fail_after=True)
            second = self.Response(b'x', status=200, headers={'Content-Length': '100000'})
            first_read, second_read = first.read, second.read
            def quick_read(size):
                clock[0] += 1
                return first_read(size)
            def slow_read(size):
                clock[0] += 31
                return second_read(size)
            first.read1, second.read1 = quick_read, slow_read
            responses = [first, second]
            def request(_request, timeout):
                return responses.pop(0) if responses else self.Response(
                    b'', status=200, headers={'Content-Length': '0'})
            dest = Path(tmp) / core.ROOTFS_NAME
            with mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
                 mock.patch.object(core.time, 'sleep'), \
                 mock.patch.object(core.urllib.request, 'urlopen', side_effect=request) as fetch:
                with self.assertRaisesRegex(core.RecoveryError, '2 часа'):
                    core.download_rootfs(dest, lambda _: None)
            self.assertFalse(dest.exists())
            self.assertEqual(dest.with_name(dest.name + '.part').read_bytes(), b'x')
            self.assertEqual(fetch.call_count, 2)

    def test_download_resumes_only_with_matching_content_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture.tbz2"
            _rootfs_tar(fixture)
            data = fixture.read_bytes()
            split = len(data) // 2
            responses = [
                self.Response(data[:split], status=200, headers={"Content-Length": str(len(data))}, fail_after=True),
                self.Response(
                    data[split:], status=206,
                    headers={"Content-Length": str(len(data) - split), "Content-Range": f"bytes {split}-{len(data)-1}/{len(data)}"},
                ),
            ]

            def urlopen(request, timeout):
                self.assertEqual(timeout, 60)
                if len(responses) == 1:
                    self.assertEqual(request.headers.get("Range"), f"bytes={split}-")
                return responses.pop(0)

            dest = Path(tmp) / core.ROOTFS_NAME
            with mock.patch.object(core.urllib.request, "urlopen", side_effect=urlopen), \
                 mock.patch.object(core, "ROOTFS_SHA256", hashlib.sha256(data).hexdigest()), \
                 mock.patch.object(core.time, "sleep"):
                self.assertEqual(core.download_rootfs(dest, lambda _: None), dest)
            self.assertEqual(dest.read_bytes(), data)
            self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_download_rejects_insecure_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = self.Response(b"x", status=200, headers={"Content-Length": "1"}, url="http://example.test/file")
            with mock.patch.object(core.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(core.RecoveryError, "HTTPS"):
                    core.download_rootfs(Path(tmp) / core.ROOTFS_NAME, lambda _: None)


class UsbDetectionTests(unittest.TestCase):
    def test_pid_7023_is_supported_but_does_not_claim_32gb_sku(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp)
            device = sysfs / "1-1"
            device.mkdir()
            (device / "idVendor").write_text("0955\n")
            (device / "idProduct").write_text("7023\n")
            (device / "product").write_text("APX\n")
            with mock.patch.object(core, "SYS_USB_DEVICES", sysfs):
                found = core.recovery_devices()
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0]["supported"])
            self.assertNotIn("32", found[0]["family"])
            self.assertIn("SKU", found[0]["detail"])


if __name__ == "__main__":
    unittest.main()
