#!/usr/bin/env python3
"""Assemble the exact tested release from the complete GitHub project ZIP."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
ARCHIVE = 'GEACX1-JP7.2-Recovery-v1.2-Full.tar.gz'
SHA256 = '248b91f6165b3a24d05f9944f358dd8d4954aee3bd705b0ea78251c3ba3c7bb0'
SIZE = 3514758836
EXTRA_MANIFEST_SHA256 = '19a7ba2e2da0853068150ec3e85f2655eaaee755426959fcae0464eaa23be471'

class DeliveryError(Exception):
    pass

def load_manifest(root=ROOT):
    data = json.loads((root / 'parts.json').read_text())
    if not isinstance(data, dict) or (data.get('archive'), data.get('sha256'), data.get('size'), data.get('directory')) != (
            ARCHIVE, SHA256, SIZE, 'geacx1-recovery'):
        raise DeliveryError('parts.json не соответствует проверенному комплекту v1.2.')
    parts = data.get('parts', [])
    if not isinstance(parts, list) or not parts:
        raise DeliveryError('Некорректный список частей в parts.json.')
    for index, part in enumerate(parts, 1):
        if (not isinstance(part, dict) or part.get('name') != f'{ARCHIVE}.{index:03d}'
                or not isinstance(part.get('size'), int) or part['size'] <= 0
                or not re.fullmatch('[0-9a-f]{64}', str(part.get('sha256')))):
            raise DeliveryError('Некорректная часть в parts.json.')
    if sum(part['size'] for part in parts) != SIZE:
        raise DeliveryError('Суммарный размер частей не соответствует релизу.')
    return data

def load_extra_manifest(root=ROOT):
    raw = (root / 'extra-manufacturer.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXTRA_MANIFEST_SHA256:
        raise DeliveryError('Нарушена целостность списка дополнительных файлов производителя.')
    return json.loads(raw)

def check_parts(root, manifest, part_directory='payload'):
    for part in manifest['parts']:
        path = root / part_directory / part['name']
        if not path.is_file():
            raise DeliveryError(f'Не найдена часть {part["name"]}. Скачайте весь проект через Code → Download ZIP и распакуйте полностью.')
        if path.stat().st_size != part['size']:
            raise DeliveryError(f'Неверный размер {part["name"]}. Скачивание или распаковка проекта не завершены.')

def combine(root, manifest, output=None, part_directory='payload'):
    """Hash every byte actually copied; validate parts and whole archive."""
    check_parts(root, manifest, part_directory)
    whole = hashlib.sha256()
    written = 0
    for index, part in enumerate(manifest['parts'], 1):
        digest = hashlib.sha256()
        size = 0
        with (root / part_directory / part['name']).open('rb') as source:
            while chunk := source.read(8 * 1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                whole.update(chunk)
                if output is not None:
                    output.write(chunk)
        if size != part['size'] or digest.hexdigest() != part['sha256']:
            raise DeliveryError(f'Повреждена часть {part["name"]}: SHA-256 не совпал. Повторно скачайте проект.')
        written += size
        print(f'  Проверено {index}/{len(manifest["parts"])} частей — {written * 100 // manifest["size"]}%', flush=True)
    if written != manifest['size'] or whole.hexdigest() != manifest['sha256']:
        raise DeliveryError('SHA-256 полного архива не совпал. Распаковка отменена.')

def restore_extras(root, records, staging=None):
    output_dir = staging / 'manufacturer-extra' if staging is not None else None
    if output_dir is not None:
        output_dir.mkdir()
    for record in records:
        print('Дополнительный исходный пакет: ' + record['name'], flush=True)
        context = (output_dir / record['name']).open('xb') if output_dir else contextlib.nullcontext(None)
        with context as output:
            combine(root, record, output, part_directory='manufacturer-extra')
    if output_dir is not None:
        shutil.copyfile(root / 'manufacturer-extra/README_RU.txt', output_dir / 'README_RU.txt')

def check_host(destination):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise DeliveryError('Запускайте на компьютере Ubuntu 24.04 Intel/AMD. Для проверки частей без распаковки: bash START.sh --verify-only')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
        raise DeliveryError('Этот стартовый сценарий рассчитан на Ubuntu 24.04.')
    if os.geteuid() == 0:
        raise DeliveryError('Запустите bash START.sh от обычного пользователя, без sudo. Основной мастер сам запросит пароль.')
    for command in ('tar', 'sha256sum', 'findmnt', 'bash'):
        if not shutil.which(command):
            raise DeliveryError(f'Не найдена системная команда {command}.')
    parent = destination.parent
    while not parent.exists():
        parent = parent.parent
    fs = subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(parent)], text=True).strip()
    if fs != 'ext4':
        raise DeliveryError(f'Для комплекта выберите папку на ext4; сейчас {fs}. Пример: bash START.sh --destination /путь-на-ext4/geacx1-kit-v1.2')
    if shutil.disk_usage(parent).free < 12 * 1024**3:
        raise DeliveryError('Для объединения и распаковки нужно минимум 12 ГиБ свободно. Для дальнейшей подготовки прошивки мастер потребует ещё 80 ГиБ.')

def unpack(root, manifest, destination, extras=None):
    """Publish a verified kit only after complete extraction; never overwrite."""
    if destination.exists() or destination.is_symlink():
        raise DeliveryError(f'Папка уже существует: {destination}. Она не изменена. Если комплект уже готов, откройте {destination / "geacx1-recovery"} и запустите bash START.sh. Либо задайте новую папку через --destination.')
    check_parts(root, manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.geacx1-unpack-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive = staging / manifest['archive']
        print('\n1/3 — Проверка и объединение файлов проекта. Плата пока не нужна.', flush=True)
        with archive.open('xb') as output:
            combine(root, manifest, output)
        print('\n2/3 — Распаковка файлов NVIDIA и производителя. Это может занять несколько минут.', flush=True)
        subprocess.run(['tar', '--extract', '--gzip', '--file', str(archive),
                        '--directory', str(staging), '--no-same-owner'], check=True)
        kit = staging / manifest['directory']
        print('\n3/3 — Проверка всех распакованных файлов по SHA-256.', flush=True)
        log = staging / 'unpack-verification.log'
        with log.open('w') as stream:
            completed = subprocess.run(['sha256sum', '--check', '--strict', 'SHA256SUMS'],
                                       cwd=kit, stdout=stream, stderr=subprocess.STDOUT)
        if completed.returncode:
            detail = '\n'.join(log.read_text(errors='replace').splitlines()[-12:])
            raise DeliveryError('Распакованные файлы не прошли проверку. Мастер не запущен.\n' + detail)
        links = json.loads((kit / 'docs/SYMLINKS.json').read_text())
        if any(not (kit / name).is_symlink() or os.readlink(kit / name) != target for name, target in links.items()):
            raise DeliveryError('При распаковке нарушены символические ссылки. Мастер не запущен.')
        if extras is not None:
            print('\nСохраняю дополнительные исходные пакеты производителя. Они не устанавливаются.', flush=True)
            restore_extras(root, extras, staging)
        if destination.exists() or destination.is_symlink():
            raise DeliveryError('Папка назначения появилась во время работы; она не изменена.')
        staging.rename(destination)
    return destination / manifest['directory']

def main():
    parser = argparse.ArgumentParser(description='Подготовить полный GEACX1 Recovery из ZIP проекта GitHub.')
    parser.add_argument('--verify-only', action='store_true', help='Только проверить части, без распаковки и запуска.')
    parser.add_argument('--unpack-only', action='store_true', help='Распаковать и проверить, не запускать мастер.')
    parser.add_argument('--destination', type=Path, default=Path.home() / 'geacx1-kit-v1.2', help='Новая папка на ext4 (по умолчанию ~/geacx1-kit-v1.2).')
    args = parser.parse_args()
    print('GEACX1 / JetPack 7.2 — полный комплект из проекта GitHub', flush=True)
    manifest = load_manifest()
    extras = load_extra_manifest()
    if args.verify_only:
        combine(ROOT, manifest)
        restore_extras(ROOT, extras)
        print('Все части и полный архив совпадают с проверенным релизом v1.2.')
        return
    destination = args.destination.expanduser().resolve()
    check_host(destination)
    kit = unpack(ROOT, manifest, destination, extras)
    print(f'\nКомплект готов: {kit}\nИнструкция с картинками: {ROOT / "docs/GEACX1-GUIDE-RU.pdf"}', flush=True)
    if args.unpack_only:
        print(f'Для запуска откройте {kit} в терминале и выполните bash START.sh')
        return
    print('\nЗапускаю мастер. Для восстановления с нуля выберите пункт 1.\nЗапись на плату начнётся только после вашего отдельного подтверждения.', flush=True)
    os.execvp('bash', ['bash', str(kit / 'START.sh')])

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено пользователем. Прошивка не запускалась.', file=sys.stderr)
        sys.exit(130)
    except (DeliveryError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'\nОШИБКА: {error}', file=sys.stderr)
        sys.exit(1)
