#!/usr/bin/env bash
set -euo pipefail
cd /workspace
export DEBIAN_FRONTEND=noninteractive REPORTS=/reports
apt-get update
apt-get install -y --no-install-recommends sudo python3 ca-certificates lsb-release
/usr/bin/python3 ci/validate.py identity | tee /reports/identity.log
/usr/bin/python3 -m unittest discover -s recovery/tests -v 2>&1 | tee /reports/unit-tests.log
/usr/bin/python3 -m unittest discover -s ci -p test_delivery.py -v 2>&1 | tee /reports/delivery-tests.log
/usr/bin/python3 assemble.py --verify-only 2>&1 | tee /reports/full-payload.log
bash -n recovery/START.sh recovery/POSTCHECK.sh
/usr/bin/python3 ci/validate.py dependencies 2>&1 | tee /reports/dependencies.log
/usr/bin/python3 ci/validate.py host-tools 2>&1 | tee /reports/host-tools.log
bash ci/run-backup-shell.sh 2>&1 | tee /reports/backup-shell.log
printf '%s\n' 'All CI checks passed. No board was flashed.' > /reports/RESULT.txt
