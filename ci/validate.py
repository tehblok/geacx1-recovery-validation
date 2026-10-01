#!/usr/bin/env python3
"""Validate original release files and host tools; never invoke flashing."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORTS = Path(os.environ.get('REPORTS', ROOT / 'reports'))
REPORTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / 'recovery/lib'))
import recovery_core as core

def run(argv, cwd):
    print('RUN', repr(argv), flush=True)
    subprocess.run(argv, cwd=cwd, env=core.sanitized_env(), check=True)

def identity():
    records = json.loads((ROOT / 'ci/release-files.json').read_text())
    for relative, expected in records.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError('Release file changed: ' + relative)
    print(f'RELEASE_IDENTITY_OK: {len(records)} files', flush=True)

def dependencies():
    if platform.machine() != 'x86_64':
        raise RuntimeError('This test must run on native x86_64')
    if 'VERSION_ID="24.04"' not in Path('/etc/os-release').read_text():
        raise RuntimeError('This test requires Ubuntu 24.04')
    vendor = ROOT / 'recovery/vendor' / core.VENDOR_NAME
    core.install_host_dependencies(vendor, run)
    audit = subprocess.check_output(['dpkg', '--audit'], text=True)
    if audit.strip():
        raise RuntimeError(audit)
    report = {'architecture': platform.machine(), 'ubuntu': '24.04',
              'missing_runtime_tools': core.check_runtime(), 'dpkg_audit': audit,
              'hardware_access': False}
    (REPORTS / 'dependencies.json').write_text(json.dumps(report, indent=2))
    packages = subprocess.check_output(
        ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n'], text=True)
    (REPORTS / 'installed-packages.tsv').write_text(packages)
    print('DEPENDENCIES_OK', flush=True)

def host_tools():
    directory = ROOT / 'recovery/vendor' / core.VENDOR_NAME / 'Linux_for_Tegra/bootloader'
    baseline = json.loads((ROOT / 'ci/host-tools-baseline.json').read_text())
    result = []
    for record in baseline:
        name = record['tool']
        completed = subprocess.run([str(directory / name), '--help'],
                                   text=True, capture_output=True, timeout=15)
        output = completed.stdout + completed.stderr
        # NVIDIA help exits nonzero on some tools. Require the expected exit
        # code AND usage text, not simply the absence of a Python exception.
        if completed.returncode != record['code'] or 'usage:' not in output.lower():
            raise RuntimeError(f'{name}: exit {completed.returncode}: {output}')
        result.append({'tool': name, 'returncode': completed.returncode, 'text': output})
    (REPORTS / 'host-tools.json').write_text(json.dumps(result, indent=2))
    print(f'HOST_TOOLS_OK: {len(result)}', flush=True)

if __name__ == '__main__':
    {'identity': identity, 'dependencies': dependencies, 'host-tools': host_tools}[sys.argv[1]]()
