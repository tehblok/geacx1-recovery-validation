import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
P = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P / "lib"))
import backup_restore as br


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        successful = type("Result", (), {"returncode": 0})()
        self.compression_check = patch.object(br.subprocess, "run", return_value=successful)
        self.compression_check.start()
        self.addCleanup(self.compression_check.stop)
        self.raw_size_check = patch.object(br, '_zstd_decompressed_size', return_value=8 * 512)
        self.raw_size_check.start()
        self.addCleanup(self.raw_size_check.stop)

    def fake_primary_gpt(self, partition_count=1, backup_lba=1032):
        data = bytearray(34 * 512)
        data[512:520] = b"EFI PART"
        data[584:592] = (2).to_bytes(8, "little")
        data[592:596] = (128).to_bytes(4, "little")
        data[596:600] = (128).to_bytes(4, "little")
        data[544:552] = backup_lba.to_bytes(8, "little")
        for index in range(partition_count):
            offset = 1024 + index * 128
            data[offset:offset + 16] = bytes([index + 1]) * 16
            start = 34 + index * 16
            data[offset + 32:offset + 40] = start.to_bytes(8, "little")
            data[offset + 40:offset + 48] = (start + 7).to_bytes(8, "little")
        return bytes(data)

    def make_l4t(self, root):
        l4t = Path(root) / "Linux_for_Tegra"
        for name in (
            "tools/backup_restore/l4t_backup_restore.sh",
            "tools/backup_restore/l4t_backup_restore.func",
            "tools/backup_restore/nvbackup_partitions.sh",
            "tools/backup_restore/nvrestore_partitions.sh",
            "tools/kernel_flash/l4t_initrd_flash.sh",
        ):
            path = l4t / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stub")
        return l4t

    def make_images(self, root, nvme=False):
        images = Path(root)
        images.mkdir(parents=True, exist_ok=True)
        # Exact six-column nvpartitionmap format emitted by nvbackup_partitions.sh:
        # filename,partition,start,size,flags,sha256.
        payloads = {
            "mmcblk0_gptmbr.img": (self.fake_primary_gpt(), "gpt_1", "", 0, 34),
            "mmcblk0p1_bak.img": (b"partition", "mmcblk0", "", 34, 8),
            "mmcblk0_gptbackup.img": (b"g" * (33 * 512), "gpt_2", "", 1000, 33),
            "QSPI0.img": (b"qspi", "qspi0", "", 0, 4),
        }
        if nvme:
            payloads.update({
                "nvme0n1_gptmbr.img": (self.fake_primary_gpt(backup_lba=2032), "gpt_1", "", 0, 34),
                "nvme0n1p1_bak.img": (b"nvme-partition", "nvme0n1", "", 34, 8),
                "nvme0n1_gptbackup.img": (b"n" * (33 * 512), "gpt_2", "", 2000, 33),
            })
        rows = ["board_spec,3701-500-0004-A.0-production-00-geacx1-32gb-jp72-"]
        for index, (name, record) in enumerate(payloads.items(), 1):
            content, part, flags, start, size = record
            (images / name).write_bytes(content)
            rows.append(f"{name},{part},{start},{size},{flags},{hashlib.sha256(content).hexdigest()}")
        (images / br.MAP_NAME).write_text("\n".join(rows) + "\n")
        return images

    def test_official_argv_covers_emmc_nvme_and_qspi_is_implicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            l4t = self.make_l4t(tmp)
            self.assertEqual(br.build_command(l4t, "backup", "emmc_nvme"), [
                "./tools/backup_restore/l4t_backup_restore.sh", "-e", "mmcblk0:nvme0n1",
                "-b", br.BOARD_NAME,
            ])
            self.assertEqual(br.build_command(l4t, "restore", "emmc")[-2:], ["-r", br.BOARD_NAME])

    def test_export_and_verify_full_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / "images", nvme=True)
            target = root / "user-selected-backup"
            br.export_snapshot(images, target, "emmc_nvme")
            metadata = br.validate_snapshot(target)
            self.assertEqual(metadata["devices"], ["mmcblk0", "nvme0n1"])
            self.assertTrue(metadata["includes_qspi"])
            self.assertIn("eFuse", metadata["exclusions"])

    def test_digest_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "backup"
            br.export_snapshot(self.make_images(root / "images"), target, "emmc")
            (target / "QSPI0.img").write_bytes(b"tampered")
            with self.assertRaisesRegex(br.BackupError, "повреждён"):
                br.validate_snapshot(target)

    def test_missing_qspi_or_requested_nvme_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / "images")
            lines = (images / br.MAP_NAME).read_text().splitlines()
            (images / br.MAP_NAME).write_text("\n".join(line for line in lines if "QSPI0" not in line) + "\n")
            with self.assertRaisesRegex(br.BackupError, "QSPI0"):
                br.export_snapshot(images, root / "bad", "emmc")
            images = self.make_images(root / "images2")
            with self.assertRaisesRegex(br.BackupError, "nvme0n1"):
                br.export_snapshot(images, root / "bad2", "emmc_nvme")

    def test_wrong_family_and_extra_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "backup"
            br.export_snapshot(self.make_images(root / "images"), target, "emmc")
            manifest = json.loads((target / br.MANIFEST_NAME).read_text())
            manifest["family"] = "another-board"
            (target / br.MANIFEST_NAME).write_text(json.dumps(manifest))
            with self.assertRaisesRegex(br.BackupError, "family"):
                br.validate_snapshot(target)
            manifest["family"] = br.FAMILY
            (target / br.MANIFEST_NAME).write_text(json.dumps(manifest))
            (target / "unexpected.img").write_bytes(b"x")
            with self.assertRaisesRegex(br.BackupError, "[Сс]остав"):
                br.validate_snapshot(target)

    def test_isolation_restores_existing_images_even_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            l4t = self.make_l4t(tmp)
            original = l4t / "tools/backup_restore/images"
            original.mkdir()
            (original / "keep.txt").write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "fail"):
                with br.isolated_images(l4t) as staged:
                    self.assertFalse((staged / "keep.txt").exists())
                    (staged / "partial.img").write_bytes(b"partial")
                    raise RuntimeError("fail")
            self.assertEqual((original / "keep.txt").read_text(), "keep")
            self.assertFalse((original / "partial.img").exists())

    def test_restore_staging_verifies_copy_and_restores_old_images_on_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l4t = self.make_l4t(root / "work")
            original = l4t / "tools/backup_restore/images"
            original.mkdir()
            (original / "keep.txt").write_text("keep")
            snapshot = root / "snapshot"
            br.export_snapshot(self.make_images(root / "fresh"), snapshot, "emmc")
            with self.assertRaises(KeyboardInterrupt):
                with br.isolated_images(l4t, snapshot) as staged:
                    self.assertTrue((staged / "QSPI0.img").is_file())
                    raise KeyboardInterrupt
            self.assertEqual((original / "keep.txt").read_text(), "keep")
            self.assertFalse((original / "QSPI0.img").exists())

    def test_export_survives_transactional_staging_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l4t = self.make_l4t(root / 'work')
            snapshot = root / 'snapshot'
            with br.isolated_images(l4t) as staged:
                self.make_images(staged)
                br.export_snapshot(staged, snapshot, 'emmc')
            self.assertFalse((l4t / 'tools/backup_restore/images').exists())
            self.assertEqual(br.validate_snapshot(snapshot)['scope'], 'emmc')

    def test_gpt_partition_count_must_match_payload_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / "images")
            gpt = self.fake_primary_gpt(partition_count=2)
            (images / "mmcblk0_gptmbr.img").write_bytes(gpt)
            lines = (images / br.MAP_NAME).read_text().splitlines()
            digest = hashlib.sha256(gpt).hexdigest()
            lines = [line.rsplit(',', 1)[0] + ',' + digest if line.startswith('mmcblk0_gptmbr.img,') else line
                     for line in lines]
            (images / br.MAP_NAME).write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(br.BackupError, "описывает 2"):
                br.export_snapshot(images, root / "backup", "emmc")

    def test_forged_partition_target_and_extra_qspi_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / 'images')
            lines = (images / br.MAP_NAME).read_text().splitlines()
            lines = [line.replace('mmcblk0p1_bak.img,mmcblk0,', 'mmcblk0p1_bak.img,sda,')
                     for line in lines]
            (images / br.MAP_NAME).write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(br.BackupError, 'не в свой раздел'):
                br.export_snapshot(images, root / 'bad-target', 'emmc')

            images = self.make_images(root / 'images2')
            evil = b'evil'
            (images / 'evil.img').write_bytes(evil)
            with (images / br.MAP_NAME).open('a') as stream:
                stream.write(f"evil.img,qspi0,0,4,,{hashlib.sha256(evil).hexdigest()}\n")
            with self.assertRaisesRegex(br.BackupError, 'вне подтверждённого'):
                br.export_snapshot(images, root / 'bad-qspi', 'emmc')

    def test_wrong_secondary_gpt_seek_and_short_raw_stream_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / 'images')
            lines = (images / br.MAP_NAME).read_text().splitlines()
            lines = [line.replace('mmcblk0_gptbackup.img,gpt_2,1000,33,',
                                  'mmcblk0_gptbackup.img,gpt_2,0,33,') for line in lines]
            (images / br.MAP_NAME).write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(br.BackupError, 'поля GPT'):
                br.export_snapshot(images, root / 'bad-seek', 'emmc')

            images = self.make_images(root / 'images2')
            with patch.object(br, '_zstd_decompressed_size', return_value=8 * 512 - 1), \
                 self.assertRaisesRegex(br.BackupError, 'не в свой раздел'):
                br.export_snapshot(images, root / 'short-raw', 'emmc')

    def test_hardening_makes_both_tar_failures_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            l4t = self.make_l4t(tmp)
            source = (P / 'vendor/flashtool_jp7.2_GA_r1.0_20260722/Linux_for_Tegra/'
                      'tools/backup_restore/nvbackup_partitions.sh')
            target = l4t / 'tools/backup_restore/nvbackup_partitions.sh'
            target.write_bytes(source.read_bytes())
            restore_source = (P / 'vendor/flashtool_jp7.2_GA_r1.0_20260722/Linux_for_Tegra/'
                              'tools/backup_restore/nvrestore_partitions.sh')
            (l4t / 'tools/backup_restore/nvrestore_partitions.sh').write_bytes(restore_source.read_bytes())
            patched_hashes = br.harden_prepared_tools(l4t)
            text = target.read_text()
            self.assertEqual(text.count("if ! tar -I 'zstd -T0'"), 2)
            self.assertNotIn('set +e', text)
            self.assertIn('if ! dd if="$i" bs=64K iflag=fullblock', text)
            self.assertNotIn('noerror', text)
            self.assertEqual(patched_hashes['backup'], hashlib.sha256(target.read_bytes()).hexdigest())
            restore = (l4t / 'tools/backup_restore/nvrestore_partitions.sh').read_text()
            self.assertIn('set -eo pipefail', restore)
            self.assertIn('requested storage device', restore)
            self.assertIn('required QSPI device /dev/mtd0', restore)
            self.assertIn('Fail before blkdiscard', restore)
            self.assertIn('printf "%.0f\\n", $3 + $4', restore)
            self.assertNotIn('\n\t\t\t\texit\n', restore)

    def test_symlink_payload_is_rejected_before_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / "images")
            (images / "QSPI0.img").unlink()
            (images / "QSPI0.img").symlink_to("mmcblk0_gptmbr.img")
            with self.assertRaisesRegex(br.BackupError, "обычный файл"):
                br.export_snapshot(images, root / "backup", "emmc")

    def test_corrupt_compressed_partition_is_rejected_even_when_vendor_digest_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = self.make_images(root / "images")
            content = b"not-zstd"
            name = "mmcblk0p1.tar.zst"
            (images / name).write_bytes(content)
            with (images / br.MAP_NAME).open("a") as stream:
                stream.write(f"{name},mmcblk0p1,1,1,tz,{hashlib.sha256(content).hexdigest()}\n")
            failed = type("Result", (), {"returncode": 1})()
            with patch.object(br.subprocess, "run", return_value=failed), \
                 self.assertRaisesRegex(br.BackupError, "Сжатый образ"):
                br.export_snapshot(images, root / "backup", "emmc")


if __name__ == "__main__":
    unittest.main()
