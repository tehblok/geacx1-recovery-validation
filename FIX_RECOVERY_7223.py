#!/usr/bin/env python3
"""Small offline PID fix for original GEACX1 kits v1.2/v1.3/v1.4. Never flashes."""
import argparse
import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import shlex
import sys
import tempfile

KNOWN = {'wizard.py': [['v1.2', '8520c84645af64dcbb9094ec816ef14c2692ce32421d7ec092d50d187b6cf499', 'e49f033e9e5917edb94abf779bae47aa9782b756eafe14f5ad27d0f813cdfa97'], ['v1.3', '9f5fcdc9d61d872c20fc6a8d8840c40551344429cd45c88050da07c213895d39', 'e54aaaf8528b12fb593661ee10e522cdca2d51f7e0076455f666092c00858861'], ['v1.4', '7450b526e2ec4d546eea44c5567e8d5f8935372a1112a7a4f97d9a853650f037', '3b2eb33b4a297327de181102de8bebe52101279be14ba99ac551d54ffed5148e']], 'lib/recovery_core.py': [['v1.2', '8b6dcf21cbf458bf00aa8421b7fa7e8bd1ef6d0e6eb0f004c893c1d7f14cce10', '4a5e4ffa2fbc2a4db832b2f5d0573f45a093d6a2abc092dda8ae5f39ec822fb9'], ['v1.3', 'd072ff4589cab777d875f6980ba473a61fa2103c21b231468f80ea9188e890a8', 'f3bbe84a509548dfb3a4cd0b95012bb8a18f112a997717fd28db6dbb8bca9028'], ['v1.4', '81af5fba17a7c7dcb32cee97cbc5b2b7af6b80bbe705e470dbdbe7996c17e93c', '040e249a7df7949fc78b1c2f6e627cb217e5e2f3d331e7d974fc68760d72575e']]}

def transform(name,data):
 s=data.decode()
 if name=='lib/recovery_core.py':
  s=s.replace('SYS_USB_DEVICES = Path("/sys/bus/usb/devices")\n','SYS_USB_DEVICES = Path("/sys/bus/usb/devices")\n# NVIDIA Quick Start: 7223 is production P3701-0004 (32 GB); 7023 is\n# the broader AGX Orin recovery family. EEPROM remains the final SKU gate.\nAGX_ORIN_RECOVERY_PIDS = frozenset(("7023", "7223"))\n')
  s=s.replace('product_id == "7023"','product_id in AGX_ORIN_RECOVERY_PIDS')
 else:
  s=s.replace(".get('product_id') == '7023'", ".get('product_id') in core.AGX_ORIN_RECOVERY_PIDS")
  s=s.replace(".get('product_id') != '7023'", ".get('product_id') not in core.AGX_ORIN_RECOVERY_PIDS")
  s=s.replace('Нужно ровно одно AGX Orin в Recovery (0955:7023).','Нужно ровно одно AGX Orin в Recovery (0955:7223 или 0955:7023). SKU 3701/0004 проверит EEPROM.')
 return s.encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _replace(path, data, info):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.7223-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, info.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_fix(kit, keep_backup=True):
    kit = Path(kit).resolve()
    manifest = kit / 'SHA256SUMS'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Не найден обычный файл SHA256SUMS. Укажите папку распакованного комплекта, где лежит wizard.py.')
    original_manifest = manifest.read_bytes()
    text = original_manifest.decode('utf-8')
    changes = []
    for name, records in KNOWN.items():
        path = kit / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(kit):
            raise ValueError('Неверный путь: ' + name)
        before = path.read_bytes()
        digest = sha(before)
        matched = [entry for entry in records if digest in entry[1:]]
        if not matched:
            raise ValueError('Файл изменён или версия не поддерживается: ' + name + '. Ничего не обновлено.')
        version, old_hash, new_hash = matched[0]
        after = transform(name, before) if digest == old_hash else before
        if sha(after) != new_hash:
            raise ValueError('Исправление не совпало с проверенной версией: ' + name)
        compile(after, name, 'exec')
        pattern = re.compile(r'(?m)^([0-9a-fA-F]{64})([ \t]+[ *])' + re.escape(name) + r'$')
        entries = list(pattern.finditer(text))
        if len(entries) != 1 or entries[0].group(1).lower() not in (old_hash, new_hash):
            raise ValueError('Манифест не соответствует исходному файлу: ' + name)
        text = pattern.sub(lambda m: new_hash + m.group(2) + name, text)
        if after != before:
            changes.append((path, before, after, path.stat()))
    if text.encode() != original_manifest:
        changes.append((manifest, original_manifest, text.encode(), manifest.stat()))
    if not changes:
        return None
    backup = None
    if keep_backup:
        backup = Path(tempfile.mkdtemp(prefix='usb-7223-original-', dir=kit))
        for path, before, _after, info in changes:
            saved = backup / path.relative_to(kit)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(before)
            saved.chmod(info.st_mode & 0o7777)
    committed = []
    try:
        for path, before, after, info in changes:
            _replace(path, after, info)
            committed.append((path, before, info))
    except BaseException:
        for path, before, info in reversed(committed):
            _replace(path, before, info)
        raise
    return backup


@contextlib.contextmanager
def wizard_lock():
    fd = os.open('/run/lock/geacx1-recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Мастер ещё открыт. На экране Recovery введите 0, затем 0 в главном меню. Повторите исправление.')
        yield
    finally:
        os.close(fd)


def find_kit():
    home = Path(pwd.getpwnam(os.environ['SUDO_USER']).pw_dir) if os.environ.get('SUDO_USER') else Path.home()
    candidates = [Path.cwd(), *[home / ('geacx1-kit-' + v) / 'geacx1-recovery' for v in ('v1.4', 'v1.3', 'v1.2')]]
    found = []
    for candidate in candidates:
        if (candidate / 'wizard.py').is_file() and (candidate / 'SHA256SUMS').is_file() and candidate.resolve() not in found:
            found.append(candidate.resolve())
    if len(found) == 1:
        return found[0]
    if found:
        for index, path in enumerate(found, 1):
            print(str(index) + ': ' + str(path))
        answer = input('Номер используемой папки: ').strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer)-1]
        raise ValueError('Папка не выбрана.')
    return Path(input('Путь к папке комплекта с wizard.py: ').strip()).expanduser()


def main():
    parser = argparse.ArgumentParser(description='Исправить распознавание Recovery 0955:7223, без прошивки и повторной сборки.')
    parser.add_argument('kit', nargs='?', type=Path)
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise ValueError('Запустите на Ubuntu: sudo python3 FIX_RECOVERY_7223.py')
    with wizard_lock():
        kit = (args.kit or find_kit()).expanduser().resolve()
        backup = apply_fix(kit)
    print('Готово: 0955:7223 разрешён. Проверка EEPROM 3701/0004 сохранена.')
    if backup:
        print('Оригиналы сохранены:', backup)
    print('Подготовленная система, last-work.json и данные платы не изменены.')
    print('Запустите: bash ' + shlex.quote(str(kit / 'START.sh')))
    print('Выберите пункт 2 — продолжить с подготовленной системой; затем нужный накопитель.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print('Исправление не выполнено:', error, file=sys.stderr)
        sys.exit(1)
