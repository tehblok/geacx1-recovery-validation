"""Safe wrapper around NVIDIA's R39.2 full-device backup/restore workflow.

The NVIDIA tool always reads and writes ``tools/backup_restore/images``.  This
module gives that fixed directory a transactional lifetime, preserves anything
that was there before, and exports a self-contained snapshot with SHA-256
metadata to a user-selected directory.
"""
from contextlib import contextmanager
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid


FORMAT = "geacx1-full-backup-v1"
FAMILY = "geacx1-p3701-0004"
BOARD_NAME = "geacx1-32gb-jp72"
L4T_RELEASE = "R39.2.0"
MANIFEST_NAME = "GEACX1_BACKUP.json"
MAP_NAME = "nvpartitionmap.txt"
SCOPES = {
    "emmc": ("mmcblk0",),
    "emmc_nvme": ("mmcblk0", "nvme0n1"),
}
NVIDIA_BACKUP_SCRIPT_SHA256 = "0fd9307a46594f41eac27c8f683cf429ed0016264b6a3e1027270a30cfe88cf8"
NVIDIA_RESTORE_SCRIPT_SHA256 = "ae39bc6122a9de54f9c7938e3511bb12c0cf20f6cbb92d851fd7febfffeec127"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")


class BackupError(ValueError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tool_dir(l4t):
    return Path(l4t) / "tools" / "backup_restore"


def validate_tools(l4t):
    l4t = Path(l4t)
    required = (
        _tool_dir(l4t) / "l4t_backup_restore.sh",
        _tool_dir(l4t) / "l4t_backup_restore.func",
        _tool_dir(l4t) / "nvbackup_partitions.sh",
        _tool_dir(l4t) / "nvrestore_partitions.sh",
        l4t / "tools" / "kernel_flash" / "l4t_initrd_flash.sh",
    )
    missing = [str(path.relative_to(l4t)) for path in required if not path.is_file()]
    if missing:
        raise BackupError("В подготовленной сборке нет штатных средств NVIDIA: " + ", ".join(missing))
    return True


def harden_prepared_tools(l4t):
    """Make NVIDIA's backup fail if tar failed instead of hashing a partial archive.

    The replacement is deliberately pinned to the unmodified R39.2 vendor
    script.  It is applied only to the disposable prepared copy, never to the
    manufacturer's source bundle.
    """
    script = _tool_dir(l4t) / "nvbackup_partitions.sh"
    restore_script = _tool_dir(l4t) / "nvrestore_partitions.sh"
    if not script.is_file() or _sha256(script) != NVIDIA_BACKUP_SCRIPT_SHA256:
        raise BackupError("Нельзя безопасно усилить nvbackup_partitions.sh: версия или SHA-256 не совпали.")
    if not restore_script.is_file() or _sha256(restore_script) != NVIDIA_RESTORE_SCRIPT_SHA256:
        raise BackupError("Нельзя безопасно усилить nvrestore_partitions.sh: версия или SHA-256 не совпали.")
    text = script.read_text(encoding="utf-8")
    squash_old = """\t\tset +e
\t\ttar -I 'zstd -T0' -cpf \"${LDK_DIR}/${app_partition##*/}.tar.zst\" \"${COMMON_TAR_OPTIONS[@]}\"
\t\tset -e
"""
    squash_new = """\t\tif ! tar -I 'zstd -T0' -cpf \"${LDK_DIR}/${app_partition##*/}.tar.zst\" \"${COMMON_TAR_OPTIONS[@]}\"; then
\t\t\tprint_message \"Error: tar failed while backing up ${app_partition}\"
\t\t\texit 1
\t\tfi
"""
    normal_old = """\t\t\t\tset +e
\t\t\t\ttar -I 'zstd -T0' -cpf \"${LDK_DIR}/${tmp}.tar.zst\" \"${COMMON_TAR_OPTIONS[@]}\"
\t\t\t\tset -e
"""
    normal_new = """\t\t\t\tif ! tar -I 'zstd -T0' -cpf \"${LDK_DIR}/${tmp}.tar.zst\" \"${COMMON_TAR_OPTIONS[@]}\"; then
\t\t\t\t\tprint_message \"Error: tar failed while backing up ${tmp}\"
\t\t\t\t\texit 1
\t\t\t\tfi
"""
    dd_old = """\t\t\tdd if=\"$i\" conv=sync,noerror bs=64k of=\"${LDK_DIR}/temp\" status=progress
"""
    dd_new = """\t\t\tif ! dd if=\"$i\" bs=64K iflag=fullblock of=\"${LDK_DIR}/temp\" status=progress; then
\t\t\t\tprint_message \"Error: dd failed while backing up ${tmp}\"
\t\t\t\texit 1
\t\t\tfi
"""
    if text.count(squash_old) != 1 or text.count(normal_old) != 1 or text.count(dd_old) != 1:
        raise BackupError("Структура nvbackup_partitions.sh не совпала с проверенной R39.2.")
    text = text.replace(squash_old, squash_new).replace(normal_old, normal_new).replace(dd_old, dd_new)
    script.write_text(text, encoding="utf-8")
    if ("set +e" in text or "noerror" in text or text.count("if ! tar -I 'zstd -T0'") != 2
            or text.count('if ! dd if="$i" bs=64K iflag=fullblock') != 1):
        raise BackupError("Проверка усиленного nvbackup_partitions.sh не прошла.")
    device_old = """EXISTING_BLOCK_DEVICES=()
for dev in \"${BLOCK_DEVICE_LIST[@]}\"; do
\tif [ -b \"/dev/${dev}\" ]; then
\t\tEXISTING_BLOCK_DEVICES+=(\"${dev}\")
\tfi
done
"""
    device_new = """EXISTING_BLOCK_DEVICES=()
for dev in \"${BLOCK_DEVICE_LIST[@]}\"; do
\tif [ ! -b \"/dev/${dev}\" ]; then
\t\techo \"Error: requested storage device /dev/${dev} does not exist\"
\t\texit 1
\tfi
\tEXISTING_BLOCK_DEVICES+=(\"${dev}\")
done
if [ ! -c /dev/mtd0 ]; then
\techo \"Error: required QSPI device /dev/mtd0 does not exist\"
\texit 1
fi
"""
    if text.count(device_old) != 1:
        raise BackupError("Не найдена проверка запрошенных дисков в nvbackup_partitions.sh.")
    restore_text = restore_script.read_text(encoding="utf-8")
    if restore_text.count(device_old) != 1:
        raise BackupError("Не найдена проверка запрошенных дисков в nvrestore_partitions.sh.")
    script.write_text(text.replace(device_old, device_new), encoding="utf-8")
    restore_text = restore_text.replace(device_old, device_new)
    if restore_text.count("set -e\n") != 1:
        raise BackupError("Не найден режим set -e в nvrestore_partitions.sh.")
    restore_text = restore_text.replace("set -e\n", "set -eo pipefail\n", 1)
    checksum_exit_old = """\t\t\tif [ \"${checksum}\" != \"${FIELDS[6]}\" ]; then
\t\t\t\techo \"${SCRIPT_NAME} Checksum of ${FIELDS[2]} does not match the checksum in the index file.\"
\t\t\t\texit
\t\t\tfi
"""
    checksum_exit_new = checksum_exit_old.replace("\t\t\t\texit\n", "\t\t\t\texit 1\n")
    if restore_text.count(checksum_exit_old) != 1:
        raise BackupError("Не найдена проверка SHA-256 раздела в nvrestore_partitions.sh.")
    restore_text = restore_text.replace(checksum_exit_old, checksum_exit_new)
    preflight_anchor = """declare -A able_to_delete

for device in \"${EXISTING_BLOCK_DEVICES[@]}\"; do
"""
    preflight = """# Fail before blkdiscard/flash if target geometry cannot contain this backup.
for device in \"${EXISTING_BLOCK_DEVICES[@]}\"; do
\tsector_size=$(blockdev --getss \"/dev/${device}\")
\tif [ \"${sector_size}\" != \"512\" ]; then
\t\techo \"Error: /dev/${device} has unsupported logical sector size ${sector_size}\"
\t\texit 1
\tfi
\trequired_sectors=$(awk -F, -v image=\"${device}_gptbackup.img\" '$1 == image && $2 == \"gpt_2\" {printf \"%.0f\\n\", $3 + $4}' \"${FILE_NAME}\")
\tactual_sectors=$(blockdev --getsz \"/dev/${device}\")
\tif [[ ! \"${required_sectors}\" =~ ^[0-9]+$ ]] || [ \"${actual_sectors}\" -lt \"${required_sectors}\" ]; then
\t\techo \"Error: /dev/${device} is smaller than the backup or backup geometry is missing\"
\t\texit 1
\tfi
done

declare -A able_to_delete

for device in \"${EXISTING_BLOCK_DEVICES[@]}\"; do
"""
    if restore_text.count(preflight_anchor) != 1:
        raise BackupError("Не найдена точка для проверки размера диска.")
    restore_text = restore_text.replace(preflight_anchor, preflight)
    restore_script.write_text(restore_text, encoding="utf-8")
    if (device_old in restore_text or restore_text.count("requested storage device") != 1
            or restore_text.count("required QSPI device") != 1
            or "set -eo pipefail" not in restore_text or checksum_exit_old in restore_text
            or restore_text.count("Fail before blkdiscard") != 1):
        raise BackupError("Проверка усиленного nvrestore_partitions.sh не прошла.")
    return {"backup": _sha256(script), "restore": _sha256(restore_script)}


def build_command(l4t, operation, scope):
    """Return argv for official NVIDIA Workflow 1 (backup) or 2 (restore)."""
    validate_tools(l4t)
    if operation not in ("backup", "restore"):
        raise BackupError("Неизвестная операция резервной копии.")
    if scope not in SCOPES:
        raise BackupError("Неизвестный состав накопителей.")
    flag = "-b" if operation == "backup" else "-r"
    devices = ":".join(SCOPES[scope])
    return ["./tools/backup_restore/l4t_backup_restore.sh", "-e", devices, flag, BOARD_NAME]


def _safe_regular(path, root):
    path = Path(path)
    root = Path(root).resolve()
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"Ожидался обычный файл резервной копии: {path.name}")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise BackupError(f"Файл выходит за пределы резервной копии: {path.name}") from exc


def _zstd_decompressed_size(path):
    try:
        process = subprocess.Popen(
            ["zstd", "-q", "-dc", "--", str(path)], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise BackupError("Не найден zstd для проверки образа раздела.") from exc
    total = 0
    try:
        for block in iter(lambda: process.stdout.read(4 * 1024 * 1024), b""):
            total += len(block)
    finally:
        process.stdout.close()
    if process.wait() != 0:
        raise BackupError(f"Сжатый образ раздела повреждён: {Path(path).name}")
    return total


def _parse_nvidia_map(images):
    images = Path(images)
    map_path = images / MAP_NAME
    _safe_regular(map_path, images)
    try:
        lines = map_path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise BackupError("nvpartitionmap.txt имеет неверную кодировку.") from exc
    if not lines or not lines[0].startswith("board_spec,") or not lines[0].split(",", 1)[1].strip():
        raise BackupError("В nvpartitionmap.txt отсутствует board_spec платы.")
    board_spec = lines[0].split(",", 1)[1].strip()
    if not _SAFE_NAME.fullmatch(board_spec):
        raise BackupError("В nvpartitionmap.txt указан небезопасный board_spec.")
    # flash.sh builds TNSPEC as BOARDID-FAB-BOARDSKU-BOARDREV-...-board-.
    # The board name itself may contain dashes, so only fixed positions are
    # interpreted here; restore independently compares the entire TNSPEC.
    spec_fields = board_spec.split("-")
    if (len(spec_fields) < 8 or spec_fields[0] != "3701" or spec_fields[2] != "0004"
            or not all(spec_fields[index] for index in range(7)) or spec_fields[-1] != ""):
        raise BackupError("Копия создана не для GEACX1 AGX Orin 32 ГБ (P3701-0004).")
    entries = []
    seen_files = set()
    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        fields = line.split(",")
        if len(fields) != 6:
            raise BackupError(f"Повреждена строка {number} nvpartitionmap.txt.")
        name, partition, start, size, flags, digest = fields
        if not _SAFE_NAME.fullmatch(name) or Path(name).name != name:
            raise BackupError(f"Недопустимое имя файла в строке {number} nvpartitionmap.txt.")
        if name in seen_files:
            raise BackupError(f"Дублируется файл в nvpartitionmap.txt: {name}")
        seen_files.add(name)
        if (not _SAFE_NAME.fullmatch(partition) or not start.isdigit() or not size.isdigit()
                or flags not in ("", "tz") or not _SHA256.fullmatch(digest)):
            raise BackupError(f"Повреждены поля строки {number} nvpartitionmap.txt.")
        file_path = images / name
        _safe_regular(file_path, images)
        if _sha256(file_path) != digest:
            raise BackupError(f"Не совпала контрольная сумма NVIDIA: {name}")
        decompressed_size = None
        if name.endswith(".tar.zst"):
            try:
                result = subprocess.run(
                    ["tar", "-I", "zstd", "-tf", str(file_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except OSError as exc:
                raise BackupError("Не найдены tar/zstd для проверки сжатых разделов.") from exc
            if result.returncode:
                raise BackupError(f"Сжатый образ раздела повреждён: {name}")
        elif name.endswith("_bak.img"):
            decompressed_size = _zstd_decompressed_size(file_path)
        entries.append({"file": name, "partition": partition, "flags": flags,
                        "start": int(start), "size": int(size),
                        "decompressed_size": decompressed_size})
    if not entries:
        raise BackupError("Карта NVIDIA не содержит ни одного раздела.")
    qspi = [item for item in entries
            if item["partition"].lower() == "qspi0" and item["file"] == "QSPI0.img"]
    if len(qspi) != 1:
        raise BackupError("Копия неполная: в ней нет полного образа QSPI0.")
    if (images / "QSPI0.img").stat().st_size != qspi[0]["size"]:
        raise BackupError("Копия QSPI0 имеет неверный размер.")
    return board_spec, entries


def _gpt_partitions(path):
    """Return GPT partition-number to (start sector, sector count)."""
    data = Path(path).read_bytes()
    if len(data) < 1024 or data[512:520] != b"EFI PART":
        raise BackupError(f"Повреждён первичный GPT: {Path(path).name}")
    entries_lba = int.from_bytes(data[584:592], "little")
    entries_count = int.from_bytes(data[592:596], "little")
    entry_size = int.from_bytes(data[596:600], "little")
    if not (1 <= entries_count <= 4096 and 128 <= entry_size <= 4096):
        raise BackupError(f"Неверный заголовок GPT: {Path(path).name}")
    begin = entries_lba * 512
    end = begin + entries_count * entry_size
    if begin < 1024 or end > len(data):
        raise BackupError(f"Копия GPT оборвана: {Path(path).name}")
    partitions = {}
    for index, offset in enumerate(range(begin, end, entry_size), 1):
        if data[offset:offset + 16] == b"\0" * 16:
            continue
        first = int.from_bytes(data[offset + 32:offset + 40], "little")
        last = int.from_bytes(data[offset + 40:offset + 48], "little")
        if first < 2 or last < first:
            raise BackupError(f"Неверная геометрия GPT раздела {index}: {Path(path).name}")
        partitions[index] = (first, last - first + 1)
    if not partitions:
        raise BackupError(f"GPT не содержит разделов: {Path(path).name}")
    return partitions


def _gpt_secondary_start(path):
    data = Path(path).read_bytes()
    if len(data) < 552 or data[512:520] != b"EFI PART":
        raise BackupError(f"Повреждён заголовок GPT: {Path(path).name}")
    backup_lba = int.from_bytes(data[544:552], "little")
    if backup_lba < 33:
        raise BackupError(f"Неверный backup LBA в GPT: {Path(path).name}")
    return backup_lba - 32


def _require_devices(images, entries, scope):
    images = Path(images)
    allowed_files = {"QSPI0.img"}
    for device in SCOPES[scope]:
        device_entries = [item for item in entries if item["file"].startswith(device)]
        if not device_entries:
            raise BackupError(f"Копия неполная: нет данных накопителя {device}.")
        primary = f"{device}_gptmbr.img"
        secondary = f"{device}_gptbackup.img"
        primary_entries = [item for item in device_entries if item["file"] == primary]
        secondary_entries = [item for item in device_entries if item["file"] == secondary]
        if len(primary_entries) != 1 or primary_entries[0]["partition"] != "gpt_1":
            raise BackupError(f"Копия неполная: нет первичного GPT накопителя {device}.")
        if len(secondary_entries) != 1 or secondary_entries[0]["partition"] != "gpt_2":
            raise BackupError(f"Копия неполная: нет резервного GPT накопителя {device}.")
        primary_entry, secondary_entry = primary_entries[0], secondary_entries[0]
        if (primary_entry["start"] != 0 or primary_entry["flags"] != ""
                or secondary_entry["size"] != 33 or secondary_entry["flags"] != ""
                or secondary_entry["start"] != _gpt_secondary_start(images / primary)):
            raise BackupError(f"Неверные поля GPT накопителя {device}.")
        if ((images / primary).stat().st_size != primary_entry["size"] * 512
                or (images / secondary).stat().st_size != secondary_entry["size"] * 512):
            raise BackupError(f"Неверный размер образа GPT: {device}.")
        payloads = [item for item in device_entries if item["partition"] not in ("gpt_1", "gpt_2")]
        if not payloads:
            raise BackupError(f"Копия неполная: нет разделов {device}.")
        gpt = _gpt_partitions(images / primary)
        if primary_entry["size"] != min(start for start, _size in gpt.values()):
            raise BackupError(f"Первичный GPT {device} не доходит до первого раздела.")
        if len(payloads) != len(gpt):
            raise BackupError(f"Копия неполная: GPT {device} описывает {len(gpt)} разделов, "
                              f"а в карте копии их {len(payloads)}.")
        seen_numbers = set()
        for item in payloads:
            raw = re.fullmatch(re.escape(device) + r"p([1-9][0-9]*)_bak\.img", item["file"])
            tar = re.fullmatch(re.escape(device) + r"p([1-9][0-9]*)\.tar\.zst", item["file"])
            match = raw or tar
            if not match:
                raise BackupError(f"Неизвестный тип образа {device}: {item['file']}")
            number = int(match.group(1))
            if number in seen_numbers or number not in gpt:
                raise BackupError(f"Образ {item['file']} не соответствует GPT {device}.")
            seen_numbers.add(number)
            expected_start, expected_size = gpt[number]
            if raw and (item["partition"] != device or item["flags"] != ""
                        or item["start"] != expected_start or item["size"] != expected_size
                        or item["decompressed_size"] != expected_size * 512):
                raise BackupError(f"Образ {item['file']} может записаться не в свой раздел.")
            if tar and (item["partition"] != f"{device}p{number}" or item["flags"] != "tz"
                        or item["start"] != 0 or item["size"] != expected_size):
                raise BackupError(f"Архив {item['file']} может распаковаться не в свой раздел.")
            allowed_files.add(item["file"])
        allowed_files.update((primary, secondary))
    if {item["file"] for item in entries} != allowed_files:
        raise BackupError("Карта NVIDIA содержит раздел вне подтверждённого состава копии.")


def export_snapshot(images, destination, scope):
    """Validate NVIDIA output and atomically export it to a new directory."""
    if scope not in SCOPES:
        raise BackupError("Неизвестный состав накопителей.")
    images = Path(images)
    destination = Path(destination).expanduser().absolute()
    try:
        destination.relative_to(images.absolute())
    except ValueError:
        pass
    else:
        raise BackupError("Папка копии не должна находиться внутри временной папки NVIDIA.")
    if destination.exists():
        raise BackupError("Папка назначения уже существует. Выберите новое имя, чтобы ничего не перезаписать.")
    if not destination.parent.is_dir():
        raise BackupError("Родительская папка для резервной копии не существует.")
    board_spec, entries = _parse_nvidia_map(images)
    _require_devices(images, entries, scope)
    referenced = {MAP_NAME, *(item["file"] for item in entries)}
    temp = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    temp.mkdir(mode=0o700)
    try:
        for name in sorted(referenced):
            source = images / name
            _safe_regular(source, images)
            target = temp / name
            try:
                # On one filesystem this is atomic and needs no second copy of a
                # potentially huge image.  Removing NVIDIA's staging link later
                # leaves the exported link and its data intact.
                os.link(source, target, follow_symlinks=False)
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
                    raise
                shutil.copy2(source, target, follow_symlinks=False)
        files = {}
        for path in sorted(temp.iterdir(), key=lambda item: item.name):
            _safe_regular(path, temp)
            files[path.name] = {"sha256": _sha256(path), "size": path.stat().st_size}
        metadata = {
            "format": FORMAT,
            "family": FAMILY,
            "board_name": BOARD_NAME,
            "l4t_release": L4T_RELEASE,
            "scope": scope,
            "devices": list(SCOPES[scope]),
            "includes_qspi": True,
            "board_spec": board_spec,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "files": files,
            "exclusions": ["eFuse", "EEPROM", "CPLD firmware", "eMMC boot0/boot1/RPMB hardware regions"],
        }
        (temp / MANIFEST_NAME).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temp, destination)
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp)
        raise
    return destination


def validate_snapshot(snapshot):
    """Verify compatibility, completeness, map checksums and our full manifest."""
    snapshot = Path(snapshot).expanduser().absolute()
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise BackupError("Укажите обычную папку резервной копии GEACX1.")
    manifest_path = snapshot / MANIFEST_NAME
    _safe_regular(manifest_path, snapshot)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("Манифест резервной копии повреждён.") from exc
    expected = {"format": FORMAT, "family": FAMILY, "board_name": BOARD_NAME, "l4t_release": L4T_RELEASE}
    for key, value in expected.items():
        if data.get(key) != value:
            raise BackupError(f"Несовместимая резервная копия: поле {key}.")
    scope = data.get("scope")
    if scope not in SCOPES or data.get("devices") != list(SCOPES[scope]) or data.get("includes_qspi") is not True:
        raise BackupError("Манифест содержит неверный состав накопителей.")
    files = data.get("files")
    if not isinstance(files, dict) or MAP_NAME not in files:
        raise BackupError("В манифесте нет полного списка файлов.")
    actual_names = {path.name for path in snapshot.iterdir()}
    expected_names = set(files) | {MANIFEST_NAME}
    if actual_names != expected_names:
        raise BackupError("Состав папки не совпадает с манифестом резервной копии.")
    for name, record in files.items():
        if not _SAFE_NAME.fullmatch(name) or Path(name).name != name or not isinstance(record, dict):
            raise BackupError("В манифесте есть небезопасное имя файла.")
        path = snapshot / name
        _safe_regular(path, snapshot)
        digest, size = record.get("sha256"), record.get("size")
        if not _SHA256.fullmatch(str(digest)) or not isinstance(size, int) or size < 0:
            raise BackupError(f"Повреждена запись манифеста: {name}")
        if path.stat().st_size != size or _sha256(path) != digest:
            raise BackupError(f"Файл резервной копии повреждён: {name}")
    board_spec, entries = _parse_nvidia_map(snapshot)
    _require_devices(snapshot, entries, scope)
    referenced = {MAP_NAME, *(item["file"] for item in entries)}
    if set(files) != referenced:
        raise BackupError("Манифест и карта NVIDIA описывают разный набор файлов.")
    if data.get("board_spec") != board_spec:
        raise BackupError("board_spec манифеста не совпадает с картой NVIDIA.")
    return data


@contextmanager
def isolated_images(l4t, snapshot=None):
    """Temporarily provide a clean official images directory and restore the old one."""
    validate_tools(l4t)
    base = _tool_dir(l4t)
    images = base / "images"
    saved = base / f".images.geacx1-saved-{uuid.uuid4().hex}"
    if images.is_symlink():
        raise BackupError("Штатная папка images является символической ссылкой; операция остановлена.")
    had_images = images.exists()
    if had_images:
        if not images.is_dir():
            raise BackupError("Штатный путь images занят не папкой.")
        os.replace(images, saved)
    try:
        images.mkdir(mode=0o700)
        if snapshot is not None:
            metadata = validate_snapshot(snapshot)
            for path in Path(snapshot).iterdir():
                if path.name == MANIFEST_NAME:
                    continue
                _safe_regular(path, snapshot)
                shutil.copy2(path, images / path.name, follow_symlinks=False)
            # Detect source changes or a short/corrupt copy before touching the board.
            for name, record in metadata["files"].items():
                staged = images / name
                _safe_regular(staged, images)
                if staged.stat().st_size != record["size"] or _sha256(staged) != record["sha256"]:
                    raise BackupError(f"Временная копия для восстановления повреждена: {name}")
            board_spec, entries = _parse_nvidia_map(images)
            _require_devices(images, entries, metadata["scope"])
            if board_spec != metadata["board_spec"]:
                raise BackupError("board_spec временной копии изменился.")
        yield images
    finally:
        if images.exists():
            shutil.rmtree(images)
        if had_images:
            os.replace(saved, images)
