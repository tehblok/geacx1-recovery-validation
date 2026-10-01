import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('delivery', Path(__file__).resolve().parents[1] / 'assemble.py')
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def fixture(root, data=b'a small complete archive fixture'):
    (root / 'payload').mkdir()
    records = []
    for i, content in enumerate((data[:len(data)//2], data[len(data)//2:]), 1):
        name = f'fixture.tar.gz.{i:03d}'
        (root / 'payload' / name).write_bytes(content)
        records.append({'name': name, 'size': len(content), 'sha256': sha(content)})
    return {'archive': 'fixture.tar.gz', 'sha256': sha(data), 'size': len(data),
            'directory': 'geacx1-recovery', 'parts': records}

class DeliveryTests(unittest.TestCase):
    def test_extra_manifest_hash_and_original_package_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = b'extra original manufacturer package'
            record = fixture(root, content)
            (root / 'payload').rename(root / 'manufacturer-extra')
            record['name'] = 'original.deb'
            (root / 'manufacturer-extra/README_RU.txt').write_text('Do not install automatically')
            raw = json.dumps([record]).encode()
            (root / 'extra-manufacturer.json').write_bytes(raw)
            with mock.patch.object(delivery, 'EXTRA_MANIFEST_SHA256', sha(raw)):
                self.assertEqual(delivery.load_extra_manifest(root), [record])
                (root / 'extra-manufacturer.json').write_bytes(raw + b' ')
                with self.assertRaises(delivery.DeliveryError):
                    delivery.load_extra_manifest(root)
            staging = root / 'staging'; staging.mkdir()
            with mock.patch.object(delivery.subprocess, 'run') as execute:
                delivery.restore_extras(root, [record], staging)
            execute.assert_not_called()
            self.assertEqual((staging / 'manufacturer-extra/original.deb').read_bytes(), content)

    def test_exact_bytes_are_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'12345678' * 30
            manifest = fixture(root, data)
            output = io.BytesIO()
            delivery.combine(root, manifest, output)
            self.assertEqual(output.getvalue(), data)

    def test_missing_and_truncated_parts_fail_before_copy(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = fixture(root)
                path = root / 'payload' / manifest['parts'][1]['name']
                path.unlink() if missing else path.write_bytes(b'x')
                output = io.BytesIO()
                with self.assertRaises(delivery.DeliveryError):
                    delivery.combine(root, manifest, output)
                self.assertEqual(output.getvalue(), b'')

    def test_same_size_corruption_and_wrong_whole_hash_fail(self):
        for corrupt_part in (False, True):
            with self.subTest(corrupt_part=corrupt_part), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = fixture(root)
                if corrupt_part:
                    part = manifest['parts'][0]
                    (root / 'payload' / part['name']).write_bytes(b'X' * part['size'])
                else:
                    manifest['sha256'] = '0' * 64
                with self.assertRaises(delivery.DeliveryError):
                    delivery.combine(root, manifest, io.BytesIO())

    def test_manifest_is_pinned_and_rejects_paths_and_malformed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = fixture(root)
            cases = []
            for key, value in [('archive', '../bad.tgz'), ('sha256', '0'*64),
                               ('size', 1), ('directory', '../escape')]:
                bad = copy.deepcopy(valid); bad[key] = value; cases.append(bad)
            bad = copy.deepcopy(valid); bad['parts'][0]['name'] = '../escape'; cases.append(bad)
            bad = copy.deepcopy(valid); bad['parts'][0]['size'] = 'broken'; cases.append(bad)
            bad = copy.deepcopy(valid); bad['parts'] = [None]; cases.append(bad)
            cases.extend([[], {'parts': None}])
            with mock.patch.multiple(delivery, ARCHIVE=valid['archive'], SHA256=valid['sha256'], SIZE=valid['size']):
                (root / 'parts.json').write_text(json.dumps(valid))
                self.assertEqual(delivery.load_manifest(root), valid)
                for bad in cases:
                    with self.subTest(data=bad):
                        (root / 'parts.json').write_text(json.dumps(bad))
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.load_manifest(root)

    def test_existing_destination_is_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manifest = fixture(root)
            destination = root / 'existing'; destination.mkdir()
            marker = destination / 'important.txt'; marker.write_text('keep')
            with mock.patch.object(delivery.subprocess, 'run') as run:
                with self.assertRaises(delivery.DeliveryError):
                    delivery.unpack(root, manifest, destination)
            run.assert_not_called()
            self.assertEqual(marker.read_text(), 'keep')

    def test_bad_part_and_tar_failure_leave_no_destination_or_staging(self):
        for bad_part in (False, True):
            with self.subTest(bad_part=bad_part), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); manifest = fixture(root)
                if bad_part:
                    manifest['parts'][0]['sha256'] = '0' * 64
                destination = root / 'new'
                with mock.patch.object(delivery.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['tar'])):
                    with self.assertRaises((delivery.DeliveryError, subprocess.CalledProcessError)):
                        delivery.unpack(root, manifest, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob('.geacx1-unpack-*')), [])

    @unittest.skipUnless(shutil.which('tar') and shutil.which('sha256sum'), 'Linux tar and sha256sum required')
    def test_real_small_archive_unpacks_and_validates_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = {'START.sh': b'#!/bin/sh\nexit 0\n',
                       'docs/SYMLINKS.json': b'{"start-link": "START.sh"}\n'}
            content['SHA256SUMS'] = ''.join(f'{sha(data)}  {name}\n' for name, data in content.items()).encode()
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                for name, data in content.items():
                    member = tarfile.TarInfo('geacx1-recovery/' + name)
                    member.size = len(data); member.mode = 0o755 if name == 'START.sh' else 0o644
                    archive.addfile(member, io.BytesIO(data))
                member = tarfile.TarInfo('geacx1-recovery/start-link')
                member.type = tarfile.SYMTYPE; member.linkname = 'START.sh'
                archive.addfile(member)
            manifest = fixture(root, output.getvalue())
            with mock.patch.object(delivery.os, 'execvp') as launch:
                kit = delivery.unpack(root, manifest, root / 'destination with spaces')
            launch.assert_not_called()
            self.assertEqual((kit / 'START.sh').read_bytes(), content['START.sh'])
            self.assertTrue((kit / 'start-link').is_symlink())
            self.assertEqual(list(root.glob('.geacx1-unpack-*')), [])

if __name__ == '__main__':
    unittest.main()
