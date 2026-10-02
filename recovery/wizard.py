#!/usr/bin/env python3
"""Русский мастер GEACX1. Все команды прошивки доступны только интерактивно."""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import selectors
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'lib'))
import recovery_core as core
import backup_restore as backup
import usb_support
import reusable_images as reusable
import generated_cleanup

LAST_WORK = BASE / 'last-work.json'

MODES = {
    'emmc': ('Полное восстановление: QSPI + eMMC', 'ERASE GEACX1 32GB'),
    'qspi': ('Только QSPI / загрузочная цепочка', 'FLASH QSPI GEACX1 32GB'),
    'nvme': ('QSPI + NVMe (расширенный режим)', 'ERASE NVME GEACX1 32GB'),
}
COLOR = sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def paint(s, color='36'):
    return f'\033[{color}m{s}\033[0m' if COLOR else s


def screen(title, step=''):
    if COLOR:
        print('\033[2J\033[H', end='')
    print(paint('╔══════════════════════════════════════════════════════════════╗'))
    print(paint('║  GEACX1 RECOVERY    ·    32 ГБ    ·    JETPACK 7.2            ║'))
    print(paint('╚══════════════════════════════════════════════════════════════╝'))
    print(paint(f'\n{title}', '1;37'))
    if step:
        print(paint(step, '90'))
    print()


def pause():
    try:
        input('\nНажмите Enter, чтобы вернуться в меню… ')
    except (EOFError, KeyboardInterrupt):
        print()


def ask(prompt, default=''):
    val = input(f'{prompt}' + (f' [{default}]' if default else '') + ': ').strip()
    return val or default


def choose(title, options):
    while True:
        screen(title)
        for key, value in options:
            print(f'  {paint(key, "1;36")}  {value}')
        try:
            value = ask('\nВаш выбор')
        except (EOFError, KeyboardInterrupt):
            print('\nВозврат в предыдущее меню.')
            return '0'
        if value in dict(options):
            return value
        print('Введите номер из меню.')
        time.sleep(.5)


def confirmation_matches(value, mode):
    if mode not in MODES:
        raise ValueError('Неизвестный режим')
    return value.strip() == MODES[mode][1]


def ask_erase(mode):
    print(paint('ВНИМАНИЕ: запись прошивки необратимо заменит выбранные разделы.', '1;33'))
    if mode == 'emmc':
        print('Будут заменены QSPI, загрузчик, ядро, DTB и система на eMMC.\nДанные eMMC будут потеряны. NVMe отдельно не очищается.')
    elif mode == 'nvme':
        print('Будут заменены QSPI и разделы NVMe устройства Jetson nvme0n1.\nДанные этого NVMe будут потеряны. eMMC не восстанавливается.')
    else:
        print('Будет заменена загрузочная цепочка QSPI.\nСтарая система на eMMC/NVMe может быть несовместима с JP7.2.')
    print('Отключите лишние Jetson. Не отключайте питание и USB во время записи.')
    print(f'\nДля запуска введите точно: {paint(MODES[mode][1], "1;33")}')
    return confirmation_matches(input('Подтверждение (Enter = отмена): '), mode)


def diagnose(text):
    t = text.lower()
    tips = []
    if any(x in t for x in ('usb', 'tegrarcm', 'rcm_state', 'no device', 'return value 3')):
        tips.append('USB / Recovery: снова зажмите RECV, нажмите RST на 1–3 с, отпустите RST, затем RECV. Используйте Micro-USB кабель данных, прямой USB-порт ПК, без хаба; при повторном сбое смените кабель/порт.')
    if any(x in t for x in ('nfs', 'ssh', 'fc00:', 'waiting for target', 'network is unreachable')):
        tips.append('NFS / USB-сеть: нужна для NVMe и резервных копий. Проверьте USB device mode, IPv6, nfs-kernel-server и правила firewall для изолированного USB-интерфейса fc00:1:1::/48. Мастер не отключает firewall целиком. Чистая прошивка eMMC через пункт 1 не требует NFS.')
    if any(x in t for x in ('no space', 'disk full', 'enospc', 'не хватает места',
                            'не хватает свободного места', 'свободное место', 'свободного места')):
        tips.append('Недостаточно места: освободите минимум 80 ГиБ на ext4, затем создайте новую рабочую папку. Частично созданный образ не используйте.')
    if any(x in t for x in ('permission denied', 'operation not permitted', 'read-only file', 'chown')):
        tips.append('Права/файловая система: запуск через sudo, рабочая папка на ext4. NTFS, exFAT, FUSE, общие папки VM для сборки не подходят.')
    if any(x in t for x in ('exec format', 'qemu', 'chroot', 'binfmt')):
        tips.append('ARM rootfs: проверьте qemu-user-static и binfmt-support на Ubuntu amd64. Повторите установку зависимостей через меню и подготовку в новой папке.')
    if any(x in t for x in ('signature', 'secure boot', 'authentication', 'pkc', 'sbk', 'fuse')):
        tips.append('Secure Boot: плата может требовать ключи владельца PKC/SBK. Перепрошивка не сбрасывает eFuse. Нужны правильные ключи и процедура производителя; не пытайтесь прожигать fuse.')
    if any(x in t for x in ('sku', 'eeprom', 'board id', 'boardid', 'crc', '3701')):
        tips.append('Модель/EEPROM: мастер допускает только P3701-0004 (AGX Orin 32 ГБ). Не подменяйте BOARDID/BOARDSKU и не отключайте проверку EEPROM. При повреждении EEPROM нужна процедура производителя для вашей платы.')
    if any(x in t for x in ('unsupported', 'rootfs', '39.2', 'archive', 'checksum', 'sha256', 'sha-256', 'контрольная сумма')):
        tips.append('Пакеты: нужен именно L4T 39.2.0 / JetPack 7.2, Ubuntu 24.04 aarch64 rootfs. Проверьте SHA256SUMS и скачайте повреждённый архив повторно; JP6/JP4 и 39.2.1 не смешиваются.')
    if any(x in t for x in ('ascii', 'non-ascii', 'кириллиц', 'кириллица', 'path must', 'путь должен')):
        tips.append('Путь рабочей папки: используйте локальный ext4-каталог с латинскими буквами, например /var/tmp/geacx1-work. Не используйте проброс папки из Windows/macOS, NTFS, exFAT или сетевой диск.')
    if any(x in t for x in ('резервной копии', 'geacx1_backup.json', 'nvpartitionmap')):
        tips.append('Резервная копия: укажите всю папку, созданную пунктом 7, с GEACX1_BACKUP.json, nvpartitionmap.txt и всеми образами. Проверить её можно без подключения платы. Неполную копию не используйте для восстановления.')
    if '404' in t and any(x in t for x in ('fae_use', 'geacx1-jp7.2.json', 'tool-deb-install')):
        tips.append('Установщик производителя: на 02.10.2026 конфигурация GEACX1-JP7.2.json отсутствовала (HTTP 404). Используйте локальный комплект JP7.2; не подставляйте JSON или пакеты JP6.2.')
    if any(x in t for x in ('fstab', 'dependency failed for', 'timed out waiting for device')):
        tips.append('После restore на другой диск: сравните UUID в /etc/fstab с lsblk -f на GEACX1. Старый UUID может задерживать загрузку. Не удаляйте все записи fstab; исправляйте только подтверждённое несоответствие. POSTCHECK.sh показывает проверку таблицы монтирования.')
    if 'cpld' in t:
        tips.append('CPLD: полная прошивка Jetson его не меняет. У GEACX1 и GEACX1SC разные пакеты. Для диагностики используйте инструкцию своей ревизии; команды сброса, триггеров и прошивки не являются чтением состояния. Подробнее: меню 5 → 4.')
    if not tips:
        tips.append('Сохраните полный журнал. Повторный запуск возможен после повторного входа в Recovery; подготовку после сбоя выполняйте в новой папке. Успех процесса ещё не подтверждает загрузку устройства.')
    return tips


def run_with_status(label, action):
    """Keep long local validation visibly alive without inventing progress or ETA."""
    result, failure = [], []

    def worker():
        try:
            result.append(action())
        except BaseException as exc:
            failure.append(exc)

    print(label)
    started = time.monotonic()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    ticks = 0
    interrupted = False
    while thread.is_alive():
        try:
            thread.join(.25)
        except KeyboardInterrupt:
            interrupted = True
            print('\nПроверка уже читает файлы и безопасно завершится; дождитесь результата.')
            while thread.is_alive():
                try:
                    thread.join(.25)
                except KeyboardInterrupt:
                    print('\nПроверка файлов всё ещё завершается; результат не будет использован.')
        if thread.is_alive() and COLOR:
            elapsed = int(time.monotonic() - started)
            print(f'\r\033[K  {"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[ticks % 10]} выполняется · {elapsed} с', end='', flush=True)
            ticks += 1
    elapsed = int(time.monotonic() - started)
    if COLOR:
        print('\r\033[K', end='')
    if failure:
        print(f'Остановлено с ошибкой через {elapsed} с.')
        raise failure[0]
    if interrupted:
        print(f'Проверка безопасно завершена через {elapsed} с. Возвращаюсь в меню.')
        raise KeyboardInterrupt
    print(f'Готово за {elapsed} с.')
    return result[0] if result else None


def command_description(argv):
    text = ' '.join(str(x) for x in argv)
    if 'l4t_flash_prerequisites.sh' in text:
        return 'Устанавливаю инструменты сборки на компьютере'
    if 'l4t_update_initrd.sh' in text:
        return 'Собираю загрузочный образ для GEACX1'
    if 'apply_binaries.sh' in text:
        return 'Добавляю драйверы NVIDIA в систему GEACX1'
    if 'l4t_backup_restore.sh' in text and ' -b ' in f' {text} ':
        return 'Читаю все разделы GEACX1 в полную резервную копию'
    if 'l4t_backup_restore.sh' in text and ' -r ' in f' {text} ':
        return 'Восстанавливаю QSPI и накопители GEACX1 из копии'
    if 'flash.sh' in text or 'l4t_initrd_flash.sh' in text:
        return 'Записываю загрузчик и систему в GEACX1'
    return 'Выполняю необходимую команду'


class StopConfirmation:
    """Read cancellation without ever blocking NVIDIA's stdout drain loop."""
    def __init__(self):
        self.active = False
        self.fd = None
        self.buffer = b''

    def request(self):
        print('\nЗапись может быть активна. Для остановки введите STOP и Enter; '
              'пустой Enter — продолжать. Пока вы отвечаете, команда работает.', flush=True)
        if not self.active:
            try:
                self.fd = sys.stdin.fileno()
                self.buffer = b''
                self.active = True
            except (OSError, ValueError, AttributeError):
                print('Ввод недоступен. STOP не подтверждён; продолжаю ждать завершения.', flush=True)

    def poll(self):
        if not self.active:
            return None
        try:
            if not select.select([self.fd], [], [], 0)[0]:
                return None
            data = os.read(self.fd, 4096)
        except (OSError, ValueError):
            data = b''
        self.buffer += data
        if not data or len(self.buffer) > 128:
            answer = False
        elif b'\n' in self.buffer:
            answer = self.buffer.split(b'\n', 1)[0].strip() == b'STOP'
        else:
            return None
        self.active = False
        return answer


class Runner:
    def __init__(self, log, quiet=False):
        self.log = Path(log)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.quiet = quiet
        self.destructive = False

    def note(self, message):
        with self.log.open('a', encoding='utf-8') as log:
            log.write(message + '\n')
        if not self.quiet:
            print(message, flush=True)

    def run(self, argv, cwd):
        argv = [str(a) for a in argv]
        heading = '\n$ ' + shlex.join(argv) + '\n'
        with self.log.open('a', encoding='utf-8') as log:
            log.write(heading)
            log.flush()
            if not self.quiet:
                print('\n' + paint(command_description(argv), '1;36'))
                print('Подробный вывод сохраняется в журнал:', self.log)
            proc = subprocess.Popen(argv, cwd=str(cwd), env=core.sanitized_env(), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            start = time.monotonic()
            last = 'Запуск…'
            pending = b''
            ticks = 0
            last_notice = start
            stop_confirmation = StopConfirmation()
            cancelled = False
            try:
                while sel.get_map() or proc.poll() is None:
                    try:
                        events = sel.select(.15)
                        for key, _ in events:
                            data = os.read(key.fileobj.fileno(), 8192)
                            if not data:
                                sel.unregister(key.fileobj)
                                continue
                            decoded = data.decode('utf-8', errors='replace')
                            log.write(decoded)
                            log.flush()
                            pending += data
                            lines = pending.replace(b'\r', b'\n').split(b'\n')
                            pending = lines.pop()[-65536:]
                            if lines:
                                last = lines[-1].decode('utf-8', errors='replace')
                            elif pending:
                                last = pending.decode('utf-8', errors='replace')[-220:]
                            if not COLOR and not self.quiet:
                                print(decoded, end='', flush=True)
                        if not COLOR and not self.quiet and time.monotonic() - last_notice >= 15:
                            print(f'\n[Команда работает {int(time.monotonic()-start)} с; полный вывод: {self.log}]', flush=True)
                            last_notice = time.monotonic()
                        if stop_confirmation.poll() is True:
                            # Outside the inner KeyboardInterrupt handler: an
                            # explicit STOP must terminate, not reopen the prompt.
                            cancelled = True
                            break
                        if COLOR and not self.quiet and not stop_confirmation.active:
                            width = max(30, shutil.get_terminal_size().columns - 20)
                            # Vendor output cannot inject terminal escape/control sequences.
                            safe = ''.join(c for c in last if c.isprintable()).replace('\x1b', '')
                            print(f'\r\033[K {"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[ticks % 10]} {int(time.monotonic()-start):4d} с  {safe[:width]}', end='', flush=True)
                            ticks += 1
                    except KeyboardInterrupt:
                        if self.destructive:
                            stop_confirmation.request()
                            continue
                        raise
                if cancelled:
                    raise KeyboardInterrupt
                rc = proc.wait()
                if stop_confirmation.active:
                    print('\nКоманда завершилась; запрос STOP больше не действует.', flush=True)
            except BaseException:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                raise
            finally:
                sel.close()
                proc.stdout.close()
                if not self.quiet:
                    print()
            log.write(f'\n[exit={rc}]\n')
            if rc:
                raise core.RecoveryError(f'Команда завершилась с кодом {rc}. Журнал: {self.log}')


@contextlib.contextmanager
def flash_host_state(runner):
    """Временные изменения только на время flash, с восстановлением в finally."""
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'udisks2.service'], check=False).returncode == 0
    try:
        if active:
            runner.run(['systemctl', 'stop', 'udisks2.service'], BASE)
        with usb_support.keep_usb_awake(core.SYS_USB_DEVICES, notify=runner.note):
            yield
    finally:
        if active:
            rc = subprocess.run(['systemctl', 'start', 'udisks2.service'], check=False).returncode
            if rc:
                print('Не удалось вернуть udisks2. Выполните: sudo systemctl start udisks2.service')


@contextlib.contextmanager
def backup_host_state(runner):
    """Provide NVIDIA backup's NFS service and restore the host state afterwards."""
    nfs_active = subprocess.run(
        ['systemctl', 'is-active', '--quiet', 'nfs-kernel-server.service'], check=False
    ).returncode == 0
    if not nfs_active:
        runner.run(['systemctl', 'start', 'nfs-kernel-server.service'], BASE)
    try:
        with flash_host_state(runner):
            yield
    finally:
        if not nfs_active:
            try:
                runner.run(['systemctl', 'stop', 'nfs-kernel-server.service'], BASE)
            except core.RecoveryError:
                print('Не удалось остановить временно запущенный NFS. '
                      'Выполните: sudo systemctl stop nfs-kernel-server.service')


def vendor_path():
    for p in (BASE/'vendor'/core.VENDOR_NAME, BASE.parent/core.VENDOR_NAME):
        if p.is_dir():
            return p
    return BASE/'vendor'/core.VENDOR_NAME


def require_root():
    if os.geteuid() != 0:
        raise core.RecoveryError('Для этого шага запустите sudo bash START.sh. Проверки/демо работают без sudo.')


@contextlib.contextmanager
def exclusive_session():
    """The kernel releases the lock even after a crash; never remove a live lock."""
    path = '/run/lock/geacx1-recovery.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise core.RecoveryError('Другой экземпляр мастера уже работает. Завершите его перед продолжением.') from exc
        yield
    finally:
        os.close(fd)


def host_check_action(check):
    """Return one concrete, non-destructive next step for a failed host check."""
    if check.get('name') == 'Свободное место' and 'required_bytes' in check:
        needed = check['required_bytes'] / 1024**3
        return f'Нужно свободно {needed:.1f} ГиБ. Меню 9 показывает ненужные результаты сборки для очистки.'
    actions = {
        'Linux': 'Запустите комплект на обычном компьютере с Linux; прошивка из macOS или Windows не поддерживается.',
        'Ubuntu': 'Загрузите на компьютере нативную Ubuntu 24.04 или 22.04 и повторите проверку.',
        'Архитектура хоста': 'Используйте компьютер x86_64/amd64; ARM-компьютер и виртуальная машина Apple Silicon не подходят.',
        'Нативный хост': 'Загрузите Ubuntu напрямую на компьютере. WSL и Docker для прошивки не поддерживаются.',
        'Файловая система': 'Выберите существующий локальный диск ext4 через меню 6 «Дополнительные режимы». Мастер ничего не форматирует и не удаляет.',
        'Безопасный путь': 'Через меню 6 выберите абсолютный путь только с латинскими буквами, цифрами и знаками _ . / -, например /var/tmp/geacx1-work.',
        'Свободное место': 'Освободите минимум 80 ГиБ или через меню 6 выберите другую существующую папку на ext4. Мастер не удаляет файлы и не форматирует диск.',
    }
    return actions.get(check.get('name'), 'Исправьте указанное условие и повторите пункт 3 «Проверить компьютер и комплект».')


def print_checks(work, min_free_bytes=80 * 1024**3):
    checks = core.host_checks(work, min_free_bytes=min_free_bytes)
    for c in checks:
        print(f'  {paint("✓", "32") if c["ok"] else paint("✗", "31")} {c["name"]}: {c["detail"]}')
        if not c['ok']:
            print('      → ' + host_check_action(c))
    return all(c['ok'] for c in checks)


def require_runtime_tools():
    missing = core.check_runtime()
    if missing:
        raise core.RecoveryError('Не хватает системных команд: ' + ', '.join(missing) +
                                 '. Повторите установку зависимостей NVIDIA.')


def write_host_report(vendor, work, report):
    checks = core.host_checks(work)
    bundle = core.inspect_bundle(vendor)
    ok = all(item['ok'] for item in checks)
    lines = [
        'GEACX1 Recovery — отчёт проверки компьютера и комплекта',
        'Дата: ' + datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'Рабочая папка: ' + str(work),
        '',
        'Проверки компьютера:',
    ]
    for item in checks:
        lines.append(f"  {'OK' if item['ok'] else 'ОШИБКА'} — {item['name']}: {item['detail']}")
        if not item['ok']:
            lines.append('    Действие: ' + host_check_action(item))
    lines.extend(['', 'Комплект производителя:'])
    for name, value in sorted(bundle.items()):
        if isinstance(value, (str, int, float, bool)) or value is None:
            lines.append(f'  {name}: {value}')
    lines.extend([
        '',
        'Основа системы: NVIDIA Sample Root Filesystem R39.2.0 + BSP/ядро/DTB производителя.',
        'Заводской rootfs v1.01 не включён и не сравнивался; это не подтверждение заводской идентичности.',
        'Итог: ' + ('проверки компьютера пройдены' if ok else 'есть ошибки, подготовку начинать нельзя'),
        'Следующее действие: ' + (
            'в главном меню выберите «Восстановить с нуля: загрузчик + система».'
            if ok else 'выполните подсказки «Действие» под ошибками, затем повторите пункт 3.'
        ),
    ])
    report = Path(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return ok


def select_mode():
    choice = choose('Что восстановить?', [('1', MODES['emmc'][0]+' — рекомендуется'),
                                           ('2', MODES['qspi'][0]), ('3', MODES['nvme'][0]), ('0','Назад')])
    mode = {'1':'emmc','2':'qspi','3':'nvme'}.get(choice)
    if mode == 'nvme' and not confirm_nvme_requirements():
        return None
    return mode


def confirm_nvme_requirements():
    print('\nNVMe: рецепт производителя есть, но на этой плате здесь физически не проверен.\n'
          'Нужен установленный в GEACX1 SSD nvme0n1 не меньше 64 GiB (68,7 GB); рекомендуется 128 GB.\n'
          'Будут записаны QSPI + SSD. Встроенная eMMC не восстанавливается.\n'
          'Загрузка с SSD зависит от порядка загрузки UEFI и может потребовать отдельной настройки.')
    return ask('Введите NVME64, если SSD установлен и его можно стереть') == 'NVME64'


def show_recovery():
    screen('Переведите GEACX1 в Force Recovery', 'Установленная система для этого не нужна')
    print('  1. Подключите питание GEACX1 и кабель данных Micro-USB к ПК.\n'
          '  2. На GEACX1 зажмите кнопку RECV / Recovery.\n'
          '  3. Удерживая RECV, нажмите RST / Reset на 1–3 секунды.\n'
          '  4. Отпустите RST, затем RECV.\n'
          '  5. Оставьте подключённым только один Jetson.\n\n'
          'Чёрный экран в Recovery — нормально. HDMI здесь не нужен.\n'
          'После каждой попытки прошивки входите в Recovery повторно.')
    while True:
        devs = core.recovery_devices()
        print('\nОбнаружено NVIDIA USB-устройств:', len(devs))
        for d in devs:
            port = Path(d.get('sysfs', 'неизвестно')).name
            product = d.get('product_id', 'неизвестно')
            serial = d.get('serial') or 'нет серийного номера в Recovery'
            print(f'  NVIDIA, USB-порт {port}, код устройства {product}, {serial}')
        if len(devs) == 1 and devs[0].get('product_id') in core.AGX_ORIN_RECOVERY_PIDS:
            print(paint('USB Recovery AGX Orin найден. SKU будет проверен по EEPROM перед записью.', '32'))
            print(usb_support.describe_connection(devs[0]['sysfs']))
            return devs[0]
        print('Нужно ровно одно AGX Orin в Recovery (0955:7223 или 0955:7023). SKU 3701/0004 проверит EEPROM.')
        if ask('Enter — проверить снова; 0 — назад') == '0':
            return False


def usb_identity(device):
    """Bind approval to this USB enumeration, even when APX has no serial."""
    path = Path(device['sysfs'])
    try:
        bus = (path / 'busnum').read_text().strip()
        number = (path / 'devnum').read_text().strip()
    except OSError as exc:
        raise core.RecoveryError('Нельзя зафиксировать USB-устройство; повторите Recovery.') from exc
    if not bus.isdigit() or not number.isdigit():
        raise core.RecoveryError('Некорректный адрес USB-устройства.')
    return str(path), bus, number, device.get('serial', '')


def prepare_wizard(vendor, runner, pause_after=True, simple=False):
    require_root()
    screen('Подготовка новой системы', 'Шаг 1 / 4 · Ubuntu 24.04 amd64 → JetPack 7.2')
    print('Сборка выполняется в отдельной НОВОЙ папке на ext4.\nИсходные файлы производителя сохраняются. Требуется минимум 80 ГиБ свободно.')
    suggested = '/var/tmp/geacx1-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if simple:
        work = Path(suggested)
        print('Рабочая папка выбрана автоматически:', work)
    else:
        work = Path(ask('Рабочая папка', suggested)).expanduser().absolute()
    if not print_checks(work):
        suffix = ' Для выбора другого диска используйте «Дополнительные режимы».' if simple else ''
        raise core.RecoveryError('Проверки компьютера не пройдены. Исправьте пункты с крестиком.' + suffix)
    core.inspect_bundle(vendor)
    screen('Официальная файловая система NVIDIA', 'Шаг 2 / 4 · R39.2.0 / aarch64')
    bundled = BASE/'downloads'/core.ROOTFS_NAME
    print('Нужен Sample Root Filesystem R39.2.0, а не ISO, SDK Manager или JetPack 6.')
    if simple:
        rootfs = bundled
        print('Использую rootfs из комплекта:', rootfs)
    else:
        rootfs = Path(ask('Путь к архиву rootfs (либо download)', str(bundled))).expanduser().absolute()
    if rootfs.name == 'download' or not rootfs.is_file():
        if not simple and ask('Архив отсутствует. Скачать около 2 ГБ с NVIDIA? да/нет', 'да') != 'да':
            return None
        print('Rootfs отсутствует в комплекте. Скачиваю официальный архив NVIDIA…')
        rootfs = core.download_rootfs(bundled, lambda m: print(m, flush=True))
    run_with_status('Проверяю контрольные суммы комплекта. Большой архив читается целиком…',
                    lambda: core.verify_manifest(BASE))
    run_with_status('Проверяю архив NVIDIA и архитектуру ARM64. Это может занять несколько минут…',
                    lambda: core.validate_rootfs(rootfs))
    screen('Зависимости компьютера', 'Шаг 3 / 4 · Проверка и установка')
    print('Будет запущен штатный l4t_flash_prerequisites.sh NVIDIA:\nAPT установит инструменты сборки, qemu и NFS. Это не записывает устройство.')
    if ask('Установить/проверить зависимости? да/нет', 'да') != 'да':
        raise core.RecoveryError('Подготовка отменена до установки зависимостей.')
    core.install_host_dependencies(vendor, runner.run)
    screen('Сборка системы с драйверами GEACX1', 'Шаг 4 / 4 · Не закрывайте терминал')
    print('Журнал:',runner.log)
    print('Проценты не угадываются: ниже показаны текущий этап и время команды.')
    l4t = core.prepare(vendor, rootfs, work, runner.run, lambda m: print('\n'+paint(str(m),'1;36'),flush=True))
    LAST_WORK.write_text(json.dumps({'l4t':str(l4t)}, ensure_ascii=False)+'\n')
    print(paint('\nПодготовка завершена. Устройство пока не прошивалось.', '32'))
    print('Рабочая папка:',l4t)
    if pause_after:
        pause()
    return l4t


def flash_wizard(l4t, runner, fixed_mode=None, reuse_images=False):
    require_root()
    if l4t is None:
        saved = LAST_WORK
        default = ''
        if saved.is_file():
            try:
                default = json.loads(saved.read_text()).get('l4t','')
            except (ValueError,OSError):
                pass
        value = ask('Путь к ПОДГОТОВЛЕННОЙ Linux_for_Tegra',default)
        if not value:
            return False
        l4t = Path(value).expanduser().absolute()
    minimum = 80 * 1024**3
    if reuse_images:
        if fixed_mode not in ('emmc', 'nvme'):
            raise core.RecoveryError('Для готового образа выберите eMMC или NVMe.')
        metadata = run_with_status('Проверяю SHA-256 готовых образов…',
                                   lambda: reusable.validate(l4t, fixed_mode))
        minimum = reusable.required_free_bytes(l4t, fixed_mode)
        print('Готовый образ проверен. Сохранён:', metadata['created_at'])
        print('Будет записано прежнее содержимое образа. Для включения новых изменений rootfs нужна обычная сборка.')
    if not print_checks(l4t, min_free_bytes=minimum):
        raise core.RecoveryError('Проверки хоста/рабочей папки не пройдены.')
    require_runtime_tools()
    core.validate_prepared(l4t)
    if fixed_mode is not None and fixed_mode not in MODES:
        raise ValueError('Неизвестный режим прошивки.')
    mode = fixed_mode or select_mode()
    if mode is None:
        return False
    if fixed_mode == 'nvme' and not confirm_nvme_requirements():
        print('Запись NVMe отменена. Устройство не прошивалось.')
        return False
    if mode == 'nvme':
        core.require_initrd_network()
    selected = show_recovery()
    if not selected:
        return False
    identity = usb_identity(selected)
    screen('Последняя проверка перед записью', 'Автоматического старта и подтверждения по Enter нет')
    command = reusable.build_command(mode) if reuse_images else core.build_command(l4t, mode)
    command[1:1] = ['--usb-instance', Path(selected['sysfs']).name]
    print('Режим:',MODES[mode][0])
    print('USB:', Path(selected['sysfs']).name, 'bus/device:', identity[1], identity[2])
    print('Каталог:',l4t)
    print('Команда:',shlex.join(command))
    print('Журнал:',runner.log,'\n')
    if not ask_erase(mode):
        print('Запись отменена. Устройство не прошивалось.')
        pause()
        return False
    # Recheck after confirmation; never continue with a changed USB topology.
    devs = core.recovery_devices()
    if len(devs) != 1 or devs[0].get('product_id') not in core.AGX_ORIN_RECOVERY_PIDS:
        raise core.RecoveryError('USB состав изменился. Вернитесь в Recovery и повторите выбор.')
    if usb_identity(devs[0]) != identity:
        raise core.RecoveryError('Подтверждённое USB-устройство отключалось или было заменено. Повторите подтверждение.')
    core.validate_prepared(l4t)
    if reuse_images:
        run_with_status('Повторно проверяю образ перед записью…', lambda: reusable.validate(l4t, mode))
    else:
        reusable.invalidate(l4t)
    runner.destructive = True
    try:
        with core.preserve_prepared_initrd(l4t), flash_host_state(runner):
            usb_preflight(identity, runner)
            reusable.isolate_board_spec(l4t)
            runner.run(command, l4t)
    finally:
        runner.destructive = False
    if mode in ('emmc', 'nvme'):
        try:
            if reuse_images:
                run_with_status('Проверяю сохранность готового образа после записи…',
                                lambda: reusable.refresh_after_repeat(l4t, mode))
            else:
                run_with_status('Сохраняю контрольные суммы для повторной прошивки…',
                                lambda: reusable.record_success(l4t, mode))
            runner.note('READY_IMAGE: ' + mode + ' ' + str(l4t))
        except (core.RecoveryError, OSError, ValueError) as exc:
            runner.note('READY_IMAGE_UNAVAILABLE: ' + str(exc))
            print('NVIDIA завершила запись, но готовый образ для повтора не сохранён:', exc)
    screen('Утилита прошивки завершилась без ошибки')
    print(paint('Следующий шаг — проверить загрузку самого GEACX1.', '32'))
    if mode == 'qspi':
        print('Обновлена только QSPI. Для чистой системы выполните полное восстановление eMMC.')
    else:
        print('Дождитесь перезагрузки, подключите HDMI/клавиатуру.\nПройдите первоначальную настройку Ubuntu: язык, имя пользователя и пароль.\nНа GEACX1 запустите: bash POSTCHECK.sh\nВычислительные библиотеки JetPack устанавливаются отдельно по инструкции.')
    print('\nЖурнал:',runner.log)
    pause()
    return True


def select_guided_target():
    while True:
        choice = choose('Куда установить чистую систему?', [
            ('1', 'Встроенная eMMC — восстановить загрузчик + систему (рекомендуется)'),
            ('2', 'SSD NVMe — восстановить загрузчик + систему на установленном SSD'),
            ('3', 'Оба накопителя — сначала eMMC, затем отдельным проходом NVMe'),
            ('4', 'Не знаю, какой накопитель установлен'),
            ('0', 'Назад'),
        ])
        if choice in ('1', '2', '3', '0'):
            return {'1':'emmc', '2':'nvme', '3':'both'}.get(choice)
        screen('Как выбрать накопитель')
        print('eMMC — встроенное хранилище модуля GEACX1, оно есть всегда. Объём 32 ГБ в названии платы означает оперативную память, а не eMMC.\n'
              'NVMe — отдельный SSD, установленный в разъём платы. Проверяйте его физически при выключенном питании.\n'
              'Диски компьютера ничего не говорят о наличии SSD внутри GEACX1.\n\n'
              'Если нужен полный возврат платы к чистой системе и вы не уверены — выбирайте eMMC.')
        pause()


def guided_recovery(vendor, runner):
    target = select_guided_target()
    if target is None:
        return False
    screen('Восстановление с нуля', 'Система на самой плате может быть полностью неработоспособна')
    print('Мастер создаст новую систему JetPack 7.2 на этом компьютере.\n'
          'Старая Ubuntu, SSH, пользователи и настройки GEACX1 не используются.\n'
          'EEPROM, CPLD и eFuse автоматически не изменяются; модель проверяется перед записью.')
    l4t = prepare_wizard(vendor, runner, pause_after=False, simple=True)
    if l4t is None:
        return False
    next_step = choose('Система подготовлена на компьютере', [
        ('1', 'Перейти к подключению платы и записи'),
        ('0', 'Вернуться в меню — запись не начнётся'),
    ])
    if next_step != '1':
        print('Подготовленная система сохранена. GEACX1 не прошивался.')
        return False
    return flash_prepared_target(l4t, runner, target)


def flash_prepared_target(l4t, runner, target):
    first_mode = 'emmc' if target == 'both' else target
    if not flash_wizard(l4t, runner, fixed_mode=first_mode):
        return False
    if target != 'both':
        return True
    screen('eMMC восстановлена', 'NVMe — отдельная операция и отдельное подтверждение')
    print('Для NVMe плата должна снова войти в Force Recovery.\n'
          'NVMe-рецепт повторно записывает QSPI — это штатная часть процедуры производителя.\n'
          'Порядок загрузки UEFI с SSD автоматически не гарантируется.')
    if choose('Продолжить вторым проходом?', [
        ('1', 'Повторить Recovery и отдельно записать NVMe'),
        ('0', 'Завершить — NVMe не изменять'),
    ]) != '1':
        return True
    return flash_wizard(l4t, runner, fixed_mode='nvme')


def saved_work_path():
    if not LAST_WORK.is_file():
        raise core.RecoveryError('Нет сохранённой подготовленной системы. Сначала выберите пункт 1 и выполните подготовку с нуля.')
    try:
        value = json.loads(LAST_WORK.read_text(encoding='utf-8')).get('l4t', '')
    except (OSError, ValueError) as exc:
        raise core.RecoveryError('Файл последней рабочей папки повреждён. Выполните новую подготовку через пункт 1.') from exc
    if not value:
        raise core.RecoveryError('В сохранении нет пути к Linux_for_Tegra. Выполните новую подготовку через пункт 1.')
    return Path(value).expanduser().absolute()


def continue_prepared(runner):
    l4t = saved_work_path()
    core.validate_prepared(l4t)
    screen('Подготовленная система проверена')
    print('Папка:', l4t)
    print('Этот пункт повторно использует только сборку на компьютере. Старая система GEACX1 не используется.')
    target = select_guided_target()
    if target is None:
        return False
    if target == 'both':
        print('\nМаршрут «оба накопителя» начинается с eMMC. После её успеха мастер отдельно предложит NVMe.\n'
              'Если eMMC уже восстановлена и нужен только второй проход, выберите SSD NVMe.')
    if choose('Продолжить?', [('1', 'Перейти к подключению платы и записи'), ('0', 'Назад')]) != '1':
        return False
    return flash_prepared_target(l4t, runner, target)


def repeat_ready_images(runner):
    l4t = saved_work_path()
    screen('Повторить запись готового образа')
    print('Используется образ последней успешной операции, зарегистрированный этой версией мастера.\n'
          'Ядро и Ubuntu заново не собираются. NVIDIA подготовит загрузчики и проверит модель модуля.\n'
          'Старые образы без записи проверки и образы другого накопителя не принимаются.\n'
          'Если готового образа ещё нет, выполните обычную запись через пункт 2 один раз.')
    mode = choose('Какой готовый образ записать?', [('1', 'eMMC'), ('2', 'NVMe'), ('0', 'Назад')])
    if mode == '0':
        return False
    return flash_wizard(l4t, runner, fixed_mode={'1': 'emmc', '2': 'nvme'}[mode], reuse_images=True)


def cleanup_generated_images(runner):
    require_root()
    l4t = saved_work_path()
    protected = set()
    # Keep NVMe's raw input even if its checksum has failed: cleanup must not
    # silently make an existing repeat package unusable.
    receipt = l4t / reusable.RECEIPT
    if receipt.exists() or receipt.is_symlink():
        try:
            data = json.loads(receipt.read_text())
            if not isinstance(data, dict) or data.get('mode') != 'emmc':
                protected = reusable.protected_cleanup_paths('nvme')
        except (OSError, ValueError):
            protected = reusable.protected_cleanup_paths('nvme')
    plan = generated_cleanup.plan_cleanup(l4t, protected_paths=protected)
    screen('Очистка результатов сборки на компьютере')
    print('Рабочая сборка:', l4t)
    for item in plan['entries']:
        print(f"  {item['path']}: занято {item['allocated_bytes'] / 1024**3:.2f} ГиБ "
              f"(видимый размер {item['size'] / 1024**3:.2f} ГиБ)")
    if protected:
        print('system.img.raw сохранён: он нужен для повторной записи NVMe либо состояние образа не подтверждено.')
    if not plan['entries']:
        print('В этой сборке нет ненужных промежуточных образов для удаления.')
        pause()
        return False
    print(f"Можно освободить до {plan['allocated_bytes'] / 1024**3:.2f} ГиБ. Исходники, система, журналы и system.img сохраняются.")
    if ask('Для удаления перечисленных файлов введите CLEAN GENERATED') != 'CLEAN GENERATED':
        print('Очистка отменена.')
        return False
    result = generated_cleanup.execute_cleanup(l4t, plan)
    runner.note('GENERATED_CLEANUP: ' + json.dumps(result, ensure_ascii=False))
    print('Перечисленные промежуточные файлы удалены.')
    pause()
    return True


def _validate_backup_prepared(l4t):
    l4t = Path(l4t).expanduser().absolute()
    if not print_checks(l4t):
        raise core.RecoveryError('Компьютер или рабочая папка не прошли проверку.')
    core.validate_prepared(l4t)
    backup.validate_tools(l4t)
    require_runtime_tools()
    return l4t


def backup_prepared_path(runner):
    if LAST_WORK.is_file():
        try:
            l4t = _validate_backup_prepared(saved_work_path())
            print('Использую уже проверенную среду NVIDIA:', l4t)
            return l4t
        except (core.RecoveryError, OSError, ValueError) as exc:
            print('Сохранённая среда не подошла:', exc)
    choice = choose('Для копии нужна среда NVIDIA на компьютере', [
        ('1', 'Подготовить среду сейчас — GEACX1 ещё не прошивается'),
        ('2', 'Указать готовую папку Linux_for_Tegra вручную'),
        ('0', 'Назад'),
    ])
    if choice == '0':
        raise core.RecoveryError('Операция отменена до подключения платы.')
    if choice == '1':
        l4t = prepare_wizard(vendor_path(), runner, pause_after=False, simple=True)
        if l4t is None:
            raise core.RecoveryError('Подготовка среды отменена.')
        return _validate_backup_prepared(l4t)
    value = ask('Путь к подготовленной Linux_for_Tegra')
    if not value:
        raise core.RecoveryError('Путь не указан.')
    return _validate_backup_prepared(value)


def select_backup_scope():
    choice = choose('Что сохранить или восстановить?', [
        ('1', 'QSPI + GPT и все разделы встроенной eMMC (рекомендуется)'),
        ('2', 'QSPI + GPT и все разделы eMMC и SSD NVMe nvme0n1'),
        ('0', 'Назад'),
    ])
    return {'1': 'emmc', '2': 'emmc_nvme'}.get(choice)


def recheck_recovery_identity(identity):
    devs = core.recovery_devices()
    if len(devs) != 1 or devs[0].get('product_id') not in core.AGX_ORIN_RECOVERY_PIDS:
        raise core.RecoveryError('USB состав изменился. Вернитесь в Recovery и повторите выбор.')
    if usb_identity(devs[0]) != identity:
        raise core.RecoveryError('Подтверждённое USB-устройство отключалось или было заменено.')


def usb_preflight(identity, runner, samples=7, interval=0.5):
    runner.note(usb_support.describe_connection(identity[0]))
    runner.note('USB: проверяю стабильность подключения перед запуском. На плату ещё ничего не записывается.')
    for index in range(samples):
        if index:
            time.sleep(interval)
        recheck_recovery_identity(identity)
    runner.note('USB: за время предварительной проверки переподключений не обнаружено. '
                'Не закрывайте крышку ноутбука и не отключайте питание/кабель. '
                'Лимит скачивания 2 часа не ограничивает время прошивки.')


def create_full_backup(runner):
    require_root()
    l4t = backup_prepared_path(runner)
    scope = select_backup_scope()
    if scope is None:
        return False
    screen('Полная резервная копия', 'Штатный NVIDIA Backup Workflow 1')
    print('Копия включит QSPI с загрузчиком, GPT и все разделы выбранных накопителей.\n'
          'EEPROM, eFuse, CPLD и аппаратные области eMMC boot0/boot1/RPMB в эту копию не входят.\n'
          'Плата будет временно загружена в initrd через USB. NVIDIA монтирует ext4 для чтения файлов,\n'
          'поэтому журнал ext4 и служебные метаданные могут измениться; старые разделы не стираются.')
    suggested = '/var/tmp/GEACX1-full-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    destination = Path(ask('НОВАЯ папка для копии', suggested)).expanduser().absolute()
    if destination.exists():
        raise core.RecoveryError('Папка копии уже существует. Выберите новое имя.')
    if not destination.parent.is_dir():
        raise core.RecoveryError('Родительская папка для копии не существует.')
    required_gib = 160 if scope == 'emmc_nvme' else 80
    work_free_gib = shutil.disk_usage(l4t).free / 1024**3
    print(f'Свободно для временных образов NVIDIA: {work_free_gib:.1f} ГиБ.')
    if work_free_gib < required_gib:
        raise core.RecoveryError(f'В рабочей папке нужно не меньше {required_gib} ГиБ свободного места.')
    free_gib = shutil.disk_usage(destination.parent).free / 1024**3
    print(f'Свободно в месте копии: {free_gib:.1f} ГиБ; нижний порог мастера: {required_gib} ГиБ.')
    if scope == 'emmc_nvme':
        print('Реально нужное место зависит от ёмкости и заполнения NVMe; для большого SSD 160 ГиБ может не хватить.')
    if free_gib < required_gib:
        raise core.RecoveryError(f'Для полной копии нужно не меньше {required_gib} ГиБ свободного места.')
    core.require_initrd_network()
    selected = show_recovery()
    if not selected:
        return False
    identity = usb_identity(selected)
    command = backup.build_command(l4t, 'backup', scope)
    screen('Последняя проверка перед чтением')
    print('Источник:', 'QSPI + eMMC + NVMe' if scope == 'emmc_nvme' else 'QSPI + eMMC')
    print('Куда:', destination)
    print('Команда NVIDIA:', shlex.join(command))
    print('Команда запуска и весь вывод NVIDIA/удалённых скриптов будут сохранены в журнале:', runner.log)
    if ask('Введите BACKUP GEACX1 для начала') != 'BACKUP GEACX1':
        print('Создание копии отменено.')
        return False
    core.validate_prepared(l4t)
    with backup.isolated_images(l4t) as images:
        with core.preserve_prepared_initrd(l4t), backup_host_state(runner):
            usb_preflight(identity, runner)
            runner.run(command, l4t)
        run_with_status('Проверяю каждый раздел и создаю криптографический манифест…',
                        lambda: backup.export_snapshot(images, destination, scope))
    print(paint('\nПолная копия проверена и сохранена: ' + str(destination), '32'))
    pause()
    return True


def verify_full_backup():
    snapshot = Path(ask('Папка резервной копии')).expanduser().absolute()
    metadata = run_with_status('Проверяю все файлы и SHA-256…', lambda: backup.validate_snapshot(snapshot))
    print(paint('Копия целая и совместима с GEACX1 P3701-0004 / R39.2.0.', '32'))
    print('Состав:', 'QSPI + eMMC + NVMe' if metadata['scope'] == 'emmc_nvme' else 'QSPI + eMMC')
    print('board_spec:', metadata['board_spec'])
    pause()
    return metadata


def restore_full_backup(runner):
    require_root()
    snapshot = Path(ask('Папка резервной копии')).expanduser().absolute()
    metadata = run_with_status('До подключения платы проверяю все SHA-256…', lambda: backup.validate_snapshot(snapshot))
    l4t = backup_prepared_path(runner)
    staging_bytes = sum(record['size'] for record in metadata['files'].values())
    staging_free = shutil.disk_usage(l4t).free
    if staging_free < staging_bytes + 5 * 1024**3:
        need_gib = (staging_bytes + 5 * 1024**3) / 1024**3
        raise core.RecoveryError(f'Для безопасной временной копии перед восстановлением нужно {need_gib:.1f} ГиБ свободно.')
    scope = metadata['scope']
    core.require_initrd_network()
    selected = show_recovery()
    if not selected:
        return False
    identity = usb_identity(selected)
    command = backup.build_command(l4t, 'restore', scope)
    screen('ВОССТАНОВЛЕНИЕ ИЗ КОПИИ', 'После этого отменить запись безопасно нельзя')
    targets = 'QSPI, eMMC и NVMe nvme0n1' if scope == 'emmc_nvme' else 'QSPI и eMMC'
    print('Будут полностью перезаписаны:', targets)
    print('Копия:', snapshot)
    print('board_spec копии:', metadata['board_spec'])
    print('Команда NVIDIA:', shlex.join(command))
    print('Команда запуска и весь вывод NVIDIA/удалённых скриптов будут сохранены в:', runner.log)
    if ask('Введите точно RESTORE GEACX1 BACKUP') != 'RESTORE GEACX1 BACKUP':
        print('Восстановление отменено. Плата не изменена.')
        return False
    run_with_status('Повторно проверяю копию после подтверждения…', lambda: backup.validate_snapshot(snapshot))
    core.validate_prepared(l4t)
    with backup.isolated_images(l4t, snapshot):
        with core.preserve_prepared_initrd(l4t), backup_host_state(runner):
            usb_preflight(identity, runner)
            runner.destructive = True
            try:
                runner.run(command, l4t)
            finally:
                runner.destructive = False
    print(paint('\nNVIDIA завершила восстановление без ошибки.', '32'))
    print('Исходная папка копии не изменена. Перезагрузите GEACX1 и проверьте загрузку.')
    print('Если после переноса на другой диск загрузка останавливается на ожидании накопителя,\n'
          'сверьте UUID из /etc/fstab с lsblk -f на GEACX1. Не удаляйте fstab целиком.\n'
          'Восстановление возвращает также прежние настройки и ошибки системы из копии.')
    pause()
    return True


def backup_menu(runner):
    while True:
        choice = choose('Полная копия и восстановление', [
            ('1', 'Создать полную копию QSPI + eMMC / NVMe'),
            ('2', 'Проверить копию и SHA-256, не подключая плату'),
            ('3', 'Восстановить все из копии (сотрёт выбранные накопители)'),
            ('0', 'Назад'),
        ])
        if choice == '0':
            return
        if choice == '1':
            create_full_backup(runner)
        elif choice == '2':
            verify_full_backup()
        elif choice == '3':
            restore_full_backup(runner)


def advanced_menu(vendor, runner):
    while True:
        choice = choose('Дополнительные режимы', [
            ('1', 'Подготовить новую систему с ручным выбором папок'),
            ('2', 'Выбрать подготовленную папку и режим eMMC / QSPI / NVMe'),
            ('0', 'Назад'),
        ])
        if choice == '0':
            return
        if choice == '1':
            prepare_wizard(vendor, runner)
        elif choice == '2':
            flash_wizard(None, runner)


def check_computer_and_bundle(vendor, work):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    report = BASE / 'logs' / f'check-{stamp}.txt'
    ok = write_host_report(vendor, work, report)
    print(report.read_text(encoding='utf-8'))
    print('Текущий отчёт сохранён:', report)
    try:
        verified = run_with_status('Проверяю SHA256 всех файлов комплекта. Большой rootfs читается целиком…',
                                   lambda: core.verify_manifest(BASE))
    except (KeyboardInterrupt, EOFError):
        with report.open('a', encoding='utf-8') as stream:
            stream.write('\nКонтрольные суммы: проверка отменена; целостность не подтверждена.\n')
            stream.write('Общий итог: до подготовки или прошивки нужно завершить проверку комплекта.\n')
            stream.write('Следующее действие: повторите пункт 3 «Проверить компьютер и комплект».\n')
        print('Отмена проверки записана в отчёт:', report)
        raise
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        with report.open('a', encoding='utf-8') as stream:
            stream.write(f'\nКонтрольные суммы: ОШИБКА — {reason}\n')
            stream.write('Общий итог: комплект нельзя использовать для подготовки или прошивки.\n')
            stream.write('Следующее действие: повторно скачайте или распакуйте полный комплект, затем повторите проверку.\n')
        print('Ошибка контрольных сумм записана в отчёт:', report)
        raise
    with report.open('a', encoding='utf-8') as stream:
        stream.write(f'\nКонтрольные суммы: OK, проверено файлов: {len(verified)}\n')
        stream.write('Общий итог: компьютер и комплект проверены.\n' if ok else
                     'Общий итог: контрольные суммы верны, но проверки компьютера не пройдены.\n')
    print(f'Контрольные суммы: OK, проверено файлов: {len(verified)}')
    print('Отчёт сохранён:', report)
    return ok, report


def help_menu():
    choice = choose('Инструкция и помощь', [
        ('1', 'Где открыть пошаговую инструкцию'),
        ('2', 'Что приложить при обращении за помощью'),
        ('3', 'Что мастер намеренно не изменяет'),
        ('4', 'Пакеты производителя, заводской rootfs и CPLD'),
        ('0', 'Назад'),
    ])
    screen('Инструкция и помощь')
    if choice == '1':
        print('Откройте в браузере:', BASE/'docs/GEACX1-GUIDE-RU.html')
        print('Версия для печати:', BASE/'docs/GEACX1-GUIDE-RU.pdf')
    elif choice == '2':
        print('Приложите последний файл из папки:', BASE/'logs')
        print('Также укажите, на каком экране остановился мастер и горит ли питание платы.')
    elif choice == '3':
        print('Мастер не сбрасывает EEPROM, CPLD и eFuse и не прожигает ключи Secure Boot.\n'
              'Он не отключает firewall целиком и не повторяет неудачную прошивку автоматически.')
    elif choice == '4':
        print('Комплект v1.3: NVIDIA Sample Root Filesystem R39.2.0 + BSP, ядро и DTB производителя.\n'
              'Заводской rootfs v1.01 (16,67 ГБ) не скачан и не сравнивался с этой системой.\n'
              'Для обычного восстановления через этот мастер скачивать его не требуется.\n\n'
              'Сетевой установщик производителя: GEACX1-JP7.2.json возвращал HTTP 404 на 02.10.2026.\n'
              'Не заменяйте его конфигурацией JP6.2. Мастер использует локальные пакеты JP7.2.\n\n'
              'CPLD не входит в автоматическое восстановление. Инструкция GEACX1 указывает\n'
              '/dev/ttyTHS1 для диагностики версии, состояния и синхронизации.\n'
              'Перед использованием утилиты сверьте её версию и ревизию платы с инструкцией.\n'
              'CPLD GEACX1SC не подходит автоматически для GEACX1; MCU-архив JP5.1.1 содержит тест GPIO/SPI.\n'
              'Команды изменения режима, сброса и прошивки отдельно от чтения состояния.')
        print('\nПроверенные ссылки и ограничения:', BASE/'docs/SITE_REVIEW_RU.txt')
    else:
        return
    pause()


def demo():
    screen('Демонстрация интерфейса', 'Без sudo, загрузок, установки пакетов и обращения к USB')
    print('① Выбор eMMC/SSD → ② Проверки ПК → ③ Чистая система → ④ Recovery → ⑤ Подтверждение → ⑥ Запись')
    print('\nПример этапов подготовки:')
    for line in ('Проверка версии: JetPack 7.2 / R39.2.0', 'Проверка модели: P3701-0004',
                 'Драйверы: 510JX0 r3.0, kernel 6.8.12', 'Полная запись: QSPI + eMMC'):
        print('  '+paint('✓','32')+' '+line)
    print('\nЭто пример экранов, не результат проверки оборудования.')
    print('Основной пункт меню всегда создаёт новую систему и не зависит от состояния Ubuntu на плате.')
    print('Для записи потребуется фраза: ERASE GEACX1 32GB')


def main():
    parser = argparse.ArgumentParser(description='GEACX1 32GB / JP7.2: русский мастер восстановления')
    parser.add_argument('--demo',action='store_true',help='Показать интерфейс без действий')
    parser.add_argument('--check',action='store_true',help='Только проверить хост/комплект; без sudo')
    parser.add_argument('--plan',action='store_true',help='Только показать команды')
    parser.add_argument('--mode',choices=MODES,default='emmc')
    parser.add_argument('--work',type=Path,default=Path('/var/tmp/geacx1-work'))
    args = parser.parse_args()
    if args.demo:
        demo(); return 0
    if args.plan:
        print('ПЛАН, БЕЗ ВЫПОЛНЕНИЯ. Сначала нужна подготовка vendor BSP и rootfs.')
        print(shlex.join(core.build_command(args.work/'Linux_for_Tegra',args.mode)))
        return 0
    vendor = vendor_path()
    if args.check:
        screen('Диагностика без изменений')
        ok, report = check_computer_and_bundle(vendor, args.work)
        print('Runtime tools:',core.check_runtime())
        print('Отчёт:', report)
        return 0 if ok else 1
    if not sys.stdin.isatty():
        parser.error('Меню требует терминал. Для проверки используйте --check или --plan.')
    log = BASE/'logs'/('recovery-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.log')
    runner = Runner(log)
    while True:
        option = choose('Восстановление после повреждения прошивки', [
            ('1','Восстановить с нуля: загрузчик + система (рекомендуется)'),
            ('2','Продолжить с подготовленной системой на компьютере'),
            ('3','Проверить компьютер и комплект'),
            ('4','Подключение платы / Force Recovery'),
            ('5','Инструкция и помощь'),
            ('6','Дополнительные режимы'),
            ('7','Полная резервная копия / восстановление'),
            ('8','Повторить прошивку готовым проверенным образом'),
            ('9','Освободить место: ненужные результаты сборки'),
            ('0','Выход')])
        try:
            if option == '0':
                return 0
            if option == '1':
                guided_recovery(vendor, runner)
            elif option == '2':
                continue_prepared(runner)
            elif option == '3':
                screen('Проверка компьютера и комплекта', 'Ничего не устанавливается и GEACX1 не изменяется')
                check_computer_and_bundle(vendor, Path('/var/tmp/geacx1-work'))
                pause()
            elif option == '4':
                show_recovery(); pause()
            elif option == '5':
                help_menu()
            elif option == '6':
                advanced_menu(vendor, runner)
            elif option == '7':
                backup_menu(runner)
            elif option == '8':
                repeat_ready_images(runner)
            elif option == '9':
                cleanup_generated_images(runner)
        except (core.RecoveryError,OSError,ValueError) as exc:
            runner.note('Операция остановлена: ' + str(exc))
            print(paint('\nОперация остановлена: '+str(exc),'1;31'))
            tail = ''
            if runner.log.exists():
                with runner.log.open('rb') as f:
                    f.seek(max(0,runner.log.stat().st_size-25000))
                    tail = f.read().decode('utf-8',errors='replace')
            for tip in diagnose(str(exc)+'\n'+tail):
                print('\n• '+tip)
            print('\nЖурнал:',runner.log)
            pause()
        except (KeyboardInterrupt, EOFError):
            print('\nОперация прервана. Возвращаюсь в главное меню.\n'
                  'Если прервали подготовку — создайте новую сборку через пункт 1.\n'
                  'Если прервали запись — повторите Recovery; пункт 2 заново проверит готовую сборку перед записью.')
            pause()

if __name__ == '__main__':
    try:
        read_only = any(arg in sys.argv for arg in ('--demo', '--check', '--plan', '--help', '-h'))
        if sys.platform.startswith('linux') and os.geteuid() == 0 and not read_only:
            with exclusive_session():
                result = main()
        else:
            result = main()
        raise SystemExit(result)
    except (core.RecoveryError,OSError,ValueError) as exc:
        print('Ошибка:',exc,file=sys.stderr)
        raise SystemExit(1)
    except (KeyboardInterrupt,EOFError):
        print('\nВыход. Если запись была прервана, повторите Force Recovery.',file=sys.stderr)
        raise SystemExit(130)
