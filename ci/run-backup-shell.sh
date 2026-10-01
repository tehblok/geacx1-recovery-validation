#!/usr/bin/env bash
# Run only on an ephemeral Linux CI runner.  It creates fake device files below
# mktemp; every command that could write a device is a local stub.
set -eo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# The Actions dry-run mounts work/github-validation as /workspace.  The
# fallback keeps this file locally executable from the full development tree.
if [ -d "$HERE/../recovery/lib" ]; then
    ROOT=$(cd "$HERE/.." && pwd)
    RECOVERY="$ROOT/recovery"
    FIXTURES="$RECOVERY/vendor/flashtool_jp7.2_GA_r1.0_20260722/Linux_for_Tegra/tools/backup_restore"
else
    ROOT=$(cd "$HERE/../../.." && pwd)
    RECOVERY="$ROOT/geacx1-recovery"
    FIXTURES="$HERE/backup-shell/vendor"
fi
TMP=$(mktemp -d)
L4T="$TMP/Linux_for_Tegra"
TOOLS="$L4T/tools/backup_restore"
IMAGES="$TOOLS/images"
BIN="$TMP/bin"
DEV="$TMP/dev"
LOGS="$TMP/logs"
export HARNESS_DEV="$DEV" DESTRUCTIVE_LOG="$LOGS/destructive.log"
mkdir -p "$TOOLS" "$L4T/tools/kernel_flash" "$BIN" "$DEV" "$LOGS" "$TMP/sys/block/mmcblk0"
publish_logs() {
  local rc=$?
  set +e
  if [ -n "${REPORTS:-}" ]; then
    mkdir -p "$REPORTS/backup-shell"
    cp -a "$LOGS/." "$REPORTS/backup-shell/"
    printf 'exit_status=%s\n' "$rc" > "$REPORTS/backup-shell/summary.txt"
  fi
  rm -rf "$TMP"
  trap - EXIT
  exit "$rc"
}
trap publish_logs EXIT
cp "$FIXTURES/nvbackup_partitions.sh" "$FIXTURES/nvrestore_partitions.sh" "$TOOLS/"
: > "$L4T/tools/kernel_flash/l4t_initrd_flash.sh"
printf 'foo\n' > "$TMP/board_spec.txt"
printf '4096\n' > "$TMP/sys/block/mmcblk0/size"

PYTHONPATH="$RECOVERY/lib" python3 - "$L4T" <<'PY'
import sys
import backup_restore
print(backup_restore.harden_prepared_tools(sys.argv[1]))
PY

# The two substitutions make the unmodified-plus-hardened scripts runnable in
# an unprivileged filesystem namespace.  Their tested control flow is intact.
for script in "$TOOLS"/nv*partitions.sh; do
    sed -i 's|^MODEL=.*device-tree/compatible).*|MODEL="3701" # CI fixture|' "$script"
    sed -i "s|/etc/board_spec.txt|$TMP/board_spec.txt|g; s|/sys/block/|$TMP/sys/block/|g; s|/dev/|\\${HARNESS_DEV}/|g" "$script"
    # CI owns no device nodes.  In this disposable copy only, ordinary files
    # represent the already-selected test devices; dd/erase are still stubs.
    sed -i 's/\[ ! -b /[ ! -e /g; s/\[ -b /[ -e /g; s/\[ ! -c /[ ! -e /g; s/\[ -c /[ -e /g' "$script"
    sed -i '\|mapfile -t partition_lists|c\partition_lists=("${HARNESS_DEV}/mmcblk0p1")' "$script"
done

cat > "$BIN/dd" <<'SH'
#!/bin/sh
for arg in "$@"; do case "$arg" in of=*) out=${arg#of=};; esac; done
[ -n "${out:-}" ] || exit 96
if [ "${DD_MODE:-}" = fail ] && echo "$out" | grep -q '/temp$'; then echo TEST_DD_FAILURE >&2; exit 7; fi
case "$out" in
  */temp) printf 'GEACX1-EXACT-PARTITION-BYTES\n' > "$out";;
  *gptmbr.img) printf 'GEACX1-PRIMARY-GPT\n' > "$out";;
  *gptbackup.img) printf 'GEACX1-SECONDARY-GPT\n' > "$out";;
  *QSPI0.img) printf 'GEACX1-QSPI-BYTES\n' > "$out";;
  *) : > "$out";;
esac
echo "dd $*" >> "$DESTRUCTIVE_LOG"
SH
cat > "$BIN/zstd" <<'SH'
#!/bin/sh
[ "${ZSTD_MODE:-}" = fail ] && { echo TEST_ZSTD_FAILURE >&2; exit 41; }
exec /usr/bin/zstd "$@"
SH
cat > "$BIN/fdisk" <<'SH'
#!/bin/sh
printf '%s\n\n' 'Device Boot Start End Sectors Size Id Type' "$HARNESS_DEV/mmcblk0p1 2048 4095 2048 1M 83 Linux"
SH
cat > "$BIN/blockdev" <<'SH'
#!/bin/sh
case "$1:${BLOCK_MODE:-ok}" in
  --getss:sector4096) echo 4096;; --getss:*) echo 512;;
  --getsz:small) echo 50;; --getsz:*) echo 4096;; *) exit 98;;
esac
SH
cat > "$BIN/blkid" <<'SH'
#!/bin/sh
[ "${BLKID_MODE:-}" = ext4 ] && echo ext4
SH
cat > "$BIN/mtd_debug" <<'SH'
#!/bin/sh
echo 'mtd.size = 67108864'
SH
cat > "$BIN/tar" <<'SH'
#!/bin/sh
[ "${TAR_MODE:-}" = fail ] && { echo TEST_TAR_FAILURE >&2; exit 23; }
exec /usr/bin/tar "$@"
SH
for name in sync partprobe flash_erase mount umount; do
  cat > "$BIN/$name" <<'SH'
#!/bin/sh
exit 0
SH
done
cat > "$BIN/blkdiscard" <<'SH'
#!/bin/sh
echo "blkdiscard $*" >> "$DESTRUCTIVE_LOG"
exit 1
SH
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

devices() {
  rm -f "$DEV"/*
  : > "$DEV/mmcblk0"
  : > "$DEV/mmcblk0p1"
  : > "$DEV/mtd0"
}
clear_images() { rm -rf "$IMAGES"; mkdir -p "$IMAGES"; : > "$DESTRUCTIVE_LOG"; }
sha() { sha256sum "$1" | awk '{print $1}'; }
restore_map() {
  clear_images
  printf gpt > "$IMAGES/mmcblk0_gptmbr.img"; printf payload > "$IMAGES/mmcblk0p1_bak.img"
  printf backupgpt > "$IMAGES/mmcblk0_gptbackup.img"; printf qspi > "$IMAGES/QSPI0.img"
  printf 'board_spec,foo\nmmcblk0_gptmbr.img,gpt_1,0,34,,%s\nmmcblk0p1_bak.img,mmcblk0p1,2048,1,,%s\nmmcblk0_gptbackup.img,gpt_2,4063,33,,%s\nQSPI0.img,qspi0,0,4,,%s\n' "$(sha "$IMAGES/mmcblk0_gptmbr.img")" "$(sha "$IMAGES/mmcblk0p1_bak.img")" "$(sha "$IMAGES/mmcblk0_gptbackup.img")" "$(sha "$IMAGES/QSPI0.img")" > "$IMAGES/nvpartitionmap.txt"
}
case_run() {
  local name=$1 expected=$2 needle=$3; shift 3
  set +e; "$@" > "$LOGS/$name.log" 2>&1; local got=$?; set -e
  [ "$got" = "$expected" ] || { cat "$LOGS/$name.log"; echo "$name: expected $expected, got $got" >&2; exit 1; }
  grep -Fq "$needle" "$LOGS/$name.log" || { cat "$LOGS/$name.log"; echo "$name: missing $needle" >&2; exit 1; }
  ! grep -Fq 'Backup complete' "$LOGS/$name.log" || [ "$expected" = 0 ] || exit 1
  ! grep -Fq 'Successful restore' "$LOGS/$name.log" || [ "$expected" = 0 ] || exit 1
  echo "PASS $name"
}
backup() { bash "$TOOLS/nvbackup_partitions.sh" "$@"; }
restore() { bash "$TOOLS/nvrestore_partitions.sh" "$@"; }

# 1–2: requested devices/QSPI are mandatory before a full backup.
devices; rm -f "$DEV/nvme0n1"; clear_images
case_run backup_missing_nvme 1 'requested storage device' backup -n -e mmcblk0:nvme0n1
devices; rm "$DEV/mtd0"; clear_images
case_run backup_missing_qspi 1 'required QSPI device' backup -n -e mmcblk0

# 3–4: restore geometry rejects before the tracked destructive stubs.
devices; restore_map; export BLOCK_MODE=sector4096
case_run restore_sector4096 1 'unsupported logical sector size 4096' restore -n -e mmcblk0
[ ! -s "$DESTRUCTIVE_LOG" ] || exit 1
devices; restore_map; export BLOCK_MODE=small
case_run restore_too_small 1 'smaller than the backup' restore -n -e mmcblk0
[ ! -s "$DESTRUCTIVE_LOG" ] || exit 1
unset BLOCK_MODE

# 5–8: successful byte stream and three backup failure paths.
devices; clear_images; backup -n -e mmcblk0 -z > "$LOGS/backup_exact_stream.log" 2>&1
zstd -q -dc "$IMAGES/mmcblk0p1_bak.img" | sha256sum | grep -Fq '2e6ce8375dbcccbd16074fbad8ccfe1ef99cec59d4723ce6655d92b3c4dd2081'
echo 'PASS backup_exact_stream'
devices; clear_images; export ZSTD_MODE=fail
case_run backup_zstd_failure 41 'TEST_ZSTD_FAILURE' backup -n -e mmcblk0 -z
unset ZSTD_MODE
devices; clear_images; export BLKID_MODE=ext4 TAR_MODE=fail
case_run backup_tar_failure 1 'tar failed while backing up' backup -n -e mmcblk0
unset BLKID_MODE TAR_MODE
devices; clear_images; export DD_MODE=fail
case_run backup_dd_failure 1 'dd failed while backing up' backup -n -e mmcblk0 -z
unset DD_MODE

# 9–10: pipeline and checksum failures are propagated by patched restore.
devices; restore_map; export ZSTD_MODE=fail
case_run restore_zstd_pipeline_failure 1 'Error flashing mmcblk0' restore -n -e mmcblk0
unset ZSTD_MODE
devices; restore_map
sed -i 's/mmcblk0p1_bak.img,mmcblk0p1,2048,1,,[0-9a-f]*/mmcblk0p1_bak.img,mmcblk0p1,2048,1,,0000000000000000000000000000000000000000000000000000000000000000/' "$IMAGES/nvpartitionmap.txt"
case_run restore_checksum_mismatch 1 'Checksum of mmcblk0p1 does not match' restore -n -e mmcblk0
echo 'ALL 10 BACKUP SHELL FAULT TESTS PASSED'
