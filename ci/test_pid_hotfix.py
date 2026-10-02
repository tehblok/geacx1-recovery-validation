import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import FIX_RECOVERY_7223 as fix

class HotfixTests(unittest.TestCase):
    def fixture(self, root, version='v1.4'):
        samples=json.loads(gzip.decompress((ROOT/'ci/fixtures/pid-hotfix-originals.json.gz').read_bytes()))[version]
        for name,text in samples.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        (root/'SHA256SUMS').write_text(''.join(f'{fix.sha(text.encode())}  {name}\n' for name,text in samples.items()))
        (root/'last-work.json').write_text('{"l4t":"/var/tmp/already-prepared/Linux_for_Tegra"}\n')
        return samples

    def test_all_known_versions_preserve_prepared_path_and_eeprom_guard(self):
        for version in ('v1.2','v1.3','v1.4'):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);old=self.fixture(root,version)
                last=(root/'last-work.json').read_bytes()
                backup=fix.apply_fix(root)
                self.assertTrue(backup.is_dir())
                for name,previous in old.items():
                    self.assertEqual((backup/name).read_text(),previous)
                    current=(root/name).read_text()
                    self.assertIn(fix.sha(current.encode()),(root/'SHA256SUMS').read_text())
                current=(root/'lib/recovery_core.py').read_text()
                self.assertIn('"${board_id}" != "3701"',current)
                self.assertIn('"${board_sku}" != "0004"',current)
                self.assertEqual((root/'last-work.json').read_bytes(),last)
                if version=='v1.4':
                    for name in old:
                        self.assertEqual((root/name).read_bytes(),(ROOT/'recovery'/name).read_bytes())
                self.assertIsNone(fix.apply_fix(root))

    def test_unknown_source_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            (root/'lib/recovery_core.py').write_text('unknown version')
            before={p:p.read_bytes() for p in (root/'wizard.py',root/'lib/recovery_core.py',root/'SHA256SUMS')}
            with self.assertRaises(ValueError): fix.apply_fix(root)
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            self.assertEqual(list(root.glob('usb-7223-original-*')),[])

    def test_bad_manifest_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.fixture(root)
            (root/'SHA256SUMS').write_text('0'*64+'  wizard.py\n')
            with self.assertRaises(ValueError):fix.apply_fix(root)
            self.assertEqual((root/'wizard.py').read_text(),old['wizard.py'])

    def test_failure_during_second_write_rolls_back_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.fixture(root)
            original_manifest=(root/'SHA256SUMS').read_bytes()
            replace=fix._replace;calls=0
            def failing(path,data,info):
                nonlocal calls
                calls+=1
                if calls==2:raise OSError('simulated disk error')
                replace(path,data,info)
            with patch.object(fix,'_replace',side_effect=failing):
                with self.assertRaises(OSError):fix.apply_fix(root)
            for name,text in old.items():self.assertEqual((root/name).read_text(),text)
            self.assertEqual((root/'SHA256SUMS').read_bytes(),original_manifest)

    def test_symlink_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.fixture(root)
            original=root/'wizard.py';moved=root/'outside.py';original.rename(moved);original.symlink_to(moved)
            with self.assertRaises(ValueError):fix.apply_fix(root)
            self.assertEqual(moved.read_text(),old['wizard.py'])

if __name__=='__main__':unittest.main()
