"""Reuse only the last successfully generated system image; never flash here."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import recovery_core as core

RECEIPT = '.geacx1-reuse.json'
GIB = 1024 ** 3
_UUID_NAME = re.compile(r'l4t-rootfs-uuid\.txt(?:_b)?(?:_ext)?(?:_enc)?$')


def _regular(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts:
        raise core.RecoveryError('Недопустимый путь готового образа.')
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise core.RecoveryError(f'Готовые образы не должны быть ссылками: {name}')
    try:
        info = path.stat()
    except OSError as exc:
        raise core.RecoveryError(f'Нет готового файла: {name}. Нужна обычная сборка образов.') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise core.RecoveryError(f'Ожидался непустой обычный файл: {name}')
    return path, info


def _hash_file(root, name):
    path, before = _regular(root, name)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise core.RecoveryError(f'Файл менялся во время проверки: {name}')
    return {'sha256': digest.hexdigest(), 'size': after.st_size}


def _files(root, mode):
    if mode not in ('emmc', 'nvme'):
        raise core.RecoveryError('Повтор готового образа доступен для eMMC или NVMe.')
    names = {'bootloader/system.img'}
    if mode == 'nvme':
        names.add('bootloader/system.img.raw')
    for path in (root / 'bootloader').glob('l4t-rootfs-uuid.txt*'):
        if not _UUID_NAME.fullmatch(path.name):
            raise core.RecoveryError('Неизвестный файл UUID: ' + path.name)
        names.add('bootloader/' + path.name)
    if mode == 'nvme' and 'bootloader/l4t-rootfs-uuid.txt_ext' not in names:
        raise core.RecoveryError('Нет сохранённого UUID внешней системы; нужна обычная сборка NVMe.')
    return sorted(names)


def invalidate(l4t):
    """Call before any fresh flash/build, which can overwrite the shared image."""
    path = Path(l4t) / RECEIPT
    if path.is_symlink():
        raise core.RecoveryError('Файл сведений о готовом образе оказался ссылкой.')
    path.unlink(missing_ok=True)


def record_success(l4t, mode):
    root = Path(l4t).resolve()
    core.validate_prepared(root)
    files = {name: _hash_file(root, name) for name in _files(root, mode)}
    data = {'format': 1, 'release': 'R39.2.0', 'board': core.BOARD_NAME,
            'mode': mode, 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'prepared': _hash_file(root, '.geacx1-prepared.json')['sha256'], 'files': files}
    target = root / RECEIPT
    if target.is_symlink():
        raise core.RecoveryError('Файл сведений о готовом образе оказался ссылкой.')
    fd, temporary = tempfile.mkstemp(prefix='.reuse-', dir=root)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def _validate(l4t, mode, *, refreshed_raw=False):
    root = Path(l4t).resolve()
    core.validate_prepared(root)
    receipt, info = _regular(root, RECEIPT)
    if info.st_size > 64 * 1024:
        raise core.RecoveryError('Сведения о готовом образе повреждены.')
    try:
        data = json.loads(receipt.read_text())
    except (ValueError, OSError) as exc:
        raise core.RecoveryError('Не удалось прочитать сведения о готовом образе.') from exc
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in (
            ('format', 1), ('release', 'R39.2.0'), ('board', core.BOARD_NAME), ('mode', mode))):
        raise core.RecoveryError('Готовый образ относится к другому режиму/плате. Нужна обычная сборка выбранного режима.')
    if data.get('prepared') != _hash_file(root, '.geacx1-prepared.json')['sha256']:
        raise core.RecoveryError('Подготовленная система изменилась после создания образа.')
    records = data.get('files')
    if not isinstance(records, dict) or set(records) != set(_files(root, mode)):
        raise core.RecoveryError('Набор готовых образов или файлов UUID изменился.')
    for name in sorted(records):
        if refreshed_raw and mode == 'nvme' and name == 'bootloader/system.img.raw':
            _regular(root, name)
            continue
        if records[name] != _hash_file(root, name):
            raise core.RecoveryError(f'Готовый файл изменён: {name}. Повторная запись запрещена.')
    return data


def validate(l4t, mode):
    return _validate(l4t, mode)


def refresh_after_repeat(l4t, mode):
    # NVIDIA may run fsck/resize2fs on NVMe's raw packaging input. Only after
    # its successful exit allow that generated file to change; system.img,
    # UUIDs and prepared inputs must still match the pre-flash receipt.
    _validate(l4t, mode, refreshed_raw=True)
    return record_success(l4t, mode)


def build_command(mode):
    command = core.build_command(Path('.'), mode)
    if mode not in ('emmc', 'nvme'):
        raise core.RecoveryError('Для QSPI используйте обычный режим.')
    command.insert(1, '-r')
    return command


def required_free_bytes(l4t, mode):
    if mode == 'emmc':
        return 8 * GIB
    if mode != 'nvme':
        raise core.RecoveryError('Неизвестный режим готового образа.')
    # NVMe regenerates the transfer package, possibly including tar/zstd copies.
    # st_blocks measures allocated data in a sparse raw file, not its disk size.
    allocated = sum(_regular(Path(l4t), name)[1].st_blocks * 512
                    for name in ('bootloader/system.img', 'bootloader/system.img.raw'))
    return max(12 * GIB, 2 * allocated + 8 * GIB)


def protected_cleanup_paths(mode):
    return {'bootloader/system.img.raw'} if mode == 'nvme' else set()


def isolate_board_spec(l4t):
    """Make NVIDIA query the current board rather than source an old board.spec."""
    root = Path(l4t).resolve()
    path = root / 'bootloader/board.spec'
    if not path.exists() and not path.is_symlink():
        return
    _regular(root, 'bootloader/board.spec')
    previous = root / 'bootloader/board.spec.previous'
    if previous.is_symlink():
        raise core.RecoveryError('Небезопасный путь прежних сведений о плате.')
    os.replace(path, previous)
