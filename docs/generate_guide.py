#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageEnhance
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image as RLImage, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs"
ASSETS = GUIDE / "assets"
SCRATCH = ROOT / "tmp/pdfs"
SOURCE = GUIDE / "GEACX1-MANUFACTURER-MANUAL.pdf"
OUT_PDF = GUIDE / "GEACX1-GUIDE-RU.pdf"
OUT_HTML = GUIDE / "GEACX1-GUIDE-RU.html"
MANUAL = GUIDE / "GEACX1-MANUFACTURER-MANUAL.pdf"
POPPLER = Path(shutil.which("pdftoppm") or "pdftoppm")

NAVY = colors.HexColor("#17263F")
BLUE = colors.HexColor("#176B87")
CYAN = colors.HexColor("#DFF4F5")
ORANGE = colors.HexColor("#F59E0B")
PALE_ORANGE = colors.HexColor("#FFF4D6")
RED = colors.HexColor("#B42318")
PALE_RED = colors.HexColor("#FDE8E7")
INK = colors.HexColor("#17202A")
MUTED = colors.HexColor("#52606D")
LINE = colors.HexColor("#D6DEE5")
PALE = colors.HexColor("#F5F8FA")


def prepare_assets() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    required = [
        ASSETS / "manufacturer-right-side-p22.jpg",
        ASSETS / "manufacturer-u2-port-p30.jpg",
        ASSETS / "manufacturer-recovery-p32.jpg",
    ]
    if all(path.exists() for path in required):
        return
    prefix = SCRATCH / "manual-hi"
    subprocess.run([
        str(POPPLER), "-f", "22", "-l", "32", "-jpeg", "-r", "240",
        "-jpegopt", "quality=92", str(SOURCE), str(prefix)
    ], check=True)

    def crop(page: int, rel_box: tuple[float, float, float, float], name: str) -> Path:
        src = SCRATCH / f"manual-hi-{page:03d}.jpg"
        with Image.open(src) as im:
            w, h = im.size
            box = tuple(round(v * dim) for v, dim in zip(rel_box, (w, h, w, h)))
            out = im.crop(box)
            out = ImageEnhance.Contrast(out).enhance(1.06)
            dst = ASSETS / name
            out.save(dst, quality=93, optimize=True)
            return dst

    crop(22, (0.12, 0.36, 0.82, 0.61), "manufacturer-right-side-p22.jpg")
    crop(30, (0.12, 0.47, 0.88, 0.74), "manufacturer-u2-port-p30.jpg")
    crop(32, (0.10, 0.13, 0.90, 0.61), "manufacturer-recovery-p32.jpg")


def register_fonts() -> None:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    bolds = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    regular = next(p for p in candidates if p.exists())
    bold = next(p for p in bolds if p.exists())
    pdfmetrics.registerFont(TTFont("Guide", str(regular)))
    pdfmetrics.registerFont(TTFont("Guide-Bold", str(bold)))


def build_pdf() -> None:
    register_fonts()
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName="Guide", fontSize=9.4,
                          leading=12.1, textColor=INK, spaceAfter=4)
    small = ParagraphStyle("small", parent=body, fontSize=7.6, leading=9.7, textColor=MUTED)
    h1 = ParagraphStyle("h1", parent=body, fontName="Guide-Bold", fontSize=22, leading=25,
                        textColor=NAVY, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=body, fontName="Guide-Bold", fontSize=14.2, leading=17,
                        textColor=BLUE, spaceBefore=2, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=body, fontName="Guide-Bold", fontSize=10.5, leading=13,
                        textColor=NAVY, spaceBefore=4, spaceAfter=3)
    code = ParagraphStyle("code", parent=body, fontName="Guide-Bold", fontSize=9.2, leading=12,
                          backColor=colors.HexColor("#EAF0F4"), borderPadding=7, borderRadius=3,
                          spaceBefore=3, spaceAfter=6)
    center = ParagraphStyle("center", parent=body, alignment=TA_CENTER)
    cover = ParagraphStyle("cover", parent=h1, fontSize=28, leading=31, alignment=TA_LEFT)
    white = ParagraphStyle("white", parent=body, textColor=colors.white, fontSize=10, leading=13)

    def P(txt: str, style=body): return Paragraph(txt, style)
    def bullet(txt: str, style=body): return Paragraph(f"•&nbsp;&nbsp;{txt}", style)
    def callout(title: str, text: str, danger=False):
        bg = PALE_RED if danger else PALE_ORANGE
        col = RED if danger else colors.HexColor("#8A4B08")
        t = Table([[P(f"<b>{title}</b>", ParagraphStyle("ct", parent=body, textColor=col)), P(text, body)]],
                  colWidths=[40*mm, 128*mm])
        t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), bg), ("BOX", (0,0), (-1,-1), 0.8, col),
                               ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 7),
                               ("RIGHTPADDING", (0,0), (-1,-1), 7), ("TOPPADDING", (0,0), (-1,-1), 7),
                               ("BOTTOMPADDING", (0,0), (-1,-1), 7)]))
        return t

    def step(n: str, title: str, text: str):
        num = Paragraph(f"<b>{n}</b>", ParagraphStyle("num", parent=center, fontName="Guide-Bold",
                        fontSize=13, textColor=colors.white, leading=16))
        t = Table([[num, P(f"<b>{title}</b><br/>{text}", body)]], colWidths=[11*mm, 157*mm])
        t.setStyle(TableStyle([("BACKGROUND", (0,0), (0,0), BLUE), ("BOX", (0,0), (-1,-1), 0.6, LINE),
                               ("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("LEFTPADDING", (1,0), (1,0), 8),
                               ("RIGHTPADDING", (1,0), (1,0), 8), ("TOPPADDING", (0,0), (-1,-1), 6),
                               ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
        return t

    def img(path: Path, width_mm: float, caption: str):
        with Image.open(path) as im:
            ratio = im.height / im.width
        pic = RLImage(str(path), width=width_mm*mm, height=width_mm*mm*ratio)
        return KeepTogether([pic, P(caption, small)])

    def header_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE); canvas.setLineWidth(0.5)
        canvas.line(18*mm, 15*mm, 192*mm, 15*mm)
        canvas.setFont("Guide", 7.5); canvas.setFillColor(MUTED)
        canvas.drawString(18*mm, 10*mm, "GEACX1 Recovery • JetPack 7.2 • версия комплекта 1.3")
        canvas.drawRightString(192*mm, 10*mm, f"{doc.page} / 9")
        canvas.restoreState()

    doc = BaseDocTemplate(str(OUT_PDF), pagesize=A4, leftMargin=18*mm, rightMargin=18*mm,
                          topMargin=17*mm, bottomMargin=20*mm, title="GEACX1 Recovery JP7.2 - инструкция")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates(PageTemplate(id="guide", frames=[frame], onPage=header_footer))
    story = []

    # 1
    story += [Spacer(1, 8*mm), P("GEACX1 Recovery", cover), P("JetPack 7.2 • AGX Orin 32 ГБ • Ubuntu 24.04", h2),
              Spacer(1, 2*mm), callout("Короткий маршрут", "Открыть публичный проект GitHub → <b>Code → Download ZIP</b> → распаковать ZIP в Ubuntu → открыть корень проекта в терминале → выполнить <b>bash START.sh</b> → выбрать пункт <b>1</b>."),
              Spacer(1, 7*mm), P("Что понадобится", h2),
              bullet("ПК Intel/AMD с Ubuntu 24.04 LTS; рабочий диск <b>ext4</b>."),
              bullet("После распаковки комплекта на ext4 должно оставаться не менее <b>80 ГиБ</b> для подготовки. Желательно 100–120 ГиБ для одного накопителя, около 180 ГиБ для обоих."),
              bullet("Интернет для установки инструментов Ubuntu через APT."),
              bullet("Питание GEACX1 и USB-A ↔ Micro-USB <b>кабель данных</b>, напрямую без хаба."),
              bullet("GEACX1 / carrier 510JX0 r3.0 / AGX Orin 32 GB P3701-0004."),
              Spacer(1, 6*mm), callout("Важно", "32 ГБ в названии — оперативная память модуля, не размер eMMC или SSD.", danger=True),
              Spacer(1, 7*mm), P("Комплект рассчитан на полностью не загружающуюся плату. Старая Ubuntu и SSH не нужны.", h3),
              P("Плата не стирается при запуске мастера. Запись начинается только после отдельной проверки Recovery и точной фразы подтверждения.", body), PageBreak()]

    # 2
    story += [P("1. Скачать проект с GitHub", h1),
              P("Откройте публичный проект GitHub — вход в аккаунт не требуется:", body),
              P("https://github.com/tehblok/geacx1-recovery-validation", code),
              P("На странице проекта нажмите зелёную кнопку <b>Code</b>, затем <b>Download ZIP</b>. Это правильный полный комплект: прошивка хранится внутри каталога <b>payload/</b>.", h2),
              callout("Размер и целостность", "Точный размер и SHA-256 полного архива v1.3 записаны в <b>parts.json</b>. Дождитесь полного завершения скачивания; START.sh проверит каждую часть и собранный архив."),
              Spacer(1, 6*mm), P("Почему внутри много частей", h2),
              P("Из-за ограничения GitHub на размер отдельных файлов каталог payload/ содержит много частей. Не объединяйте, не переименовывайте и не распаковывайте их вручную.", body),
              Spacer(1, 5*mm), P("Перед продолжением", h2),
              bullet("ZIP проекта полностью скачался и распаковывается на Ubuntu."),
              bullet("В корне распакованной папки видны START.sh, assemble.py и parts.json; рядом есть payload/."),
              bullet("На ПК достаточно места: стартер проверит части, соберёт и распакует большой архив."), PageBreak()]

    # 3
    story += [P("2. Запуск комплекта", h1),
              step("1", "Распакуйте скачанный ZIP", "В файловом менеджере Ubuntu: правая кнопка → «Извлечь сюда». Работайте с распакованной папкой проекта, не запускайте файлы прямо из ZIP."),
              Spacer(1, 3*mm), step("2", "Откройте корень проекта в терминале", "Откройте папку, где лежат START.sh, assemble.py, parts.json и payload/, затем нажмите правой кнопкой по свободному месту → «Открыть в терминале»."),
              Spacer(1, 3*mm), step("3", "Запустите подготовку комплекта", "Введите команду ниже. Пароль sudo — пароль вашего пользователя Ubuntu; при вводе символы не показываются."),
              P("bash START.sh", code),
              Spacer(1, 4*mm), P("Что делает START.sh", h2),
              bullet("Читает parts.json и находит все части полного архива в payload/."),
              bullet("Проверяет контрольные суммы частей, объединяет и проверяет собранный архив, затем распаковывает полный комплект."),
              bullet("Запускает оригинальный мастер восстановления."),
              bullet("По умолчанию создаёт новый каталог <b>~/geacx1-kit-v1.3</b>; существующие файлы не перезаписывает."),
              Spacer(1, 5*mm), callout("Если проверка не прошла", "Удалите повреждённую распакованную папку и заново скачайте весь ZIP проекта через Code → Download ZIP. Не объединяйте части вручную.", danger=True),
              Spacer(1, 4*mm), P("Повторный запуск", h2),
              P("Для v1.3 заново скачайте проект и запустите корневой <b>START.sh</b>. Старую рабочую папку <b>~/geacx1-kit-v1.2</b> не используйте для новой прошивки. После успешной подготовки v1.3 повторно запускайте мастер из <b>~/geacx1-kit-v1.3/geacx1-recovery</b>.", body),
              Spacer(1, 4*mm), P("До запуска подключите ноутбук к питанию, отключите автоматический сон и не закрывайте крышку во время подготовки и записи.", h3), PageBreak()]

    # 4
    data = [
        [P("Выбор", h3), P("Что будет записано", h3)],
        [P("eMMC", body), P("QSPI + встроенная eMMC. SSD не изменяется.", body)],
        [P("NVMe", body), P("QSPI + SSD nvme0n1 на GEACX1. eMMC не изменяется.", body)],
        [P("Оба", body), P("Сначала eMMC, затем после нового Recovery отдельный проход NVMe.", body)],
    ]
    table = Table(data, colWidths=[40*mm, 128*mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), NAVY), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
                               ("GRID", (0,0), (-1,-1), 0.5, LINE), ("VALIGN", (0,0), (-1,-1), "TOP"),
                               ("BACKGROUND", (0,1), (-1,-1), PALE), ("LEFTPADDING", (0,0), (-1,-1), 7),
                               ("RIGHTPADDING", (0,0), (-1,-1), 7), ("TOPPADDING", (0,0), (-1,-1), 6),
                               ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
    story += [P("3. Выбрать восстановление", h1),
              P("В главном меню выберите <b>1 — восстановление с нуля</b>. Мастер проверит Ubuntu, процессор, ext4, свободное место и файлы, при необходимости предложит установить зависимости, затем подготовит чистый образ.", body),
              Spacer(1, 4*mm), table, Spacer(1, 5*mm),
              callout("Если SSD нет", "Выбирайте eMMC. Для NVMe нужен SSD на самой плате не менее 64 ГиБ (68,7 GB); практически — 128 ГБ или больше."),
              Spacer(1, 5*mm), P("Подготовка ещё не прошивает плату", h2),
              bullet("Образ Ubuntu и файлы производителя уже находятся в полном архиве; повторно скачивать их не нужно."),
              bullet("Интернет нужен для APT на компьютере Ubuntu."),
              bullet("После успешной подготовки можно перейти к подключению платы или позже выбрать пункт <b>2</b>; мастер повторно проверит сборку."),
              bullet("Для «Оба» потребуется примерно 180 ГиБ и два отдельных прохода записи."),
              Spacer(1, 5*mm), callout("NVMe", "Рецепт основан на файлах производителя, но физически на этой конкретной плате не проверен. При двух системах загрузочный носитель может потребоваться выбрать в UEFI.", danger=True), PageBreak()]

    # 5
    story += [P("4. Найти кнопки и порт U2", h1),
              P("На правой стороне корпуса подписи идут слева направо: <b>RST</b>, <b>RECV</b>, HDMI, U3, <b>U2</b>, NANO-SIM. Для прошивки используйте Micro-USB порт <b>U2 (USB2.0-OTG)</b>.", body),
              img(ASSETS / "manufacturer-right-side-p22.jpg", 150, "Рис. 1. Правая сторона GEACX1: реальные подписи RST, RECV и U2. Источник: руководство производителя, печатная стр. 22; таблица продолжается на стр. 23."),
              Spacer(1, 4*mm), img(ASSETS / "manufacturer-u2-port-p30.jpg", 145, "Рис. 2. Производитель выделяет U2 как порт для прошивки. Источник: руководство производителя, печатная стр. 30."),
              Spacer(1, 2*mm), callout("Кабель", "Нужен кабель с передачей данных. Зарядный кабель и USB-хаб часто мешают обнаружению Recovery.", danger=True), PageBreak()]

    # 6
    story += [P("5. Войти в Force Recovery", h1),
              step("1", "Подключите питание и U2", "Подайте штатное питание GEACX1. Соедините U2 и ПК кабелем USB-A ↔ Micro-USB. К ПК должен быть подключён только один Jetson."),
              Spacer(1, 2*mm), step("2", "Зажмите RECV", "Удерживайте кнопку RECV / Recovery."),
              Spacer(1, 2*mm), step("3", "Коротко нажмите RST", "Не отпуская RECV, нажмите RST / Reset на 1–3 секунды. Отпустите RST, затем RECV."),
              Spacer(1, 3*mm), callout("Нормально", "В Recovery экран остаётся чёрным; HDMI не нужен. Ожидаемый USB ID — <b>0955:7023</b>. Мастер дополнительно проверит настоящий тип модуля."),
              Spacer(1, 4*mm), img(ASSETS / "manufacturer-recovery-p32.jpg", 142, "Рис. 3. Аппаратная последовательность и пример проверки lsusb. Источник: руководство производителя, печатная стр. 32; подготовка перечислена на стр. 31."),
              Spacer(1, 2*mm), P("Для каждого прохода записи, включая второй проход при выборе «Оба», Recovery нужно выполнить заново.", h3), PageBreak()]

    # 7
    story += [P("6. Подтвердить запись и проверить запуск", h1),
              P("Перед записью мастер покажет накопитель, рабочий каталог и журнал. Проверьте всё и введите точную фразу:", body),
              P("eMMC:  ERASE GEACX1 32GB", code),
              P("NVMe:  ERASE NVME GEACX1 32GB", code),
              callout("Стирание данных", "Запись удаляет данные выбранного накопителя. Enter без текста отменяет операцию. Для двух накопителей подтверждение вводится отдельно перед каждым проходом.", danger=True),
              Spacer(1, 4*mm), P("Во время записи", h2),
              bullet("Не отключайте питание или USB, не закрывайте терминал и не усыпляйте ПК."),
              bullet("При ошибке мастер остановится и покажет путь к журналу. Автоматического опасного повтора нет."),
              Spacer(1, 4*mm), P("После успешного завершения", h2),
              bullet("Отпустите кнопки. Если плата осталась в Recovery, сделайте обычный Reset без RECV."),
              bullet("Подключите HDMI и клавиатуру, дождитесь Ubuntu и создайте пользователя; общего пароля нет."),
              bullet("Перенесите POSTCHECK.sh на GEACX1 и выполните на плате: <b>bash POSTCHECK.sh</b>."),
              bullet("Проверьте HDMI, Ethernet, USB, нужную периферию и загрузочный накопитель. На холодной плате вентилятор может стоять."),
              Spacer(1, 5*mm), callout("Состав базового образа", "CUDA, cuDNN, TensorRT и полный metapackage nvidia-jetpack не входят в базовый набор восстановления. Их устанавливают после успешной загрузки с проверкой совместимой версии JetPack 7.2 / R39.2.0.", danger=True), PageBreak()]

    # 8
    story += [P("7. Резервная копия и помощь", h1),
              P("Главное меню → <b>7 — резервная копия / восстановление</b>. Для создания и возврата копии также нужен Recovery.", body),
              bullet("Копия может включать QSPI + eMMC или QSPI + eMMC + NVMe."),
              bullet("Подтверждения: <b>BACKUP GEACX1</b> и <b>RESTORE GEACX1 BACKUP</b>. Восстановление стирает текущие данные."),
              bullet("Сохраните всю папку копии вместе с GEACX1_BACKUP.json и nvpartitionmap.txt на отдельном диске."),
              bullet("EEPROM, eFuse, CPLD и eMMC boot0/boot1/RPMB не входят в копию. Повреждённая система сохраняется повреждённой."),
              Spacer(1, 4*mm), P("Частые ошибки", h2),
              bullet("Плата не найдена: повторите RECV + RST, смените кабель данных или прямой USB-порт, уберите хаб."),
              bullet("Нет места / диск не ext4: после распаковки комплекта выберите рабочий каталог на ext4, где остаётся ≥80 ГиБ."),
              bullet("Контрольная сумма не совпала: заново скачайте весь ZIP проекта через Code → Download ZIP."),
              bullet("Запись прошла, загрузки нет: Reset без RECV, выбор носителя в UEFI, HDMI и полный журнал."),
              P("По умолчанию журналы мастера находятся в <b>~/geacx1-kit-v1.3/geacx1-recovery/logs/</b>, а отчёт проверки и распаковки — в <b>~/geacx1-kit-v1.3/unpack-verification.log</b>. Для диагностики сохраните полный журнал, выбранный накопитель и результат проверки ПК.", body),
              Spacer(1, 4*mm), callout("Новая версия", "Для v1.3 заново скачайте проект и запустите корневой <b>START.sh</b>. Не используйте старую рабочую папку ~/geacx1-kit-v1.2 для новой прошивки."), PageBreak()]

    # 9
    story += [P("8. Проверка материалов производителя", h1),
              P("Комплект v1.3 использует <b>NVIDIA Sample Root Filesystem R39.2.0</b> вместе с проверенными BSP, board, kernel и сервисами производителя для GEACX1 / 510JX0 r3.0. Это не тот же файл, что отдельный заводской rootfs v1.01.", body),
              Spacer(1, 3*mm), callout("Заводской rootfs v1.01", "На сайте производителя найден отдельный архив размером <b>16,67 ГБ</b>. Он не скачан полностью, его MD5 и содержимое не проверены, поэтому он не включён и не подменяет текущий rootfs."),
              Spacer(1, 3*mm), P("Что нельзя подменять", h2),
              bullet("Сетевой универсальный установщик: конфигурация GEACX1-JP7.2.json на 2 октября 2026 года отвечает HTTP 404."),
              bullet("Пакеты JP4, JP5, JP6, NX, GEACX1SC и других ревизий не заменяют компоненты GEACX1 JP7.2."),
              bullet("Найденный MCU-архив содержит тест GPIO/SPI, а не прошивку загрузчика или MCU для JP7.2."),
              bullet("CPLD не прошивается автоматически. Диагностические команды смешаны с командами изменения режима; совместимость старых v1.1/v1.3 и локального rb v1.4 не доказана."),
              bullet("Справка производителя доступна в меню <b>5 → 4</b>. Вспомогательная сетевая загрузка NVIDIA rootfs показывает скорость/ETA и останавливается при общем прогнозе более двух часов."),
              Spacer(1, 3*mm), P("Дополнительные исходники", h2),
              P("Файлы из <b>~/geacx1-kit-v1.3/manufacturer-extra/</b> сохранены для справки и автоматически не устанавливаются. Полный отчёт без паролей: <b>docs/SITE_REVIEW_RU.txt</b>.", body),
              Spacer(1, 3*mm), P("Источники", h2),
              P("INSTRUCTION_RU.md, REFERENCE_RU.md, GEACX1-MANUFACTURER-MANUAL.pdf и проверенные вложенные страницы производителя. Изображения в этом руководстве: печатные стр. 22, 30 и 32 оригинального PDF.", small),
              Spacer(1, 4*mm), callout("Граница процедуры", "Восстановление записывает QSPI и выбранный накопитель. Оно не сбрасывает CPLD, EEPROM, eFuse или Secure Boot ключи."),
              Spacer(1, 3*mm), P("Проект: github.com/tehblok/geacx1-recovery-validation", small)]
    doc.build(story)


def data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_html() -> None:
    img22 = data_uri(ASSETS / "manufacturer-right-side-p22.jpg")
    img30 = data_uri(ASSETS / "manufacturer-u2-port-p30.jpg")
    img32 = data_uri(ASSETS / "manufacturer-recovery-p32.jpg")
    project = "https://github.com/tehblok/geacx1-recovery-validation"
    css = """
    :root{--navy:#17263f;--blue:#176b87;--cyan:#dff4f5;--orange:#f59e0b;--red:#b42318;--ink:#17202a;--muted:#52606d;--line:#d6dee5;--pale:#f5f8fa}
    *{box-sizing:border-box} body{margin:0;background:#edf2f5;color:var(--ink);font:16px/1.5 Arial,DejaVu Sans,sans-serif} main{max-width:980px;margin:0 auto;background:#fff;box-shadow:0 4px 30px #1b2a3840}.page{min-height:100vh;padding:54px 64px 62px;border-bottom:1px solid var(--line);position:relative}h1{font-size:36px;line-height:1.08;color:var(--navy);margin:0 0 20px}h2{font-size:23px;color:var(--blue);margin:24px 0 10px}h3{font-size:18px;color:var(--navy)}p{margin:8px 0}code,.code{display:block;background:#eaf0f4;padding:12px 15px;border-radius:7px;font:700 16px/1.35 ui-monospace,SFMono-Regular,monospace;overflow-wrap:anywhere}.call{border:1px solid #d6951b;background:#fff4d6;padding:14px 17px;border-radius:8px;margin:16px 0}.danger{border-color:var(--red);background:#fde8e7}.steps{counter-reset:s}.step{display:grid;grid-template-columns:42px 1fr;gap:12px;border:1px solid var(--line);margin:10px 0;border-radius:8px;overflow:hidden}.step:before{counter-increment:s;content:counter(s);display:grid;place-items:center;background:var(--blue);color:white;font-weight:700;font-size:20px}.step>div{padding:10px 12px}ul{padding-left:23px}.files{display:grid;gap:8px}figure{margin:18px 0;text-align:center}figure img{max-width:100%;max-height:420px;border:1px solid var(--line);border-radius:6px}figcaption{font-size:13px;color:var(--muted);text-align:left}table{border-collapse:collapse;width:100%}th,td{border:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}th{background:var(--navy);color:#fff}.foot{position:absolute;bottom:22px;left:64px;right:64px;border-top:1px solid var(--line);padding-top:7px;color:var(--muted);font-size:12px}.route{font-size:19px}.hero{padding-top:90px}.links a{color:var(--blue)}@media(max-width:700px){.page{padding:35px 22px 56px}.foot{left:22px;right:22px}h1{font-size:30px}}@media print{body{background:#fff}main{box-shadow:none;max-width:none}.page{width:210mm;height:297mm;min-height:0;page-break-after:always;padding:16mm 18mm 20mm;overflow:hidden}.page:last-child{page-break-after:auto}.foot{left:18mm;right:18mm;bottom:9mm}a{color:inherit;text-decoration:none}}
    """
    pages = [
      ("GEACX1 Recovery", """<p class='route'><b>JetPack 7.2 • AGX Orin 32 ГБ • Ubuntu 24.04</b></p><div class='call'><b>Короткий маршрут</b><p>Публичный проект GitHub → Code → Download ZIP → распаковать проект → открыть корень проекта в терминале → <code>bash START.sh</code> → пункт 1.</p></div><h2>Что понадобится</h2><ul><li>Ubuntu 24.04 LTS на Intel/AMD, диск ext4.</li><li>После распаковки комплекта на ext4 должно оставаться ≥80 ГиБ для подготовки; желательно 100–120 ГиБ для одного накопителя, около 180 ГиБ для обоих.</li><li>Интернет для инструментов Ubuntu через APT, питание платы, USB-A ↔ Micro-USB кабель данных без хаба.</li><li>GEACX1 / 510JX0 r3.0 / AGX Orin 32 GB P3701-0004.</li></ul><div class='call danger'><b>32 ГБ — оперативная память, не размер накопителя.</b></div><p>Старая Ubuntu и SSH не нужны. Запись начинается только после Recovery и точной фразы подтверждения.</p>"""),
      ("1. Скачать проект с GitHub", f"""<p>Откройте публичный проект GitHub без входа в аккаунт: <a href='{project}'>{project}</a>.</p><h2>Code → Download ZIP</h2><p>Это правильный полный комплект v1.3. В корне проекта находятся START.sh, assemble.py и parts.json, а в payload/ — части полного архива.</p><div class='call'>Точный размер и SHA-256 полного архива записаны в <b>parts.json</b>. Дождитесь полного завершения скачивания.</div><p>Архив разделён на части из-за ограничения GitHub. Не объединяйте, не переименовывайте и не распаковывайте payload вручную.</p>"""),
      ("2. Распаковать и запустить", """<div class='steps'><div class='step'><div><b>Распакуйте ZIP проекта</b><br>Правая кнопка → «Извлечь сюда».</div></div><div class='step'><div><b>Откройте корень проекта</b><br>Папка с START.sh, assemble.py, parts.json и payload/ → «Открыть в терминале».</div></div><div class='step'><div><b>Запустите подготовку комплекта</b><code>bash START.sh</code></div></div></div><h2>Что делает START.sh</h2><ul><li>читает parts.json и находит части в payload/;</li><li>проверяет контрольные суммы, собирает и распаковывает архив;</li><li>запускает оригинальный мастер;</li><li>создаёт новый ~/geacx1-kit-v1.3 и ничего не перезаписывает.</li></ul><div class='call danger'>При ошибке проверки заново скачайте весь проект через Code → Download ZIP.</div><h2>Повторный запуск</h2><p>Для v1.3 заново скачайте проект и запустите корневой START.sh. Старую рабочую папку ~/geacx1-kit-v1.2 не используйте для новой прошивки.</p>"""),
      ("3. Выбрать восстановление", """<p>Главное меню → <b>1 — восстановление с нуля</b>.</p><table><tr><th>Выбор</th><th>Что записывается</th></tr><tr><td>eMMC</td><td>QSPI + eMMC</td></tr><tr><td>NVMe</td><td>QSPI + SSD nvme0n1 на GEACX1</td></tr><tr><td>Оба</td><td>Два прохода; между ними новый Recovery</td></tr></table><p>Rootfs/BSP уже в частях архива. Интернет нужен для APT. Подготовка образа на ПК ещё не прошивает плату.</p><div class='call'>Если SSD нет — выбирайте eMMC. Для NVMe нужно ≥64 ГиБ (68,7 GB), практически 128 ГБ+.</div><div class='call danger'>NVMe-рецепт основан на vendor-файлах, но физически на вашей плате здесь не проверен. При двух ОС носитель может потребоваться выбрать в UEFI.</div>"""),
      ("4. Найти кнопки и U2", f"""<p>Справа слева направо: <b>RST, RECV, HDMI, U3, U2, NANO-SIM</b>. Для прошивки используйте Micro-USB <b>U2</b>.</p><figure><img src='{img22}'><figcaption>Рис. 1. RST, RECV и U2. Производитель, печатная стр. 22; таблица продолжается на стр. 23.</figcaption></figure><figure><img src='{img30}'><figcaption>Рис. 2. U2 выделен как порт прошивки. Производитель, печатная стр. 30.</figcaption></figure>"""),
      ("5. Войти в Force Recovery", f"""<div class='steps'><div class='step'><div>Подключите питание и U2 к ПК. Только один Jetson.</div></div><div class='step'><div>Зажмите RECV.</div></div><div class='step'><div>Удерживая RECV, нажмите RST на 1–3 с. Отпустите RST, затем RECV.</div></div></div><div class='call'>Чёрный экран нормален. Ожидаемый USB ID: <b>0955:7023</b>.</div><figure><img src='{img32}'><figcaption>Рис. 3. Recovery и lsusb. Производитель, стр. 32; подготовка — стр. 31.</figcaption></figure><p>Recovery повторяется перед каждым проходом.</p>"""),
      ("6. Записать и проверить", """<p>Точная фраза для eMMC:</p><code>ERASE GEACX1 32GB</code><p>Для NVMe:</p><code>ERASE NVME GEACX1 32GB</code><div class='call danger'>Выбранный накопитель будет стёрт. Enter без текста отменяет запись.</div><ul><li>Не отключайте питание/USB, не усыпляйте ПК.</li><li>После успеха: Reset без RECV, HDMI, первый запуск Ubuntu.</li><li>На плате выполните <b>bash POSTCHECK.sh</b>.</li><li>Проверьте HDMI, Ethernet, USB и нужную периферию.</li></ul><div class='call danger'>CUDA, cuDNN, TensorRT и полный nvidia-jetpack не входят в базовый образ. Установите их после загрузки с проверкой совместимости JetPack 7.2 / R39.2.0.</div>"""),
      ("7. Копия и ошибки", """<p><b>Меню 7</b>: backup/restore через Recovery. Фразы: BACKUP GEACX1 и RESTORE GEACX1 BACKUP. Сохраните всю папку копии.</p><p>Не входят: EEPROM, eFuse, CPLD, eMMC boot0/boot1/RPMB.</p><h2>Если ошибка</h2><ul><li>USB: повторите Recovery, смените кабель/порт, уберите хаб.</li><li>Место/ext4: после распаковки комплекта должно оставаться ≥80 ГиБ.</li><li>Контрольная сумма: заново скачайте весь ZIP проекта через Code → Download ZIP.</li><li>Нет загрузки: Reset без RECV, UEFI, HDMI, полный лог.</li></ul><p>Журналы мастера: <b>~/geacx1-kit-v1.3/geacx1-recovery/logs/</b>. Проверка распаковки: <b>~/geacx1-kit-v1.3/unpack-verification.log</b>.</p><div class='call'>Для v1.3 заново скачайте проект и запустите корневой START.sh. Старую рабочую папку v1.2 не используйте.</div>"""),
      ("8. Материалы производителя", """<p>Комплект v1.3 использует <b>NVIDIA Sample Root Filesystem R39.2.0</b> вместе с BSP, board, kernel и сервисами производителя для GEACX1 / 510JX0 r3.0.</p><div class='call'><b>Заводской rootfs v1.01 (16,67 ГБ)</b> найден отдельно, но не скачан полностью: его MD5 и содержимое не проверены, поэтому он не включён.</div><h2>Не подменять компоненты</h2><ul><li>GEACX1-JP7.2.json универсального установщика на 2 октября 2026 года отвечает HTTP 404.</li><li>JP4/JP5/JP6, NX, GEACX1SC и другие ревизии не подходят вместо JP7.2.</li><li>MCU-архив оказался тестом GPIO/SPI, а не прошивкой.</li><li>CPLD автоматически не прошивается; совместимость старых v1.1/v1.3 и локального rb v1.4 не доказана.</li><li>Справка производителя: меню <b>5 → 4</b>. Загрузка NVIDIA rootfs показывает скорость/ETA и останавливается при прогнозе более двух часов.</li></ul><p>Дополнительные файлы в <b>~/geacx1-kit-v1.3/manufacturer-extra/</b> сохранены для справки и автоматически не устанавливаются. Подробности: <b>docs/SITE_REVIEW_RU.txt</b>.</p><div class='call'>Восстановление записывает QSPI и выбранный накопитель; CPLD, EEPROM, eFuse и Secure Boot ключи не сбрасываются.</div>"""),
    ]
    chunks = []
    for i, (title, body) in enumerate(pages, 1):
        hero = " hero" if i == 1 else ""
        chunks.append(f"<section class='page{hero}'><h1>{html.escape(title)}</h1>{body}<div class='foot'>GEACX1 Recovery • v1.3 <span style='float:right'>{i} / 9</span></div></section>")
    OUT_HTML.write_text("<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>GEACX1 Recovery — инструкция</title><style>" + css + "</style></head><body><main>" + "".join(chunks) + "</main></body></html>", encoding="utf-8")


def validate() -> None:
    reader = PdfReader(OUT_PDF)
    if len(reader.pages) != 9:
        raise RuntimeError(f"Expected 9 pages, got {len(reader.pages)}")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    required = ["Code", "Download ZIP", "assemble.py", "parts.json", "payload/", "bash START.sh",
                "ERASE GEACX1 32GB", "ERASE NVME GEACX1 32GB", "0955:7023", "CUDA", "BACKUP GEACX1",
                "geacx1-recovery/logs/", "unpack-verification.log", "manufacturer-extra/",
                "16,67 ГБ", "GEACX1-JP7.2.json", "CPLD", "v1.3"]
    missing = [x for x in required if x not in text]
    if missing:
        raise RuntimeError(f"Missing text in PDF: {missing}")
    if OUT_HTML.stat().st_size < 100_000:
        raise RuntimeError("HTML unexpectedly small; embedded images may be missing")


if __name__ == "__main__":
    prepare_assets()
    build_pdf()
    build_html()
    validate()
    print(f"PDF: {OUT_PDF} ({OUT_PDF.stat().st_size} bytes)")
    print(f"HTML: {OUT_HTML} ({OUT_HTML.stat().st_size} bytes)")
    print(f"Manual: {MANUAL} ({MANUAL.stat().st_size} bytes)")
