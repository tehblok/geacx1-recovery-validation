#!/usr/bin/env bash
set -uo pipefail
if [[ $(uname -s) != Linux || $(uname -m) != aarch64 ]]; then
    printf '%s\n' 'Этот файл запускается НА GEACX1 после первого старта Ubuntu, не на компьютере прошивки.'
    exit 1
fi
printf '\n%s\n' 'GEACX1 · Проверка после восстановления JP7.2'
failures=0
if [[ -r /etc/nv_tegra_release ]]; then
    head -n 1 /etc/nv_tegra_release
    if ! head -n 1 /etc/nv_tegra_release | grep -Eq 'R39.*REVISION: 2\.0([, ]|$)'; then
        printf '%s\n' 'ОШИБКА: ожидался L4T R39.2.0'; failures=$((failures+1))
    fi
else
    printf '%s\n' 'ОШИБКА: нет /etc/nv_tegra_release'; failures=$((failures+1))
fi
printf '\nЯдро: '; uname -r
if [[ $(uname -r) != 6.8.12-1021-tegra ]]; then
    printf '%s\n' 'ОШИБКА: ожидалось vendor-ядро 6.8.12-1021-tegra'; failures=$((failures+1))
fi
printf '\nСборка ядра:\n'; cat /proc/version
printf '\nМодель DTB: '
if [[ -r /proc/device-tree/model ]]; then tr '\0' '\n' </proc/device-tree/model; fi
printf '\nСовместимость DTB:\n'
if [[ -r /proc/device-tree/compatible ]]; then tr '\0' '\n' </proc/device-tree/compatible; fi
if ! tr '\0' '\n' </proc/device-tree/compatible 2>/dev/null | grep -Fxq 'nvidia,p3701-0004'; then
    printf '%s\n' 'ОШИБКА: DTB не соответствует модулю P3701-0004'; failures=$((failures+1))
fi
if ! grep -arFq -- 'geac91_510jx0_r3_0_JP7.2_ga_v1.0.0' /proc/device-tree 2>/dev/null; then
    printf '%s\n' 'ОШИБКА: не найден vendor-маркер DTB 510JX0 r3.0 JP7.2'; failures=$((failures+1))
fi
printf '\nПамять (доступный ОС объём меньше номинальных 32 ГБ):\n'; free -h
printf '\nСистема и накопители:\n'; findmnt /; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
printf '\nПроверка /etc/fstab после переноса или восстановления (без изменений):\n'
if ! findmnt --verify --verbose; then
    printf '%s\n' 'ВНИМАНИЕ: проверьте UUID из /etc/fstab по lsblk -f; старые UUID могут задерживать загрузку.'
    printf '%s\n' 'Не удаляйте fstab целиком: исправляйте только установленную причину. Это предупреждение не доказывает неисправность прошивки.'
fi
printf '\nПакеты NVIDIA/vendor:\n'
dpkg-query -W 'nvidia-l4t-core' 'nvidia-jetpack' 'rb-jetson-*' 2>/dev/null || true
printf '\nНеуспешные службы (часть может быть не связана с прошивкой):\n'
systemctl --failed --no-pager || true
printf '\nСлужба вентилятора GEACX1:\n'
if ! systemctl is-active --quiet rb-jetson-service-fan.service || ! systemctl is-enabled --quiet rb-jetson-service-fan.service; then
    printf '%s\n' 'ОШИБКА: служба вентилятора не запущена/не включена'; failures=$((failures+1))
fi
if ! compgen -G '/sys/class/rb_gpio/fan_*-power/value' >/dev/null; then
    printf '%s\n' 'ОШИБКА: нет vendor GPIO управления вентиляторами'; failures=$((failures+1))
fi
printf '\nСеть:\n'; ip -brief address
printf '\nМодули:\n'
if [[ -d /lib/modules/$(uname -r) ]]; then
    printf '%s\n' 'Каталог модулей текущего ядра найден.'
else
    printf '%s\n' 'ОШИБКА: нет модулей текущего ядра'; failures=$((failures+1))
fi
printf '\n%s\n' 'Отдельно проверьте Ethernet, HDMI, USB, вентилятор и требуемые камеры/CAN/GPIO.'
printf '%s\n' 'Отсутствие nvidia-jetpack означает, что вычислительные библиотеки ещё не установлены.'
printf '%s\n' 'CPLD проверяется отдельно по инструкции своей ревизии; этот скрипт не отправляет команды в UART и не прошивает CPLD.'
if ((failures)); then printf 'Найдено критичных несоответствий: %s\n' "$failures"; exit 1; fi
printf '%s\n' 'Базовые проверки пройдены. Это не заменяет проверку всей периферии.'
