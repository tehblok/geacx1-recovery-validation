"""Безопасная stdlib-основа мастера восстановления GEACX1 (JetPack 7.2)."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional
import urllib.error
import urllib.parse
import urllib.request
import backup_restore


VENDOR_NAME = "flashtool_jp7.2_GA_r1.0_20260722"
ROOTFS_NAME = "Tegra_Linux_Sample-Root-Filesystem_R39.2.0_aarch64.tbz2"
# SHA-256 архива, полученного с указанного HTTPS URL; это не подпись NVIDIA.
ROOTFS_SHA256 = "3e3e4c110dc911efcaee44fffe405d355341f4d5a169b2516bf4330f846d2037"
DOWNLOAD_LIMIT_SECONDS = 2 * 60 * 60
ROOTFS_URL = (
    "https://developer.nvidia.com/downloads/embedded/L4T/r39_Release_v2.0/"
    "release/Tegra_Linux_Sample-Root-Filesystem_R39.2.0_aarch64.tbz2"
)
BOARD_DIR = "rb_flash_board_geac91_510jx0_r3_0_JP7.2_ga_v1.0"
KERNEL_DIR = "rb_flash_kernel_jp7.2_ga_v1.0"
FAN_PACKAGE = "GEACX1-JP7.2/rb-jetson-service-fan-v2.1.deb"
FAN_PACKAGE_SHA256 = "fde6379e11ad0c41aa19cd1e0f0379eae20ed3269bedaadf86ef39ca98e47325"
KERNEL_PACKAGE = "GEACX1-JP7.2/rb-jetson-firmware-kernel-jp7.2_ga-v1.0.deb"
KERNEL_PACKAGE_SHA256 = "a25e1cc36c49134cf4a0df608a0c48417721e76da28da0e7d3e74865b0cde5e4"
KERNEL_VERSION = "6.8.12-1021-tegra"
BOARD_NAME = "geacx1-32gb-jp72"
QSPI_BOARD_NAME = BOARD_NAME + "-qspi"
SYS_USB_DEVICES = Path("/sys/bus/usb/devices")
NETWORK_FLASH_SHA256 = "b7f4f2c085cbd4f91f881768d205f656e7bcbeed01dd0d3a787dc880d4ac0063"
NETWORK_WAIT_SHA256 = "998c954d400e246a1d9472faaf03d562aec4348fffee3d6ef979938a9e8270b6"

REQUIRED_L4T = (
    "nv_tegra/nv_tegra_release",
    "apply_binaries.sh",
    "flash.sh",
    "tools/l4t_update_initrd.sh",
    "tools/kernel_flash/l4t_initrd_flash.sh",
    "tools/kernel_flash/flash_l4t_external.xml",
    "bootloader/generic/cfg/flash_t234_qspi.xml",
    "bootloader/generic/cfg/flash_t234_qspi_sdmmc.xml",
    "bootloader/extlinux.conf",
)
REQUIRED_BOARD = (
    "p3701.conf.common",
    "p3737-0000-p3701-0000.conf",
    "tegra234-mb1-bct-pinmux-p3701-0000-a04.dtsi",
    "tegra234-mb1-bct-gpio-p3701-0000-a04.dtsi",
    "tegra234-mb2-bct-common.dtsi",
    "nv_recovery.sh",
    "flash.sh",
    "dtb/tegra234-p3737-0000+p3701-0000-nv.dtb",
    "dtb/tegra234-p3737-0000+p3701-0004-nv.dtb",
    "dtb/tegra234-p3737-0000+p3701-0004-nv-super.dtb",
    "dtb/tegra234-p3737-0000+p3701-0005-nv.dtb",
)
REQUIRED_KERNEL = ("boot/Image", "boot/l4t_initrd.img")

_OVERRIDE_ENV = {
    "BOARDID", "BOARDREV", "BOARDSKU", "FAB", "FUSELEVEL", "CHIPREV",
    "CHIP_SKU", "RAMCODE", "SKIPUID", "DTBFILE", "CFGFILE", "EMMC_CFG",
    "NO_ROOTFS", "NO_RECOVERY_IMG", "NO_KERNEL_DTB", "SBKKEY", "PKCKEY",
    "PKCSBK", "ROOTFS_AB", "ROOTFS_ENC", "SKIP_EEPROM_CHECK",
    "SKIP_EEPROM_CHECK_BOARDID", "SKIP_EEPROM_CHECK_SKU",
    "ADDITIONAL_DTB_OVERLAY_OPT", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV",
    "ENV", "PYTHONPATH", "PYTHONHOME", "BOOTLOADER", "CMDLINE", "DEVSECTSIZE",
    "EMCFUSE_VALUE", "FLASHAPP", "INITRD", "KERNEL_IMAGE", "MTS", "MTSPREBOOT",
    "NFSARGS", "NFSROOT", "ODMDATA", "ROOTFSSIZE", "ROOTFS_DIR", "SPEFILE",
    "WB0BOOT",
}
_CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_RUNTIME_TOOLS = (
    "bash", "tar", "cpio", "gzip", "xz", "sed", "awk", "grep", "cut",
    "xxd", "openssl", "python3", "rsync", "sshpass", "dtc", "lz4", "zstd", "dpkg-deb",
    "bzip2", "fdisk", "sgdisk", "chroot", "strings", "lsblk", "blkid", "modinfo",
    "mkfs.ext4", "mkfs.vfat", "losetup", "mount", "umount", "sha256sum",
    "qemu-aarch64-static", "exportfs", "showmount", "rpcbind", "ssh", "ip",
    "ping6", "udevadm", "xmllint", "xmlstarlet", "abootimg", "systemctl",
)


class RecoveryError(RuntimeError):
    """Ошибка, которую можно безопасно показать пользователю."""


_BOARD_GUARD = '''# GEACX1 32 GB / JetPack 7.2: проверка реальной EEPROM до выбора DTB.
source "${LDK_DIR}/p3737-0000-p3701-0000-geacx1-vendor.conf"

update_flash_args()
{
    if [ "${board_id}" != "3701" ] || [ "${board_sku}" != "0004" ]; then
        echo "ОШИБКА: ожидался модуль AGX Orin 32GB 3701/0004, обнаружен ${board_id}/${board_sku}." >&2
        echo "Прошивка остановлена: подмена BOARDID/BOARDSKU запрещена." >&2
        exit 1
    fi
    update_flash_args_common
    update_flash_args_p3737_0000_p3701_0000
}
'''

_QSPI_GUARD = '''# Только загрузочная QSPI; проверка модуля наследуется.
source "${LDK_DIR}/geacx1-32gb-jp72.conf"
EMMC_CFG="flash_t234_qspi.xml"
NO_ROOTFS=1
NO_RECOVERY_IMG=1
'''

_FAN_GPIO_HELPER = '''#!/bin/bash
set -eu
case "${1-}" in
    0|1) value="$1" ;;
    *) echo "usage: $0 0|1" >&2; exit 2 ;;
esac
found=0
for value_file in /sys/class/rb_gpio/fan_*-power/value; do
    [ -e "${value_file}" ] || continue
    printf '%s\n' "${value}" > "${value_file}"
    found=1
done
if [ "${found}" -ne 1 ]; then
    echo "GEACX1 fan GPIO not found" >&2
    exit 1
fi
'''


def _release(l4t: Path) -> str:
    release_file = l4t / "nv_tegra/nv_tegra_release"
    try:
        first = release_file.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:
        raise RecoveryError(f"Не удалось прочитать версию BSP: {release_file}") from exc
    if not re.search(r"\bR39\s*\(release\),\s*REVISION:\s*2\.0\b", first):
        raise RecoveryError(f"Нужен BSP R39.2.0 (JetPack 7.2), найдено: {first}")
    return "R39.2.0"


def _missing(base: Path, names: Iterable[str]) -> List[str]:
    return [name for name in names if not (base / name).is_file()]


def inspect_bundle(vendor: Path) -> dict:
    vendor = Path(vendor)
    l4t = vendor / "Linux_for_Tegra"
    if not l4t.is_dir():
        raise RecoveryError(f"Не найден каталог Linux_for_Tegra в {vendor}")
    release = _release(l4t)
    missing = _missing(l4t, REQUIRED_L4T)
    missing += [f"{BOARD_DIR}/{p}" for p in _missing(vendor / BOARD_DIR, REQUIRED_BOARD)]
    missing += [f"{KERNEL_DIR}/{p}" for p in _missing(vendor / KERNEL_DIR, REQUIRED_KERNEL)]
    if not (vendor / FAN_PACKAGE).is_file():
        missing.append(FAN_PACKAGE)
    modules = vendor / KERNEL_DIR / "modules"
    if not modules.is_dir() or not any(modules.glob("*/modules.dep")):
        missing.append(f"{KERNEL_DIR}/modules/<версия>/modules.dep")
    if missing:
        raise RecoveryError("Не хватает обязательных файлов производителя: " + ", ".join(missing))

    rootfs = l4t / "rootfs"
    rootfs_empty = not rootfs.exists() or next(rootfs.iterdir(), None) is None
    messages = [f"BSP NVIDIA: {release}", "Плата: GEACX1, модуль AGX Orin 32 GB (SKU 0004)"]
    messages.append("Встроенный rootfs пуст — требуется официальный sample rootfs." if rootfs_empty
                    else "В BSP уже есть rootfs; prepare всё равно создаст чистую рабочую копию.")
    return {
        "release": release,
        "rootfs_empty": rootfs_empty,
        "board_dir": str(vendor / BOARD_DIR),
        "kernel_dir": str(vendor / KERNEL_DIR),
        "messages": messages,
    }


def _safe_member_name(name: str) -> PurePosixPath:
    raw = PurePosixPath(name)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise RecoveryError(f"Небезопасный путь в rootfs: {name}")
    parts = tuple(p for p in raw.parts if p not in ("", "."))
    if not parts:
        raise RecoveryError(f"Небезопасный пустой путь в rootfs: {name}")
    return PurePosixPath(*parts)


def _safe_link_target(member_path: PurePosixPath, target: str, *, hardlink: bool) -> PurePosixPath:
    link = PurePosixPath(target)
    if hardlink and link.is_absolute():
        raise RecoveryError(f"Небезопасный hardlink в rootfs: {member_path} -> {target}")
    # Абсолютная symlink разрешена как объект; валидатор никогда её не разыменовывает.
    if link.is_absolute():
        return PurePosixPath(*link.parts[1:])
    base = PurePosixPath() if hardlink else member_path.parent
    stack: List[str] = []
    for part in (base / link).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                kind = "hardlink" if hardlink else "symlink"
                raise RecoveryError(f"Небезопасный {kind} в rootfs: {member_path} -> {target}")
            stack.pop()
        else:
            stack.append(part)
    return PurePosixPath(*stack)


def _preflight_archive(path: Path, samples: Optional[Dict[str, bytes]] = None) -> Dict[str, tarfile.TarInfo]:
    try:
        archive = tarfile.open(path, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError(f"Rootfs повреждён или имеет неверный формат: {path}") from exc
    with archive:
        members: Dict[str, tarfile.TarInfo] = {}
        links: Dict[PurePosixPath, PurePosixPath] = {}
        hardlinks: List[tuple] = []
        for member in archive:
            raw_parts = tuple(part for part in PurePosixPath(member.name).parts if part not in ("", "."))
            if not raw_parts and member.isdir():
                continue
            member_path = _safe_member_name(member.name)
            key = str(member_path)
            if key in members:
                raise RecoveryError(f"Небезопасный повтор пути в rootfs: {member.name}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise RecoveryError(f"Небезопасный специальный файл в rootfs: {member.name}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RecoveryError(f"Небезопасный тип записи в rootfs: {member.name}")
            if member.issym():
                links[member_path] = _safe_link_target(member_path, member.linkname, hardlink=False)
            elif member.islnk():
                target = _safe_link_target(member_path, member.linkname, hardlink=True)
                hardlinks.append((member_path, target))
            members[key] = member
            if samples is not None and member.isfile() and key in {
                "etc/os-release", "usr/lib/os-release", "bin/bash", "usr/bin/bash"
            }:
                # Read tiny headers while the decompressor is already here.
                # Re-opening bz2 and resolving a symlink otherwise scans it
                # repeatedly, which is especially expensive on slow laptops.
                stream = archive.extractfile(member)
                if stream is not None:
                    samples[key] = stream.read(65536 if key.endswith("os-release") else 64)

        # Ни один файл архива не может оказаться внутри symlink-каталога.
        for name in (PurePosixPath(item) for item in members):
            for parent in name.parents:
                if parent in links:
                    raise RecoveryError(f"Небезопасная запись через symlink в rootfs: {name}")
        for name, target in hardlinks:
            target_member = members.get(str(target))
            if target_member is None or not target_member.isfile():
                raise RecoveryError(f"Небезопасный hardlink в rootfs: {name} -> {target}")
            for parent in target.parents:
                if parent in links:
                    raise RecoveryError(f"Небезопасный hardlink через symlink в rootfs: {name}")
        return members


def _parse_os_release(data: bytes) -> Dict[str, str]:
    result = {}
    for line in data.decode("utf-8", "replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"\'')
    return result


def _elf_arch(data: bytes) -> str:
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return "не ELF"
    byteorder = "little" if data[5] == 1 else "big"
    machine = int.from_bytes(data[18:20], byteorder)
    return "aarch64" if machine == 183 else f"ELF machine {machine}"


def validate_rootfs(path: Path) -> dict:
    """Проверяет безопасность, Ubuntu 24.04 и aarch64 без распаковки."""
    path = Path(path)
    if path.is_dir():
        try:
            os_data = (path / "etc/os-release").read_bytes()
            elf_data = (path / "bin/bash").read_bytes()[:64]
        except OSError as exc:
            raise RecoveryError("Rootfs неполный: нужны etc/os-release и bin/bash") from exc
        count = sum(1 for _ in path.rglob("*"))
    else:
        samples: Dict[str, bytes] = {}
        members = _preflight_archive(path, samples)
        try:
            os_name = "etc/os-release"
            seen = set()
            while not members[os_name].isfile():
                if os_name in seen:
                    raise KeyError("os-release symlink cycle")
                seen.add(os_name)
                member = members[os_name]
                if not (member.issym() or member.islnk()):
                    raise KeyError("os-release is not a file")
                os_name = str(_safe_link_target(PurePosixPath(os_name), member.linkname,
                                               hardlink=member.islnk()))
            os_data = samples[os_name]
            elf_data = samples.get("bin/bash", samples.get("usr/bin/bash"))
            if elf_data is None:
                raise KeyError("bin/bash")
        except (KeyError, OSError, tarfile.TarError) as exc:
            raise RecoveryError("Rootfs неполный: нужны etc/os-release и aarch64 bin/bash") from exc
        count = len(members)

    release = _parse_os_release(os_data)
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        found = f"{release.get('ID', '?')} {release.get('VERSION_ID', '?')}"
        raise RecoveryError(f"Для JetPack 7.2 нужен Ubuntu 24.04 rootfs, найдено: {found}")
    architecture = _elf_arch(elf_data)
    if architecture != "aarch64":
        raise RecoveryError(f"Нужен rootfs aarch64, найдено: {architecture}")
    return {"version": "24.04", "architecture": architecture, "entries": count, "path": str(path)}


def validate_archive(path: Path) -> dict:
    """Совместимый публичный alias полной проверки rootfs-архива."""
    return validate_rootfs(path)


def sanitized_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if env is None else env
    # Whitelist не пропускает BASH_FUNC_*, loader/python hooks и новые NVIDIA overrides.
    safe_names = {"HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "TERM", "TMPDIR"}
    clean = {
        key: value for key, value in source.items()
        if key in safe_names and key.upper() not in _OVERRIDE_ENV
    }
    clean["PATH"] = _CLEAN_PATH
    # flash.sh checks USER, not geteuid(). sudo/root shells and minimal Linux
    # environments do not always supply this variable consistently.
    if os.geteuid() == 0:
        clean.update(USER="root", LOGNAME="root", HOME=pwd.getpwuid(0).pw_dir)
    return clean


def build_command(l4t: Path, mode: str) -> List[str]:
    del l4t  # cwd передаётся отдельно; абсолютные пути в argv намеренно не нужны.
    if mode == "emmc":
        return ["./flash.sh", BOARD_NAME, "mmcblk0p1"]
    if mode == "qspi":
        return ["./flash.sh", QSPI_BOARD_NAME, "internal"]
    if mode == "nvme":
        return [
            "./tools/kernel_flash/l4t_initrd_flash.sh",
            "-c", "tools/kernel_flash/flash_l4t_external.xml",
            "-p", "-c bootloader/generic/cfg/flash_t234_qspi.xml --no-systemimg",
            "--network", "usb0", "--showlogs", "--external-device", "nvme0n1p1",
            BOARD_NAME, "external",
        ]
    raise RecoveryError(f"Неизвестный режим прошивки: {mode}")


def check_runtime() -> List[str]:
    return [name for name in _RUNTIME_TOOLS if shutil.which(name) is None]


def install_host_dependencies(vendor: Path, run) -> None:
    l4t = Path(vendor) / "Linux_for_Tegra"
    run(["bash", str(l4t / "tools/l4t_flash_prerequisites.sh")], l4t)
    # The NVIDIA list assumes these desktop tools already exist. A clean Noble
    # container demonstrates that bzip2, xz and fdisk are not installed by it.
    run(["apt-get", "-o", "DPkg::Lock::Timeout=120", "-o", "Acquire::Retries=3",
         "install", "-y", "bzip2", "xz-utils", "fdisk", "kmod"], l4t)
    missing = check_runtime()
    if missing:
        raise RecoveryError("Установка зависимостей не завершена. Нет команд: " + ", ".join(missing))
    run(["python3", "-c", "import yaml; import usb.core"], l4t)


def require_initrd_network() -> None:
    """Catch known NVIDIA USB/NFS prerequisites before touching the device."""
    for setting in ("all", "default"):
        path = Path(f"/proc/sys/net/ipv6/conf/{setting}/disable_ipv6")
        try:
            disabled = path.read_text().strip()
        except OSError as exc:
            raise RecoveryError("Не удалось проверить IPv6: USB initrd требует IPv6 на Ubuntu-хосте.") from exc
        if disabled != "0":
            raise RecoveryError(
                f"IPv6 отключён: net.ipv6.conf.{setting}.disable_ipv6={disabled}. "
                "Для NVMe и резервных копий включите IPv6 на компьютере; "
                "затем повторите проверку. Полное восстановление eMMC обходится без NFS."
            )
    if shutil.which("ufw"):
        env = sanitized_env()
        env["LC_ALL"] = "C"
        result = subprocess.run(["ufw", "status", "verbose"], capture_output=True,
                                text=True, env=env, check=False)
        if result.returncode:
            raise RecoveryError("Не удалось прочитать состояние UFW; проверьте sudo ufw status verbose.")
        # Match the exact guard in NVIDIA's l4t_network_flash.func, otherwise
        # the tool would reject the host later during the live USB procedure.
        if "Status: active" in result.stdout and not re.search(r"(?m)^2049\s+ALLOW\s", result.stdout):
            raise RecoveryError(
                "Активный UFW не содержит правила NFS 2049, которое распознаёт утилита NVIDIA. "
                "Настройте доступ NFS для подключения платы и проверьте sudo ufw status verbose. "
                "Подробнее: REFERENCE_RU.html, раздел ошибок. Запись ещё не началась."
            )


def _mount_fstype(path: Path) -> Optional[str]:
    try:
        existing = path.resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        best = (0, None)
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields = left.split()
            mountpoint = fields[4].replace("\\040", " ")
            if str(existing) == mountpoint or str(existing).startswith(mountpoint.rstrip("/") + "/"):
                if len(mountpoint) > best[0]:
                    best = (len(mountpoint), right.split()[0])
        return best[1]
    except (OSError, ValueError, IndexError):
        return None


def host_checks(work: Path) -> List[dict]:
    work = Path(work)
    try:
        os_release = _parse_os_release(Path("/etc/os-release").read_bytes())
    except OSError:
        os_release = {}
    linux = platform.system() == "Linux"
    ubuntu = os_release.get("ID") == "ubuntu" and os_release.get("VERSION_ID") in {"22.04", "24.04"}
    arch = platform.machine().lower() in {"x86_64", "amd64"}
    try:
        version_text = Path("/proc/version").read_text(errors="ignore").lower()
        cgroup = Path("/proc/1/cgroup").read_text(errors="ignore").lower()
    except OSError:
        version_text, cgroup = "", ""
    virtualized = "microsoft" in version_text or Path("/.dockerenv").exists() or "docker" in cgroup
    safe_path = work.is_absolute() and str(work).isascii() and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", str(work)))
    probe = work
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = 0
    fstype = _mount_fstype(work)
    return [
        {"name": "Linux", "ok": linux, "detail": platform.system()},
        {"name": "Ubuntu", "ok": ubuntu, "detail": f"{os_release.get('ID', '?')} {os_release.get('VERSION_ID', '?')}"},
        {"name": "Архитектура хоста", "ok": arch, "detail": platform.machine()},
        {"name": "Нативный хост", "ok": not virtualized, "detail": "WSL/Docker не поддерживаются" if virtualized else "нативная система"},
        {"name": "Файловая система", "ok": fstype == "ext4", "detail": fstype or "не определена"},
        {"name": "Безопасный путь", "ok": safe_path, "detail": str(work)},
        {"name": "Свободное место", "ok": free >= 80 * 1024 ** 3, "detail": f"{free / 1024 ** 3:.1f} GiB"},
    ]


def recovery_devices() -> List[dict]:
    devices = []
    if not SYS_USB_DEVICES.is_dir():
        return devices
    for entry in sorted(SYS_USB_DEVICES.iterdir()):
        try:
            vendor = (entry / "idVendor").read_text().strip().lower()
            product_id = (entry / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if vendor != "0955":
            continue
        def optional(name: str) -> str:
            try:
                return (entry / name).read_text(errors="replace").strip()
            except OSError:
                return ""
        devices.append({
            "sysfs": str(entry), "vendor_id": vendor, "product_id": product_id,
            "supported": product_id == "7023", "family": "AGX Orin" if product_id == "7023" else "NVIDIA (неизвестная модель)",
            "product": optional("product"), "serial": optional("serial"),
            "detail": "USB PID подтверждает только recovery-семейство; SKU 32 GB проверит EEPROM при прошивке.",
        })
    return devices


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _inside(root: Path, candidate: Path) -> bool:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve(strict=False)
    try:
        return os.path.commonpath((str(root_resolved), str(candidate_resolved))) == str(root_resolved)
    except ValueError:
        return False


def _all_symlinks(root: Path) -> List[Path]:
    links: List[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            candidate = current_path / name
            if candidate.is_symlink():
                links.append(candidate)
    return links


def _normalize_and_check_symlinks(root: Path) -> None:
    """Делает absolute rootfs-links относительными и проверяет все цепочки."""
    root = root.resolve()
    links = _all_symlinks(root)
    for link in links:
        target = os.readlink(link)
        if os.path.isabs(target):
            root_target = root.joinpath(*PurePosixPath(target).parts[1:])
            relative = os.path.relpath(root_target, start=link.parent)
            link.unlink()
            link.symlink_to(relative)

    for link in links:
        target = os.readlink(link)
        lexical = Path(os.path.normpath(str(link.parent / target)))
        try:
            if os.path.commonpath((str(root), str(lexical))) != str(root):
                raise RecoveryError(f"Rootfs symlink выходит за пределы rootfs: {link}")
        except ValueError as exc:
            raise RecoveryError(f"Rootfs symlink выходит за пределы rootfs: {link}") from exc
        try:
            resolved = link.resolve(strict=False)
        except RuntimeError as exc:
            raise RecoveryError(f"Обнаружен цикл symlink в rootfs: {link}") from exc
        if not _inside(root, resolved):
            raise RecoveryError(f"Rootfs symlink выходит за пределы rootfs: {link}")


def _remove_generated_images(l4t: Path) -> None:
    generated = [
        l4t / "bootloader/system.img", l4t / "bootloader/system.img.raw",
        l4t / "bootloader/flashcmd.txt", l4t / "bootloader/cvm.bin",
        l4t / "bootloader/signed", l4t / "tools/kernel_flash/images",
    ]
    for path in generated:
        if not (path.exists() or path.is_symlink()):
            continue
        if not _inside(l4t, path):
            raise RecoveryError(f"Небезопасный путь старого образа: {path}")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _install_fan_service(vendor: Path, l4t: Path,
                         run: Callable[[List[str], Path], None]) -> None:
    package = vendor / FAN_PACKAGE
    actual = _sha256(package)
    if actual != FAN_PACKAGE_SHA256:
        raise RecoveryError(f"SHA-256 пакета вентилятора не совпал: {FAN_PACKAGE}")
    rootfs = l4t / "rootfs"
    staging = l4t / ".fan-payload"
    if staging.exists() or staging.is_symlink():
        raise RecoveryError("В новой рабочей копии неожиданно найден fan staging")
    staging.mkdir()
    # Извлечение в staging не запускает maintainer scripts и не может заменить usrmerge links.
    run(["dpkg-deb", "-x", str(package), str(staging)], l4t)
    modes = {
        "lib/systemd/system/rb-jetson-service-fan.service": 0o644,
        "etc/rb/rb-jetson-service-fan/config.json": 0o644,
        "opt/rb/rb-jetson-service-fan/fanctl.py": 0o755,
        "opt/rb/rb-jetson-service-fan/fan_gpio_ctrl.sh": 0o755,
        "opt/rb/rb-jetson-service-fan/md5sums": 0o644,
    }
    try:
        for relative, mode in modes.items():
            source = staging / relative
            target = rootfs / relative
            if not source.is_file() or source.is_symlink() or not _inside(staging, source):
                raise RecoveryError(f"Пакет вентилятора не извлёк обязательный файл: {relative}")
            if not _inside(rootfs, target):
                raise RecoveryError(f"Rootfs перенаправляет fan-файл наружу: {relative}")
            _copy_file(source, target)
            target.chmod(mode)
    finally:
        shutil.rmtree(staging)
    helper = rootfs / "opt/rb/rb-jetson-service-fan/fan_gpio_ctrl.sh"
    helper.write_text(_FAN_GPIO_HELPER)
    helper.chmod(0o755)
    wants = rootfs / "etc/systemd/system/multi-user.target.wants"
    if not _inside(rootfs, wants):
        raise RecoveryError("Небезопасный systemd wants-путь в rootfs")
    wants.mkdir(parents=True, exist_ok=True)
    enabled = wants / "rb-jetson-service-fan.service"
    if enabled.exists() or enabled.is_symlink():
        enabled.unlink()
    service = rootfs / "lib/systemd/system/rb-jetson-service-fan.service"
    enabled.symlink_to(os.path.relpath(service, start=wants))
    _normalize_and_check_symlinks(rootfs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_vendor_boot(l4t: Path) -> None:
    """Undo the RT default installed by apply_binaries before adding vendor initrd.

    R39.2 installs both NVIDIA kernel flavors. The manufacturer's overlay only
    contains its non-RT modules; nv-update-initrd nevertheless scans both Image
    paths. Leaving Image.real-time would either break initrd generation or boot
    the wrong kernel. Only this fresh, disposable rootfs is modified.
    """
    rootfs = l4t / "rootfs"
    rt_image = rootfs / "boot/Image.real-time"
    if rt_image.exists() or rt_image.is_symlink():
        if rt_image.is_dir() and not rt_image.is_symlink():
            raise RecoveryError("Вместо ядра Image.real-time обнаружен каталог")
        rt_image.unlink()
    _copy_file(l4t / "bootloader/extlinux.conf", rootfs / "boot/extlinux/extlinux.conf")
    _verify_vendor_boot(l4t)


def _verify_vendor_boot(l4t: Path) -> None:
    config = l4t / "rootfs/boot/extlinux/extlinux.conf"
    try:
        fields = {name: [] for name in ("DEFAULT", "LABEL", "LINUX", "INITRD")}
        for line in config.read_text().splitlines():
            active = line.strip()
            if not active or active.startswith("#"):
                continue
            parts = active.split(None, 1)
            if parts[0].upper() in fields:
                fields[parts[0].upper()].append(parts[1] if len(parts) == 2 else "")
    except OSError as exc:
        raise RecoveryError("Отсутствует загрузочный extlinux.conf") from exc
    if fields != {"DEFAULT": ["primary"], "LABEL": ["primary"],
                  "LINUX": ["/boot/Image"], "INITRD": ["/boot/initrd"]}:
        raise RecoveryError("extlinux.conf должен загружать только vendor /boot/Image и /boot/initrd через DEFAULT primary")
    rt_image = l4t / "rootfs/boot/Image.real-time"
    if rt_image.exists() or rt_image.is_symlink():
        raise RecoveryError("В rootfs вернулось несовместимое ядро Image.real-time; нужна новая подготовка")


def missing_module_dependencies(modules: Path) -> List[str]:
    """Compare exact Linux names, including case, with the vendor depmod index."""
    modules = Path(modules)
    available = {str(path.relative_to(modules)) for path in modules.rglob("*")
                 if path.is_file() and not path.is_symlink()}
    expected = set()
    for line in (modules / "modules.dep").read_text().splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise RecoveryError("Повреждён modules.dep: отсутствует разделитель")
        name, dependencies = line.split(":", 1)
        for value in [name, *dependencies.split()]:
            safe = _safe_member_name(value)
            if str(safe) != value or not value.endswith((".ko", ".ko.zst", ".ko.xz", ".ko.gz")):
                raise RecoveryError(f"Неверный путь модуля в modules.dep: {value}")
            expected.add(value)
    if not expected:
        raise RecoveryError("modules.dep не содержит модулей ядра")
    return sorted(expected - available)


def _repair_module_case_loss(vendor: Path, l4t: Path, run, progress) -> None:
    modules = l4t / "rootfs/lib/modules" / KERNEL_VERSION
    missing = missing_module_dependencies(modules)
    if not missing:
        return
    # APFS/NTFS can merge these two names. The original DEB preserves both;
    # only this verified missing payload is restored, never the older DEB Image.
    repairable = {"kernel/net/netfilter/xt_rateest.ko":
                  "4d0445f2b07509554efee1445a6a45a3edb25100c9cd93f4fab46066bb359100"}
    if any(name not in repairable for name in missing):
        raise RecoveryError("В vendor-дереве отсутствуют модули: " + ", ".join(missing))
    package = vendor / KERNEL_PACKAGE
    if not package.is_file() or _sha256(package) != KERNEL_PACKAGE_SHA256:
        raise RecoveryError("Нужен неповреждённый исходный kernel DEB для восстановления регистра имён модулей")
    progress("Восстановление модуля, потерянного при распаковке на диске без учёта регистра…")
    staging = l4t / ".kernel-module-repair"
    staging.mkdir(mode=0o700)
    try:
        run(["dpkg-deb", "-x", str(package), str(staging)], l4t)
        source_modules = staging / "opt/rb/rb-jetson-firmware-kernel-jp7.2-GA-source/modules" / KERNEL_VERSION
        for name in missing:
            source = source_modules / name
            if source.is_symlink() or not source.is_file() or _sha256(source) != repairable[name]:
                raise RecoveryError(f"Kernel DEB не содержит проверенного модуля: {name}")
            _copy_file(source, modules / name)
        remaining = missing_module_dependencies(modules)
        if remaining:
            raise RecoveryError("Диск не сохраняет регистр Linux-файлов; используйте ext4: " + ", ".join(remaining))
    finally:
        shutil.rmtree(staging)


def verify_rootfs_identity(path: Path) -> None:
    path = Path(path)
    if not path.is_file() or _sha256(path) != ROOTFS_SHA256:
        raise RecoveryError(
            "SHA-256 rootfs не совпал с закреплённым NVIDIA R39.2.0. "
            "Используйте архив из комплекта или скачайте его заново через мастер."
        )


_MARKER_FILES = (
    "flash.sh", "apply_binaries.sh", "p3701.conf.common",
    "p3737-0000-p3701-0000-geacx1-vendor.conf", "geacx1-32gb-jp72.conf",
    "geacx1-32gb-jp72-qspi.conf", "kernel/Image", "bootloader/l4t_initrd.img",
    "rootfs/boot/Image", "rootfs/boot/initrd",
    "bootloader/extlinux.conf",
    "rootfs/lib/modules/6.8.12-1021-tegra/modules.dep",
    "bootloader/generic/cfg/flash_t234_qspi.xml",
    "bootloader/generic/cfg/flash_t234_qspi_sdmmc.xml",
    "tools/kernel_flash/flash_l4t_external.xml",
    "tools/backup_restore/l4t_backup_restore.sh",
    "tools/backup_restore/l4t_backup_restore.func",
    "tools/backup_restore/nvbackup_partitions.sh",
    "tools/backup_restore/nvrestore_partitions.sh",
    "tools/kernel_flash/l4t_initrd_flash.func",
    "tools/kernel_flash/l4t_network_flash.func",
    "tools/kernel_flash/l4t_kernel_flash_vars.func",
    "bootloader/tegra234-mb1-bct-gpio-p3701-0000-a04.dtsi",
    "bootloader/tegra234-mb2-bct-common.dtsi",
    "bootloader/generic/BCT/tegra234-mb1-bct-pinmux-p3701-0000-a04.dtsi",
    "rootfs/lib/systemd/system/rb-jetson-service-fan.service",
    "rootfs/etc/rb/rb-jetson-service-fan/config.json",
    "rootfs/opt/rb/rb-jetson-service-fan/fanctl.py",
    "rootfs/opt/rb/rb-jetson-service-fan/fan_gpio_ctrl.sh",
    "rootfs/opt/rb/rb-jetson-service-fan/md5sums",
)


def _marker_files(l4t: Path) -> List[str]:
    names = set(_MARKER_FILES)
    for path in (l4t / "kernel/dtb").glob("tegra234-p3737-0000+p3701-*-nv*.dtb"):
        if path.is_file():
            names.add(str(path.relative_to(l4t)))
    for path in (l4t / "tools/kernel_flash").rglob("*.sh"):
        if path.is_file():
            names.add(str(path.relative_to(l4t)))
    for path in (l4t / "rootfs/lib/modules").rglob("*"):
        if path.is_file() and not path.is_symlink():
            names.add(str(path.relative_to(l4t)))
    return sorted(names)


def _verify_boot_mirrors(l4t: Path) -> None:
    pairs = (
        ("kernel/Image", "rootfs/boot/Image"),
        ("bootloader/l4t_initrd.img", "rootfs/boot/initrd"),
    )
    for source, mirror in pairs:
        if _sha256(l4t / source) != _sha256(l4t / mirror):
            raise RecoveryError(f"Загрузочный файл rootfs не совпадает с источником: {mirror}")


def _verify_fan_enabled(l4t: Path) -> None:
    rootfs = l4t / "rootfs"
    link = rootfs / "etc/systemd/system/multi-user.target.wants/rb-jetson-service-fan.service"
    service = rootfs / "lib/systemd/system/rb-jetson-service-fan.service"
    if not link.is_symlink():
        raise RecoveryError("Сервис вентилятора не включён в multi-user.target")
    try:
        resolved = link.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RecoveryError("Ссылка сервиса вентилятора повреждена") from exc
    if resolved != service.resolve(strict=True) or not _inside(rootfs, resolved):
        raise RecoveryError("Ссылка сервиса вентилятора ведёт на неверный файл")


def _write_marker(l4t: Path) -> None:
    _verify_boot_mirrors(l4t)
    _verify_vendor_boot(l4t)
    _verify_fan_enabled(l4t)
    missing = missing_module_dependencies(l4t / "rootfs/lib/modules" / KERNEL_VERSION)
    if missing:
        raise RecoveryError("Отсутствуют модули из modules.dep: " + ", ".join(missing))
    hashes = {name: _sha256(l4t / name) for name in _marker_files(l4t)}
    marker = {"format": 1, "release": "R39.2.0", "board": BOARD_NAME, "sha256": hashes}
    target = l4t / ".geacx1-prepared.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, target)


def validate_prepared(l4t: Path) -> dict:
    l4t = Path(l4t)
    release = _release(l4t)
    network = l4t / "tools/kernel_flash/l4t_network_flash.func"
    if not network.is_file() or _sha256(network) != NETWORK_WAIT_SHA256:
        raise RecoveryError("Рабочая копия не содержит проверенное ожидание USB-сети v1.4. Выполните новую подготовку.")
    marker_path = l4t / ".geacx1-prepared.json"
    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, ValueError) as exc:
        raise RecoveryError("Рабочая копия не завершена: отсутствует корректный marker") from exc
    if marker.get("format") != 1 or marker.get("release") != release or marker.get("board") != BOARD_NAME:
        raise RecoveryError("Marker рабочей копии не соответствует GEACX1 / R39.2.0")
    if (l4t / f"{BOARD_NAME}.conf").read_text() != _BOARD_GUARD:
        raise RecoveryError("Защитный board-конфиг изменён; нужна новая подготовка")
    if (l4t / f"{QSPI_BOARD_NAME}.conf").read_text() != _QSPI_GUARD:
        raise RecoveryError("Защитный QSPI-конфиг изменён; нужна новая подготовка")
    hashes = marker.get("sha256")
    if not isinstance(hashes, dict):
        raise RecoveryError("Marker не содержит контрольных сумм")
    expected_names = set(_marker_files(l4t))
    if set(hashes) != expected_names:
        raise RecoveryError("Набор защищённых marker-файлов изменился; нужна новая подготовка")
    for name in sorted(expected_names):
        path = l4t / name
        if not path.is_file() or hashes.get(name) != _sha256(path):
            raise RecoveryError(f"Контрольная сумма подготовленного файла не совпала: {name}")
    _verify_boot_mirrors(l4t)
    _verify_vendor_boot(l4t)
    _verify_fan_enabled(l4t)
    for config in (l4t / f"{BOARD_NAME}.conf", l4t / f"{QSPI_BOARD_NAME}.conf"):
        if re.search(r"(?m)^\s*(BOARDID|BOARDSKU|FAB|BOARDREV)\s*=", config.read_text()):
            raise RecoveryError(f"Обнаружена запрещённая подмена идентификатора: {config.name}")
    return marker


@contextlib.contextmanager
def preserve_prepared_initrd(l4t: Path):
    """Keep the verified host inputs reusable after NVIDIA rebuilds initrd.

    flash.sh rebuilds these two files even when they were already prepared.
    Preserve their exact bytes instead of trusting new hashes after a command.
    Generated images and the target board are not changed by this cleanup.
    """
    l4t = Path(l4t)
    saved = Path(tempfile.mkdtemp(prefix=".geacx1-initrd-", dir=l4t.parent))
    snapshots = []
    restored = False
    try:
        for index, relative in enumerate(("bootloader/l4t_initrd.img", "rootfs/boot/initrd")):
            target = l4t / relative
            if target.is_symlink() or not target.is_file():
                raise RecoveryError(f"Ожидался обычный подготовленный initrd: {relative}")
            snapshot = saved / str(index)
            shutil.copy2(target, snapshot)
            if _sha256(snapshot) != _sha256(target):
                raise RecoveryError(f"Не удалось сохранить рабочий initrd: {relative}")
            snapshots.append((snapshot, target))
        yield
    finally:
        try:
            for snapshot, target in snapshots:
                fd, name = tempfile.mkstemp(prefix=".initrd-restore-", dir=target.parent)
                os.close(fd)
                temporary = Path(name)
                try:
                    shutil.copy2(snapshot, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            restored = True
        except OSError as exc:
            raise RecoveryError(f"Не удалось вернуть исходные initrd на ПК. Копии сохранены в {saved}") from exc
        finally:
            if restored:
                shutil.rmtree(saved)
    # Only on normal completion: preserve the original command error otherwise.
    validate_prepared(l4t)


def extend_initrd_network_wait(l4t: Path) -> None:
    """Allow a slow initrd USB network to appear; never retry a flash command.

    R39.2's -t option is not parsed by every wrapper. Change only the exact
    known function in the disposable work copy, keeping explicit timeouts.
    """
    path = l4t / "tools/kernel_flash/l4t_network_flash.func"
    if not path.is_file() or _sha256(path) != NETWORK_FLASH_SHA256:
        raise RecoveryError("Неизвестная версия l4t_network_flash.func; ожидание USB-сети не изменено.")
    old = "wait_for_flash_ssh()\n{\n\tmaxcount=${timeout:-60}\n"
    new = "wait_for_flash_ssh()\n{\n\tmaxcount=${timeout:-300}\n"
    text = path.read_text()
    if text.count(old) != 1:
        raise RecoveryError("Не найден точный блок ожидания USB-сети; подготовка остановлена.")
    path.write_text(text.replace(old, new, 1))


def prepare(vendor: Path, rootfs: Path, work: Path,
            run: Callable[[List[str], Path], None], progress: Callable[[str], None]) -> Path:
    vendor = Path(vendor).resolve()
    rootfs = Path(rootfs).resolve()
    work = Path(work).resolve(strict=False)
    if os.geteuid() != 0:
        raise RecoveryError("Подготовку rootfs нужно запустить от root (sudo).")
    if work.exists():
        raise RecoveryError("Рабочий каталог должен быть новым: старые system.img использовать нельзя.")
    if not work.is_absolute() or not str(work).isascii() or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(work)):
        raise RecoveryError("Рабочий путь должен быть абсолютным, ASCII, без пробелов и shell-символов.")
    if _inside(vendor, work) or _inside(work, vendor):
        raise RecoveryError("Рабочий каталог и исходный vendor-набор не должны пересекаться.")
    report = inspect_bundle(vendor)
    if not report["rootfs_empty"]:
        raise RecoveryError("Исходный vendor rootfs должен быть пуст: повторное использование образа запрещено.")
    progress("Создание защищённой рабочей копии архива rootfs…")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    inputs = work / ".inputs"
    inputs.mkdir(mode=0o700)
    snapshot = inputs / ROOTFS_NAME
    with rootfs.open("rb") as source, snapshot.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    snapshot.chmod(0o400)
    progress("Проверка официального Ubuntu 24.04 aarch64 rootfs…")
    verify_rootfs_identity(snapshot)
    validate_rootfs(snapshot)

    progress("Создание чистой рабочей копии BSP…")
    l4t = work / "Linux_for_Tegra"
    shutil.copytree(vendor / "Linux_for_Tegra", l4t, symlinks=True)
    target_rootfs = l4t / "rootfs"
    if target_rootfs.exists() or target_rootfs.is_symlink():
        if target_rootfs.is_symlink() or target_rootfs.is_file():
            raise RecoveryError("Vendor rootfs неожиданно оказался ссылкой или файлом.")
        shutil.rmtree(target_rootfs)
    target_rootfs.mkdir(parents=True, exist_ok=True)
    _remove_generated_images(l4t)

    progress("Распаковка rootfs с сохранением владельцев и прав…")
    run(["tar", "--numeric-owner", "-xpf", str(snapshot), "-C", str(target_rootfs)], l4t)
    _normalize_and_check_symlinks(target_rootfs)
    validate_rootfs(target_rootfs)
    progress("Установка бинарных компонентов NVIDIA…")
    run(["./apply_binaries.sh"], l4t)
    progress("Установка проверенного сервиса охлаждения GEACX1 без postinst…")
    _install_fan_service(vendor, l4t, run)

    board = vendor / BOARD_DIR
    kernel = vendor / KERNEL_DIR
    progress("Установка проверенной конфигурации GEACX1 и ядра…")
    _copy_file(board / "p3701.conf.common", l4t / "p3701.conf.common")
    _copy_file(board / "p3737-0000-p3701-0000.conf", l4t / "p3737-0000-p3701-0000-geacx1-vendor.conf")
    _copy_file(board / "tegra234-mb1-bct-gpio-p3701-0000-a04.dtsi", l4t / "bootloader/tegra234-mb1-bct-gpio-p3701-0000-a04.dtsi")
    _copy_file(board / "tegra234-mb2-bct-common.dtsi", l4t / "bootloader/tegra234-mb2-bct-common.dtsi")
    _copy_file(board / "tegra234-mb1-bct-pinmux-p3701-0000-a04.dtsi", l4t / "bootloader/generic/BCT/tegra234-mb1-bct-pinmux-p3701-0000-a04.dtsi")
    _copy_file(board / "nv_recovery.sh", l4t / "tools/ota_tools/version_upgrade/nv_recovery.sh")
    _copy_file(board / "flash.sh", l4t / "flash.sh")
    for dtb in (board / "dtb").glob("*.dtb"):
        _copy_file(dtb, l4t / "kernel/dtb" / dtb.name)

    _copy_file(kernel / "boot/Image", l4t / "kernel/Image")
    _copy_file(kernel / "boot/Image", target_rootfs / "boot/Image")
    _copy_file(kernel / "boot/l4t_initrd.img", l4t / "bootloader/l4t_initrd.img")
    modules_target = target_rootfs / "lib/modules"
    if modules_target.exists() or modules_target.is_symlink():
        if modules_target.is_symlink() or modules_target.is_file():
            modules_target.unlink()
        else:
            shutil.rmtree(modules_target)
    shutil.copytree(kernel / "modules", modules_target, symlinks=True)
    _repair_module_case_loss(vendor, l4t, run, progress)
    _normalize_and_check_symlinks(target_rootfs)
    progress("Выбор ядра GEACX1 вместо несовместимого NVIDIA real-time ядра…")
    _select_vendor_boot(l4t)

    (l4t / f"{BOARD_NAME}.conf").write_text(_BOARD_GUARD)
    (l4t / f"{QSPI_BOARD_NAME}.conf").write_text(_QSPI_GUARD)
    backup_restore.harden_prepared_tools(l4t)
    progress("Увеличение ожидания появления USB-сети initrd для медленного подключения…")
    extend_initrd_network_wait(l4t)
    progress("Пересборка initrd после установки полного дерева модулей…")
    run(["./tools/l4t_update_initrd.sh"], l4t)
    _copy_file(l4t / "bootloader/l4t_initrd.img", target_rootfs / "boot/initrd")
    _write_marker(l4t)
    validate_prepared(l4t)
    progress("Подготовка завершена и проверена по SHA-256.")
    return l4t


def verify_manifest(bundle: Path) -> List[dict]:
    bundle = Path(bundle).resolve()
    manifest = bundle / "SHA256SUMS"
    if not manifest.is_file():
        raise RecoveryError(f"Не найден манифест SHA256SUMS в {bundle}")
    results = []
    for number, raw in enumerate(manifest.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *](.+)", line)
        if not match:
            raise RecoveryError(f"Некорректная строка SHA256SUMS #{number}")
        expected, name = match.group(1).lower(), match.group(2)
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RecoveryError(f"Небезопасный путь в SHA256SUMS: {name}")
        path = bundle.joinpath(*relative.parts)
        if not path.is_file():
            raise RecoveryError(f"Файл из SHA256SUMS не найден: {name}")
        actual = _sha256(path)
        ok = actual == expected
        results.append({"path": name, "ok": ok, "expected": expected, "actual": actual})
        if not ok:
            raise RecoveryError(f"SHA-256 не совпал: {name}")
    if not results:
        raise RecoveryError("SHA256SUMS пуст")
    return results


def download_rootfs(dest: Path, progress: Callable[[str], None]) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if dest.is_file():
        progress("Проверка уже скачанного rootfs…")
        verify_rootfs_identity(dest)
        validate_rootfs(dest)
        return dest
    progress(f"Скачивание {ROOTFS_NAME}…")
    progress("Лимит загрузки — 2 часа. В полном ZIP rootfs уже есть; повторная загрузка обычно не нужна.")
    started = time.monotonic()
    last_report = started - 5
    last_error: Optional[BaseException] = None
    max_attempts = 20
    for attempt in range(1, max_attempts + 1):
        attempt_started = time.monotonic()
        downloaded = 0
        remaining_time = DOWNLOAD_LIMIT_SECONDS - (attempt_started - started)
        if remaining_time <= 0:
            raise RecoveryError("Лимит загрузки 2 часа исчерпан. Файл .part сохранён; прошивка не начата.")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "GEACX1-Recovery/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(ROOTFS_URL, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=min(60, remaining_time)) as response:
                final_url = response.geturl()
                if urllib.parse.urlparse(final_url).scheme.lower() != "https":
                    raise RecoveryError(f"Загрузка перенаправлена не на HTTPS: {final_url}")
                status = response.getcode()
                total: Optional[int] = None
                mode = "wb"
                if offset and status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match.group(1)) != offset:
                        raise RecoveryError("Сервер вернул неверный Content-Range; продолжение отменено")
                    end, total = int(match.group(2)), int(match.group(3))
                    if end < offset or end >= total:
                        raise RecoveryError("Сервер вернул противоречивый Content-Range")
                    mode = "ab"
                elif status == 200:
                    length = response.headers.get("Content-Length", "")
                    if not length.isdigit():
                        raise RecoveryError("Сервер не сообщил полный размер rootfs")
                    total = int(length)
                    offset = 0
                    mode = "wb"
                elif not offset and status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
                    if not match:
                        raise RecoveryError("Сервер вернул неверный начальный Content-Range")
                    total = int(match.group(2))
                else:
                    raise RecoveryError(f"Неожиданный HTTP-статус при загрузке: {status}")

                with part.open(mode) as output:
                    received = offset
                    while True:
                        # read1 returns available data instead of waiting for a full MiB
                        # on a slow server. Network inactivity is bounded by socket timeout.
                        chunk = response.read1(1024 * 1024)
                        now = time.monotonic()
                        elapsed = now - started
                        if elapsed >= DOWNLOAD_LIMIT_SECONDS:
                            raise RecoveryError("Лимит загрузки 2 часа исчерпан. Файл .part сохранён; прошивка не начата.")
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        downloaded += len(chunk)
                        transfer_elapsed = now - attempt_started
                        rate = downloaded / max(transfer_elapsed, .001)
                        eta = max(0, total - received) / rate
                        if now - last_report >= 5 or received >= total:
                            progress(f"Скачано {received / 1024**2:.1f} из {total / 1024**2:.1f} MiB · "
                                     f"{rate / 1024**2:.2f} MiB/с · осталось примерно {eta / 60:.1f} мин")
                            last_report = now
                        if transfer_elapsed >= 30 and received < total and elapsed + eta > DOWNLOAD_LIMIT_SECONDS:
                            raise RecoveryError(
                                f"По измеренной скорости загрузка превысит 2 часа "
                                f"(примерно {(elapsed + eta) / 3600:.1f} ч). Загрузка остановлена, "
                                "файл .part сохранён. Используйте полный ZIP проекта или другую сеть.")
                    output.flush()
                    os.fsync(output.fileno())
                if part.stat().st_size != total:
                    raise OSError(f"получено {part.stat().st_size} из {total} байт")
            progress("Полная проверка структуры и архитектуры архива…")
            verify_rootfs_identity(part)
            validate_rootfs(part)
            os.replace(part, dest)
            return dest
        except RecoveryError:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 416 and offset:
                part.unlink(missing_ok=True)
            progress(f"Сеть прервала загрузку (попытка {attempt}/{max_attempts}), продолжение с .part…")
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            last_error = exc
            progress(f"Сеть прервала загрузку (попытка {attempt}/{max_attempts}), продолжение с .part…")
        if attempt < max_attempts:
            time.sleep(min(attempt, 5))
    raise RecoveryError(f"Не удалось полностью скачать rootfs после {max_attempts} попыток: {last_error}")
