"""Plan and execute narrowly scoped cleanup of generated flash artifacts."""

import os
from pathlib import Path
import re
import stat


MOUNTINFO = Path("/proc/self/mountinfo")
LOOP_SYSFS = Path("/sys/block")

# These are rootfs staging files created by NVIDIA's flash workflows.  The
# reusable system.img, signed data and image directories are deliberately not
# cleanup targets.
_ALLOWED = (
    "bootloader/system.img.raw",
    "tools/kernel_flash/images/internal/system.img.raw",
    "tools/kernel_flash/images/external/system.img.raw",
)
_ENTRY_FIELDS = frozenset(("path", "allocated_bytes", "size", "device", "inode", "mtime_ns"))


class GeneratedCleanupError(ValueError):
    """The cleanup target or a previously displayed plan is no longer safe."""


def _absolute(path):
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError, OSError) as exc:
        raise GeneratedCleanupError("Неверный путь Linux_for_Tegra.") from exc


def _lstat(path, *, missing_ok=False):
    try:
        return path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GeneratedCleanupError(f"Ожидаемый путь очистки исчез: {path}") from None
    except OSError as exc:
        raise GeneratedCleanupError(f"Не удалось безопасно проверить: {path}") from exc


def _validate_root(l4t):
    root = _absolute(l4t)
    root_stat = _lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise GeneratedCleanupError("Linux_for_Tegra должен быть обычным каталогом, а не symlink.")
    try:
        if root.resolve(strict=True) != root:
            raise GeneratedCleanupError("Путь Linux_for_Tegra проходит через symlink.")
    except (OSError, RuntimeError) as exc:
        raise GeneratedCleanupError("Не удалось безопасно разрешить путь Linux_for_Tegra.") from exc
    marker = root / ".geacx1-prepared.json"
    marker_stat = _lstat(marker)
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        raise GeneratedCleanupError("Нет обычного marker .geacx1-prepared.json подготовленной сборки.")
    return root


def _candidate_stat(root, relative, *, missing_ok):
    mountpoints = _mountpoints()
    current = root
    for component in Path(relative).parts[:-1]:
        current = current / component
        current_stat = _lstat(current, missing_ok=True)
        if current_stat is None:
            if missing_ok:
                return None
            raise GeneratedCleanupError(f"Родитель файла очистки исчез: {current}")
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise GeneratedCleanupError(f"Путь очистки проходит через небезопасный родитель: {current}")
        if str(current) in mountpoints:
            raise GeneratedCleanupError(f"Родитель файла очистки является mountpoint: {current}")
    target = root / relative
    target_stat = _lstat(target, missing_ok=missing_ok)
    if target_stat is None:
        return None
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise GeneratedCleanupError(f"Ожидался обычный raw-файл: {target}")
    if target_stat.st_nlink != 1:
        raise GeneratedCleanupError(f"Raw-файл имеет hardlink и не гарантирует освобождение места: {target}")
    if str(target) in mountpoints:
        raise GeneratedCleanupError(f"Файл очистки является mountpoint: {target}")
    if str(target) in _loop_backing_files():
        raise GeneratedCleanupError(f"Raw-файл используется активным loop-устройством: {target}")
    return target_stat


def _mountpoints():
    try:
        lines = MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # Non-Linux development hosts have no mountinfo; supported runtime
        # validation is performed by the caller before this module is used.
        return set()
    except (OSError, UnicodeError) as exc:
        raise GeneratedCleanupError("Не удалось проверить mountpoints перед очисткой.") from exc
    result = set()
    for line in lines:
        fields = line.split(" - ", 1)[0].split()
        if len(fields) < 5:
            raise GeneratedCleanupError("Неверный формат /proc/self/mountinfo.")
        result.add(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4]))
    return result


def _decode_kernel_path(value):
    value = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), value)
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _loop_backing_files():
    try:
        backing_files = list(LOOP_SYSFS.glob("loop*/loop/backing_file"))
    except OSError as exc:
        raise GeneratedCleanupError("Не удалось проверить loop-устройства перед очисткой.") from exc
    result = set()
    for backing_file in backing_files:
        try:
            value = backing_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # A loop device may be detached during enumeration.
            continue
        except (OSError, UnicodeError) as exc:
            raise GeneratedCleanupError(f"Не удалось прочитать loop backing file: {backing_file}") from exc
        if value:
            result.add(os.path.normpath(_decode_kernel_path(value)))
    return result


def _entry(relative, file_stat):
    return {
        "path": relative,
        "allocated_bytes": file_stat.st_blocks * 512,
        "size": file_stat.st_size,
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mtime_ns": file_stat.st_mtime_ns,
    }


def _protected_paths(paths):
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise GeneratedCleanupError("Защищённые пути должны быть списком точных raw-файлов.")
    try:
        requested = {os.fspath(path) for path in paths}
    except (TypeError, ValueError) as exc:
        raise GeneratedCleanupError("Неверный список защищённых raw-файлов.") from exc
    if not requested.issubset(_ALLOWED):
        raise GeneratedCleanupError("Защитить можно только точный путь из allowlist очистки.")
    return [relative for relative in _ALLOWED if relative in requested]


def plan_cleanup(l4t, *, protected_paths=()):
    """Return a displayable plan for removable generated rootfs raw images."""
    root = _validate_root(l4t)
    protected = _protected_paths(protected_paths)
    entries = []
    for relative in _ALLOWED:
        if relative in protected:
            continue
        file_stat = _candidate_stat(root, relative, missing_ok=True)
        if file_stat is not None:
            entries.append(_entry(relative, file_stat))
    return {
        "l4t": str(root),
        "entries": entries,
        "protected_paths": protected,
        "allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
    }


def _validate_plan(root, plan):
    if not isinstance(plan, dict) or plan.get("l4t") != str(root):
        raise GeneratedCleanupError("План очистки относится к другой сборке.")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise GeneratedCleanupError("В плане очистки нет списка файлов.")
    protected = plan.get("protected_paths")
    if not isinstance(protected, list) or protected != _protected_paths(protected):
        raise GeneratedCleanupError("Список защищённых путей изменён.")
    seen = set()
    actual_entries = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise GeneratedCleanupError("Запись плана очистки изменена.")
        relative = entry.get("path")
        if relative not in _ALLOWED or relative in protected or relative in seen:
            raise GeneratedCleanupError("План содержит неразрешённый или повторный путь.")
        seen.add(relative)
        file_stat = _candidate_stat(root, relative, missing_ok=False)
        actual = _entry(relative, file_stat)
        if entry != actual:
            raise GeneratedCleanupError(f"Файл изменился после предпросмотра: {relative}")
        actual_entries.append(actual)
    allocated = sum(entry["allocated_bytes"] for entry in actual_entries)
    if type(plan.get("allocated_bytes")) is not int or plan["allocated_bytes"] != allocated:
        raise GeneratedCleanupError("Итоговый размер плана очистки изменён.")
    return actual_entries


def execute_cleanup(l4t, plan):
    """Revalidate an approved plan completely, then unlink its exact files."""
    root = _validate_root(l4t)
    entries = _validate_plan(root, plan)
    # Repeat the complete check immediately before the first mutation.  In
    # particular, a stale directory or symlink causes no partial cleanup.
    entries = _validate_plan(root, plan)
    for entry in entries:
        (root / entry["path"]).unlink()
    return {
        "l4t": str(root),
        "entries": [{**entry, "deleted": True} for entry in entries],
        "protected_paths": list(plan["protected_paths"]),
        "allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
    }
