"""Host-only USB diagnostics and reversible power policy for NVIDIA recovery."""
import contextlib
import math
from pathlib import Path

USB_AUTOSUSPEND = Path('/sys/module/usbcore/parameters/autosuspend')


def describe_connection(path):
    path = Path(path)
    try:
        speed = float((path / 'speed').read_text().strip())
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError('invalid USB link speed')
        detail = f'USB: {speed:g} Мбит/с — скорость соединения, не скорость записи.'
        if speed < 480:
            detail += ' Необычно низкая скорость: до записи проверьте кабель и другой прямой порт ПК.'
        else:
            detail += ' Это не проверка качества кабеля и не гарантия отсутствия обрывов.'
    except (OSError, UnicodeError, ValueError):
        detail = 'USB: скорость соединения не удалось прочитать; качество кабеля не определено.'
    if '.' in path.name.partition('-')[2]:
        detail += ' В цепочке есть USB-хаб (возможно, встроенный в ПК); внешние хабы лучше убрать.'
    return detail


def _identity(path):
    return tuple((path / name).read_text().strip() for name in ('idVendor', 'idProduct', 'busnum', 'devnum'))


@contextlib.contextmanager
def keep_usb_awake(devices, global_control=USB_AUTOSUSPEND, notify=print):
    """Cover current NVIDIA devices and their APX -> initrd re-enumeration.

    Existing unrelated devices are untouched. Restore a per-device value only
    for the same enumeration, never a replacement reusing its sysfs path.
    """
    controls = []
    old_global = None
    try:
        try:
            previous = global_control.read_text().strip()
            int(previous)
            global_control.write_text('-1')
            old_global = previous
            notify('USB: автосон для новых подключений временно отключён, включая переподключение платы при загрузке initrd.')
        except (OSError, UnicodeError, ValueError):
            notify('USB: не удалось отключить общий автосон; настройки новых подключений не защищены.')
        for dev in sorted(Path(devices).glob('*')):
            try:
                if (dev / 'idVendor').read_text().strip().lower() != '0955':
                    continue
            except (OSError, UnicodeError):
                # USB interface entries have no device-level idVendor file.
                continue
            try:
                identity = _identity(dev)
                control = dev / 'power/control'
                previous = control.read_text().strip()
                control.write_text('on')
                controls.append((dev, identity, control, previous))
                notify(f'USB: энергосбережение устройства {dev.name} временно отключено.')
            except (OSError, UnicodeError):
                notify(f'USB: не удалось настроить энергосбережение {dev.name}.')
        yield
    finally:
        notices = []
        for dev, identity, control, previous in controls:
            try:
                if _identity(dev) == identity and control.read_text().strip() == 'on':
                    control.write_text(previous)
            except (OSError, UnicodeError):
                notices.append(f'USB: исходная настройка {dev.name} не восстановлена — устройство отключилось или доступ изменился.')
        if old_global is not None:
            try:
                if global_control.read_text().strip() == '-1':
                    global_control.write_text(old_global)
                    notices.append('USB: общий параметр энергосбережения возвращён к исходному значению.')
                else:
                    notices.append('USB: общий автосон изменён другим процессом; новое значение оставлено без изменений.')
            except OSError:
                notices.append('USB: не удалось вернуть исходный автосон. Перезагрузка ПК вернёт системные настройки.')
        # Restore all values before logging: a full log disk must not prevent it.
        for notice in notices:
            notify(notice)
