import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import generated_cleanup


class GeneratedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.l4t = Path(self.tmp.name).resolve() / "Linux_for_Tegra"
        self.l4t.mkdir()
        (self.l4t / ".geacx1-prepared.json").write_text("{}\n")

    def make_file(self, relative, content=b"generated"):
        path = self.l4t / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_plan_contains_only_known_raw_rootfs_images_and_allocated_size(self):
        boot = self.make_file("bootloader/system.img.raw")
        internal = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"i")
        external = self.make_file("tools/kernel_flash/images/external/system.img.raw", b"e")
        with boot.open("wb") as stream:
            stream.seek(16 * 1024 * 1024)
            stream.write(b"x")

        preserved = [
            self.make_file("bootloader/system.img", b"reusable"),
            self.make_file("bootloader/signed/keep.bin", b"signed"),
            self.make_file("tools/kernel_flash/images/internal/kernel.img", b"kernel"),
            self.make_file("rootfs/var/log/geacx1.log", b"log"),
        ]

        plan = generated_cleanup.plan_cleanup(self.l4t)

        self.assertEqual(plan["l4t"], str(self.l4t.absolute()))
        self.assertEqual(
            [entry["path"] for entry in plan["entries"]],
            [
                "bootloader/system.img.raw",
                "tools/kernel_flash/images/internal/system.img.raw",
                "tools/kernel_flash/images/external/system.img.raw",
            ],
        )
        stats = {str(path.relative_to(self.l4t)): path.stat() for path in (boot, internal, external)}
        for entry in plan["entries"]:
            stat = stats[entry["path"]]
            self.assertEqual(entry["size"], stat.st_size)
            self.assertEqual(entry["allocated_bytes"], stat.st_blocks * 512)
            self.assertEqual(entry["inode"], stat.st_ino)
            self.assertEqual(entry["device"], stat.st_dev)
            self.assertEqual(entry["mtime_ns"], stat.st_mtime_ns)
        self.assertEqual(
            plan["allocated_bytes"], sum(path.stat().st_blocks * 512 for path in (boot, internal, external))
        )
        self.assertLess(next(e for e in plan["entries"] if e["path"] == "bootloader/system.img.raw")["allocated_bytes"],
                        boot.stat().st_size)
        self.assertTrue(all(path.exists() for path in preserved))

    def test_execute_deletes_planned_files_and_returns_actual_result(self):
        raw = self.make_file("bootloader/system.img.raw", b"raw")
        reusable = self.make_file("bootloader/system.img", b"reusable")
        plan = generated_cleanup.plan_cleanup(self.l4t)

        result = generated_cleanup.execute_cleanup(self.l4t, plan)

        self.assertFalse(raw.exists())
        self.assertTrue(reusable.exists())
        self.assertEqual(result["l4t"], plan["l4t"])
        self.assertEqual(result["allocated_bytes"], plan["allocated_bytes"])
        self.assertEqual(result["entries"], [{**plan["entries"][0], "deleted": True}])

    def test_protected_exact_allowed_path_is_displayed_and_preserved(self):
        required_for_nvme = self.make_file("bootloader/system.img.raw", b"nvme-cache")
        disposable = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"generated")

        plan = generated_cleanup.plan_cleanup(
            self.l4t, protected_paths=("bootloader/system.img.raw",)
        )

        self.assertEqual(plan["protected_paths"], ["bootloader/system.img.raw"])
        self.assertEqual(
            [entry["path"] for entry in plan["entries"]],
            ["tools/kernel_flash/images/internal/system.img.raw"],
        )
        result = generated_cleanup.execute_cleanup(self.l4t, plan)
        self.assertEqual(result["protected_paths"], ["bootloader/system.img.raw"])
        self.assertTrue(required_for_nvme.exists())
        self.assertFalse(disposable.exists())

    def test_protected_paths_accept_only_exact_allowlist_members(self):
        raw = self.make_file("bootloader/system.img.raw")
        for protected in ("bootloader", "bootloader/../rootfs", "/bootloader/system.img.raw"):
            with self.subTest(protected=protected):
                with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                    generated_cleanup.plan_cleanup(self.l4t, protected_paths=(protected,))
        self.assertTrue(raw.exists())

    def test_plan_cannot_delete_a_path_listed_as_protected(self):
        raw = self.make_file("bootloader/system.img.raw")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        plan["protected_paths"] = ["bootloader/system.img.raw"]

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.execute_cleanup(self.l4t, plan)
        self.assertTrue(raw.exists())

    def test_marker_is_required_and_must_not_be_a_symlink(self):
        marker = self.l4t / ".geacx1-prepared.json"
        marker.unlink()
        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)

        real_marker = Path(self.tmp.name) / "marker.json"
        real_marker.write_text("{}\n")
        marker.symlink_to(real_marker)
        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)

    def test_symlink_target_or_parent_is_refused_without_touching_outside_file(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        outside_raw = outside / "system.img.raw"
        outside_raw.write_bytes(b"outside")
        (self.l4t / "bootloader").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)
        self.assertEqual(outside_raw.read_bytes(), b"outside")

        (self.l4t / "bootloader").unlink()
        (self.l4t / "bootloader").mkdir()
        (self.l4t / "bootloader/system.img.raw").symlink_to(outside_raw)
        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)
        self.assertEqual(outside_raw.read_bytes(), b"outside")

    def test_symlink_above_l4t_is_refused(self):
        actual_parent = Path(self.tmp.name).resolve() / "actual"
        other_l4t = actual_parent / "Linux_for_Tegra"
        other_l4t.mkdir(parents=True)
        (other_l4t / ".geacx1-prepared.json").write_text("{}\n")
        alias = Path(self.tmp.name).resolve() / "alias"
        alias.symlink_to(actual_parent, target_is_directory=True)

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(alias / "Linux_for_Tegra")

    def test_directory_at_allowed_file_path_is_refused(self):
        raw = self.l4t / "bootloader/system.img.raw"
        raw.mkdir(parents=True)

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)
        self.assertTrue(raw.is_dir())

    def test_mountpoint_at_target_is_refused(self):
        raw = self.make_file("bootloader/system.img.raw")
        mountinfo = Path(self.tmp.name) / "mountinfo"
        escaped = str(raw).replace(" ", "\\040")
        mountinfo.write_text(f"42 31 8:1 / {escaped} rw,relatime - ext4 /dev/sda1 rw\n")

        with patch.object(generated_cleanup, "MOUNTINFO", mountinfo):
            with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                generated_cleanup.plan_cleanup(self.l4t)
        self.assertTrue(raw.exists())

    def test_mountpoint_at_parent_inside_l4t_is_refused(self):
        raw = self.make_file("tools/kernel_flash/images/internal/system.img.raw")
        mountinfo = Path(self.tmp.name) / "mountinfo-parent"
        mounted_parent = raw.parent
        mountinfo.write_text(
            f"42 31 8:1 / {mounted_parent} rw,relatime - ext4 /dev/sda1 rw\n"
        )

        with patch.object(generated_cleanup, "MOUNTINFO", mountinfo):
            with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                generated_cleanup.plan_cleanup(self.l4t)
        self.assertTrue(raw.exists())

    def test_loop_backing_file_is_refused_with_kernel_escaped_path(self):
        spaced_l4t = Path(self.tmp.name).resolve() / "space parent" / "Linux_for_Tegra"
        spaced_l4t.mkdir(parents=True)
        (spaced_l4t / ".geacx1-prepared.json").write_text("{}\n")
        raw = spaced_l4t / "bootloader/system.img.raw"
        raw.parent.mkdir()
        raw.write_bytes(b"loop backing")
        loop_root = Path(self.tmp.name).resolve() / "sys-block"
        backing = loop_root / "loop7/loop/backing_file"
        backing.parent.mkdir(parents=True)
        backing.write_text(str(raw).replace(" ", "\\040") + "\n")

        with patch.object(generated_cleanup, "LOOP_SYSFS", loop_root):
            with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                generated_cleanup.plan_cleanup(spaced_l4t)
        self.assertTrue(raw.exists())

    def test_hardlinked_raw_is_refused(self):
        raw = self.make_file("bootloader/system.img.raw")
        hardlink = Path(self.tmp.name).resolve() / "same-raw"
        os.link(raw, hardlink)

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.plan_cleanup(self.l4t)
        self.assertTrue(raw.exists())
        self.assertEqual(hardlink.read_bytes(), b"generated")

    def test_new_parent_mount_aborts_before_any_file_is_deleted(self):
        first = self.make_file("bootloader/system.img.raw", b"first")
        second = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"second")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        mountinfo = Path(self.tmp.name) / "mountinfo-new-parent"
        mountinfo.write_text(
            f"42 31 8:1 / {second.parent} rw,relatime - ext4 /dev/sda1 rw\n"
        )

        with patch.object(generated_cleanup, "MOUNTINFO", mountinfo):
            with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                generated_cleanup.execute_cleanup(self.l4t, plan)
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")

    def test_new_loop_use_aborts_before_any_file_is_deleted(self):
        first = self.make_file("bootloader/system.img.raw", b"first")
        second = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"second")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        loop_root = Path(self.tmp.name).resolve() / "new-sys-block"
        backing = loop_root / "loop2/loop/backing_file"
        backing.parent.mkdir(parents=True)
        backing.write_text(str(second) + "\n")

        with patch.object(generated_cleanup, "LOOP_SYSFS", loop_root):
            with self.assertRaises(generated_cleanup.GeneratedCleanupError):
                generated_cleanup.execute_cleanup(self.l4t, plan)
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")

    def test_changed_entry_aborts_before_any_file_is_deleted(self):
        first = self.make_file("bootloader/system.img.raw", b"first")
        changed = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"before")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        changed.write_bytes(b"changed-size")

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.execute_cleanup(self.l4t, plan)

        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(changed.read_bytes(), b"changed-size")

    def test_replaced_entry_type_aborts_before_any_file_is_deleted(self):
        first = self.make_file("bootloader/system.img.raw", b"first")
        replaced = self.make_file("tools/kernel_flash/images/internal/system.img.raw", b"before")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        replaced.unlink()
        replaced.mkdir()

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.execute_cleanup(self.l4t, plan)

        self.assertTrue(first.exists())
        self.assertTrue(replaced.is_dir())

    def test_injected_path_is_rejected_without_deleting_anything(self):
        raw = self.make_file("bootloader/system.img.raw")
        foreign = self.make_file("bootloader/keep.log", b"keep")
        plan = generated_cleanup.plan_cleanup(self.l4t)
        plan["entries"].append({**plan["entries"][0], "path": "bootloader/keep.log"})

        with self.assertRaises(generated_cleanup.GeneratedCleanupError):
            generated_cleanup.execute_cleanup(self.l4t, plan)

        self.assertTrue(raw.exists())
        self.assertEqual(foreign.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
