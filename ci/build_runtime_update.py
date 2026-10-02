#!/usr/bin/env python3
"""Build a small offline updater; the large verified v1.4 archive stays unchanged."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    records = {}
    fixture = ROOT / 'ci/fixtures/update-v1.4-originals.json.gz'
    originals = json.loads(gzip.decompress(fixture.read_bytes()))
    for source in sorted((ROOT / 'recovery').rglob('*')):
        relative = source.relative_to(ROOT / 'recovery')
        if not source.is_file() or 'vendor' in relative.parts or '__pycache__' in relative.parts or source.suffix == '.pyc':
            continue
        name = str(relative)
        data = source.read_bytes()
        old = originals.get(name)
        before = [hashlib.sha256(old.encode()).hexdigest()] if old is not None else [None]
        if name in ('wizard.py', 'lib/recovery_core.py'):
            samples = json.loads(gzip.decompress((ROOT / 'ci/fixtures/pid-hotfix-originals.json.gz').read_bytes()))
            before.append(hashlib.sha256(samples['v1.4'][name].encode()).hexdigest())
        records[name] = {'before': before, 'sha256': hashlib.sha256(data).hexdigest(),
                         'data': base64.b64encode(data).decode()}
    compressed = gzip.compress(json.dumps(records, sort_keys=True).encode(), mtime=0)
    # Python 3.11/3.12 delegate mtime=0 to zlib, which sets a platform OS byte.
    compressed = compressed[:9] + b'\xff' + compressed[10:]
    payload = base64.b64encode(compressed).decode()
    return (ROOT / 'ci/update_runtime_template.py').read_text().replace('__PAYLOAD__', payload)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'UPDATE_RECOVERY.py'
    expected = build()
    if args.check:
        if output.read_text() != expected:
            raise SystemExit('UPDATE_RECOVERY.py is stale; regenerate it with ci/build_runtime_update.py')
    else:
        output.write_text(expected)
