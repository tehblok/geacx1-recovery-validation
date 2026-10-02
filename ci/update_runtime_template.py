#!/usr/bin/env python3
"""Generated self-contained GEACX1 runtime update. Does not flash hardware."""
import argparse
import base64
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import sys
import tempfile

PAYLOAD = '__PAYLOAD__'


def records():
    return json.loads(gzip.decompress(base64.b64decode(PAYLOAD)))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_target(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Недопустимый путь обновления.')
    path = root / relative
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Обновление остановлено: ссылка ' + name)
    if path.exists() and not path.is_file():
        raise ValueError('Ожидался обычный файл: ' + name)
    if not path.parent.is_dir():
        raise ValueError('Отсутствует каталог: ' + str(path.parent))
    return path


def replace_file(path, data, info):
    fd, temporary = tempfile.mkstemp(prefix='.geacx1-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else 0o644)
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_update(kit, keep_backup=True):
    root = Path(kit).resolve()
    manifest = safe_target(root, 'SHA256SUMS')
    original = manifest.read_bytes()
    text = original.decode('utf-8')
    changes = []
    for name, record in records().items():
        path = safe_target(root, name)
        before = path.read_bytes() if path.exists() else None
        old_hash = digest(before) if before is not None else None
        after = base64.b64decode(record['data'])
        if digest(after) != record['sha256']:
            raise ValueError('Повреждён сам файл обновления: ' + name)
        if old_hash not in [*record['before'], record['sha256']]:
            raise ValueError('Неизвестная версия или файл изменён вручную: ' + name + '. Ничего не обновлено.')
        if name.endswith('.py'):
            compile(after, name, 'exec')
        pattern = re.compile(r'(?m)^([0-9a-fA-F]{64})([ \t]+[ *])' + re.escape(name) + r'$')
        entries = list(pattern.finditer(text))
        if not entries and None in record['before']:
            # A power loss can leave a newly added file before the manifest.
            if text and not text.endswith('\n'):
                text += '\n'
            text += record['sha256'] + '  ' + name + '\n'
        else:
            known_hashes = [*record['before'], record['sha256']]
            # Reconcile only known old/new combinations after an interrupted
            # atomic per-file replacement. Unknown contents still fail above.
            if len(entries) != 1 or entries[0].group(1).lower() not in known_hashes:
                raise ValueError('SHA256SUMS не соответствует файлу: ' + name)
            text = pattern.sub(lambda m: record['sha256'] + m.group(2) + name, text)
        if before != after:
            changes.append((path, before, after, path.stat() if before is not None else root.stat()))
    if text.encode() != original:
        changes.append((manifest, original, text.encode(), manifest.stat()))
    if not changes:
        return None
    backup = Path(tempfile.mkdtemp(prefix='runtime-update-original-', dir=root)) if keep_backup else None
    if backup:
        for path, before, _after, _info in changes:
            if before is not None:
                saved = backup / path.relative_to(root)
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(before)
    committed = []
    try:
        for path, before, after, info in changes:
            replace_file(path, after, info)
            committed.append((path, before, info))
    except BaseException as original_error:
        rollback_errors = []
        for path, before, info in reversed(committed):
            try:
                if before is None:
                    path.unlink()
                else:
                    replace_file(path, before, info)
            except OSError as error:
                rollback_errors.append(str(path) + ': ' + str(error))
        if rollback_errors:
            raise OSError('Откат завершён частично. После устранения ошибки диска повторите обновление. '
                          'Исходники: ' + str(backup) + '. ' + '; '.join(rollback_errors)) from original_error
        raise
    return backup


@contextlib.contextmanager
def wizard_lock():
    fd = os.open('/run/lock/geacx1-recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Закройте мастер через 0 → 0, затем повторите обновление.')
        yield
    finally:
        os.close(fd)


def find_kit():
    home = Path(pwd.getpwnam(os.environ['SUDO_USER']).pw_dir) if os.environ.get('SUDO_USER') else Path.home()
    candidates = [Path.cwd(), home / 'geacx1-kit-v1.4/geacx1-recovery']
    found = []
    for path in candidates:
        if (path / 'wizard.py').is_file() and (path / 'SHA256SUMS').is_file() and path.resolve() not in found:
            found.append(path.resolve())
    if len(found) == 1:
        return found[0]
    if found:
        for index, path in enumerate(found, 1):
            print(f'{index}: {path}')
        answer = input('Номер используемого комплекта: ').strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer)-1]
        raise ValueError('Папка не выбрана.')
    value = input('Полный путь к установленному комплекту v1.4, где находится wizard.py: ').strip()
    if value.startswith('~/'):
        return home / value[2:]
    return Path(value)


def main():
    parser = argparse.ArgumentParser(description='Добавить повтор готовых образов и очистку в установленный GEACX1 v1.4.')
    parser.add_argument('kit', nargs='?', type=Path)
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise ValueError('На Ubuntu запустите: sudo python3 UPDATE_RECOVERY.py')
    with wizard_lock():
        kit = (args.kit or find_kit()).resolve()
        backup = apply_update(kit)
    print('Обновление установлено. Подготовленная система и данные платы не изменены.')
    if backup:
        print('Исходные файлы сохранены:', backup)
    print('Запуск: bash ' + shlex.quote(str(kit / 'START.sh')))
    print('Меню 8 — повтор готового проверенного образа; меню 9 — очистка промежуточных файлов.')
    print('Первый образ зарегистрируется после обычной успешной записи через меню 2. Старые образы без записи проверки не принимаются.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print('Обновление не выполнено:', error, file=sys.stderr)
        sys.exit(1)
