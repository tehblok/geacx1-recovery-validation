#!/usr/bin/env python3
"""Exercise the actual shipped archive; never request sudo or access hardware."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import assemble


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix='geacx1-delivery-test-') as tmp:
        kit = assemble.unpack(ROOT, assemble.load_manifest(), Path(tmp) / 'kit',
                              assemble.load_extra_manifest())
        # A green source-code test suite must describe the code in Download ZIP.
        shipped = [p for p in (ROOT / 'recovery').rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
        for source in shipped:
            relative = source.relative_to(ROOT / 'recovery')
            if digest(source) != digest(kit / relative):
                raise RuntimeError(f'Source differs from shipped archive: {relative}')
        for relative in ('GEACX1-GUIDE-RU.html', 'GEACX1-GUIDE-RU.pdf',
                         'SITE_REVIEW_RU.txt'):
            if digest(ROOT / 'docs' / relative) != digest(kit / 'docs' / relative):
                raise RuntimeError(f'Guide differs from shipped archive: {relative}')
        for args in (['--help'], ['--demo'], ['--plan', '--mode', 'emmc'],
                     ['--plan', '--mode', 'qspi'], ['--plan', '--mode', 'nvme']):
            subprocess.run(['bash', str(kit / 'START.sh'), *args], check=True, timeout=30)
        print(f'SHIPPED_KIT_OK: {len(shipped)} source files match; guides match; launch/plan passed.')


if __name__ == '__main__':
    main()
