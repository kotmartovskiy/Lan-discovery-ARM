#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash.py — запись образов систем Lan-discovery на SD/eMMC-карту одной командой.

Использование (запуск ОТ АДМИНИСТРАТОРА):
    python flash.py --list                 # показать диски этого ноутбука
    python flash.py x96max                 # записать образ X96 Max
    python flash.py orangepi               # записать образ Orange Pi

Образы ищутся в каталоге images/ рядом со скриптом (или --image <файл>).
Поддерживает .tar.xz (стриминговая распаковка) и .img (прямая запись).

Безопасность: скрипт никогда не пишет на системный диск ноутбука
(диск с буквой Windows) и требует явного подтверждения перед записью.
"""

import argparse
import ctypes
import hashlib
import lzma
import os
import string
import sys
import tarfile
import time

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value
BUF = 4 * 1024 * 1024
ALIGN = 512

DEVICES = {
    "x96max": {
        "title": "X96 Max (Armbian, корень на SD 29G)",
        "image_names": ["x96-sd.tar.xz", "x96-sd.img"],
        "size_hint": (24 * 1024 ** 3, 64 * 1024 ** 3),
    },
    "orangepi": {
        "title": "Orange Pi (Armbian, корень на eMMC 14.6G)",
        "image_names": ["opi-emmc.tar.xz", "opi-emmc.img"],
        "size_hint": (8 * 1024 ** 3, 64 * 1024 ** 3),
    },
}


def die(msg):
    print(f"[ОШИБКА] {msg}", file=sys.stderr)
    sys.exit(1)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_drive(n, write=True):
    path = rf"\\.\PhysicalDrive{n}"
    access = (GENERIC_READ | GENERIC_WRITE) if write else GENERIC_READ
    # Запись — с запретом записи для остальных (share=READ): иначе volmgr
    # подхватывает новые разделы из записываемого MBR и блокирует сырые записи.
    share = FILE_SHARE_READ if write else (FILE_SHARE_READ | FILE_SHARE_WRITE)
    h = ctypes.windll.kernel32.CreateFileW(
        path, access, share, None,
        OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE:
        return None
    return h


def list_disks():
    """(номер, размер_байт, модель, системный_ли) через IOCTL_DISK_GET_DRIVE_GEOMETRY_EX."""
    out = []
    IOCTL = 0x000700A0
    for n in range(0, 16):
        h = open_drive(n, write=False)
        if h is None:
            continue
        try:
            buf = ctypes.create_string_buffer(1024 + 1024 * 4)
            ret = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.DeviceIoControl(
                h, IOCTL, None, 0, buf, len(buf), ctypes.byref(ret), None)
            size = 0
            model = "?"
            if ok:
                # DISK_GEOMETRY_EX: DISK_GEOMETRY (24 байта) + int64 size + variable
                import struct
                size = struct.unpack_from("<q", buf, 24)[0]
                # +24+8 = 32: PartitionStyle (4) + буфер
                off = 32 + 4 + 4  # PartitionStyle, PartitionCount, poids
                # Имя устройства в неизвестной части не гарантировано — возьмём модель иначе
                model = get_model(n) or "?"
            else:
                model = get_model(n) or "?"
            sys_drive = is_system_disk(n)
            out.append((n, size, model, sys_drive))
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    return out


def get_model(n):
    """Модель через SetupAPI (WMI не используем — нет зависимостей)."""
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Disk {n} -ErrorAction Stop).FriendlyName"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=0x08000000)
        m = r.stdout.strip().splitlines()
        return m[0].strip() if m else None
    except Exception:
        return None


def is_system_disk(n):
    """True, если на диске есть активный системный том (буква Windows)."""
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Partition -DiskNumber {n} -ErrorAction Stop | "
             f"Where-Object DriveLetter).DriveLetter -contains 'C'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=0x08000000)
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False


def find_image(dev):
    here = os.path.dirname(os.path.abspath(__file__))
    for name in DEVICES[dev]["image_names"]:
        for base in (os.path.join(here, "images"), os.path.join(here, "..", ".."),
                     "D:\\", os.getcwd()):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return os.path.abspath(p)
    return None


def source_stream(path):
    """Возвращает (объект-потоковый читатель read(n)->bytes, полный_размер_если_известен)."""
    if path.lower().endswith((".tar.xz", ".txz", ".tar.lzma")):
        tf = tarfile.open(path, "r:xz")
        member = None
        for m in tf.getmembers():
            if m.isfile():
                member = m
                break
        if member is None:
            die(f"в архиве {path} нет файла-образа")
        fobj = tf.extractfile(member)
        return _TarStream(tf, fobj), member.size
    # обычный .img
    size = os.path.getsize(path)
    return _FileStream(open(path, "rb")), size


class _FileStream:
    def __init__(self, f):
        self.f = f

    def read(self, n):
        return self.f.read(n)

    def close(self):
        self.f.close()


class _TarStream:
    def __init__(self, tf, fobj):
        self.tf, self.fobj = tf, fobj

    def read(self, n):
        return self.fobj.read(n)

    def close(self):
        self.fobj.close()
        self.tf.close()


def write_to_disk(n, stream, total):
    h = open_drive(n, write=True)
    if h is None:
        die(rf"не удалось открыть \\.\PhysicalDrive{n} "
            "(нужен запуск от имени администратора)")
    written = 0
    t0 = time.time()
    pending = b""
    # Двухпроходный MBR: Windows (volmgr) при появлении разделов в записываемом
    # MBR примонтировывает их и блокирует дальнейшую сырую запись (WinError 5).
    # Пишем образ с нулевым MBR, реальный MBR — последним (уже после записи).
    mbr = b""
    while len(mbr) < 512:
        piece = stream.read(512 - len(mbr))
        if not piece:
            die("образ короче 512 байт")
        mbr += piece
    try:
        while True:
            data = stream.read(BUF)
            if not data:
                if pending:
                    chunk = pending + b"\x00" * ((ALIGN - len(pending) % ALIGN) % ALIGN)
                    pending = b""
                else:
                    break
            else:
                chunk = pending + data
                pending = b""
            full = len(chunk) - (len(chunk) % ALIGN)
            if full == 0:
                pending = chunk  # всё ещё < 512 байт — копим
                continue
            out = chunk[:full]
            pending = chunk[full:]
            if written == 0 and len(out) >= 512:
                out = b"\x00" * 512 + out[512:]
            w = ctypes.c_ulong()
            ok = False
            err = 0
            for attempt in range(4):
                w = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.WriteFile(
                    h, out, len(out), ctypes.byref(w), None)
                if ok and w.value == len(out):
                    break
                err = ctypes.windll.kernel32.GetLastError()
                if attempt < 3:
                    time.sleep(1.0)
            if not ok or w.value != len(out):
                die(f"ошибка записи на диске {n} (смещение {written}, "
                    f"WinError {err}, попыток {attempt + 1})")
            written += len(out)
            now = time.time()
            el = max(now - t0, 0.001)
            speed = written / el
            pct = (written / total * 100) if total else 0
            eta = (total - written) / speed if speed and total else 0
            sys.stdout.write(
                f"\r{pct:6.2f}%  {written / 1024 ** 3:6.2f} / {total / 1024 ** 3:.2f} GiB"
                f"  {speed / 1024 ** 2:5.1f} MiB/s  ETA {int(eta // 60)}:{int(eta % 60):02d}   ")
            sys.stdout.flush()
        # MBR — последним блоком: запись завершена, монтирование Windows безвредно
        ctypes.windll.kernel32.SetFilePointer(h, 0, None, 0, None)
        w = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.WriteFile(h, mbr, 512, ctypes.byref(w), None)
        if not ok or w.value != 512:
            die(f"ошибка записи MBR (WinError {ctypes.windll.kernel32.GetLastError()})")
        written += 512  # MBR писался отдельно от общего счётчика
        ctypes.windll.kernel32.FlushFileBuffers(h)
    finally:
        stream.close()
        ctypes.windll.kernel32.CloseHandle(h)
    print()
    if total and written < total:
        die(f"записано меньше ожидаемого: {written} < {total} — образ повреждён?")
    el = time.time() - t0
    print(f"[OK] Записано {written / 1024 ** 3:.2f} GiB за {int(el // 60)} мин "
          f"{int(el % 60)} сек на PhysicalDrive{n}")


def main():
    if os.name != "nt":
        die("скрипт работает только на Windows")
    ap = argparse.ArgumentParser(description="Запись образов Lan-discovery на карту")
    ap.add_argument("device", nargs="?", choices=list(DEVICES) + ["x96", "opi"],
                    help="целевое устройство (x96max | orangepi)")
    ap.add_argument("--list", action="store_true", help="показать диски и выйти")
    ap.add_argument("--image", help="путь к образу вручную")
    ap.add_argument("--disk", type=int, help="номер PhysicalDrive (иначе авто-подбор)")
    ap.add_argument("--yes", action="store_true",
                    help="не спрашивать подтверждение (ОПАСНО)")
    args = ap.parse_args()

    if args.list:
        print("Диски этого ноутбука:")
        for n, size, model, sysd in list_disks():
            flag = "  <-- СИСТЕМНЫЙ, НЕ ТРОГАТЬ" if sysd else ""
            print(f"  PhysicalDrive{n}: {size / 1024 ** 3:6.1f} GiB  {model}{flag}")
        return

    if not args.device:
        die("укажи устройство: flash.py x96max | flash.py orangepi (--list — диски)")
    dev = "x96max" if args.device == "x96" else ("orangepi" if args.device == "opi" else args.device)
    info = DEVICES[dev]
    print(f"Цель: {info['title']}")

    image = args.image or find_image(dev)
    if not image or not os.path.isfile(image):
        die(f"образ не найден (ищу {', '.join(info['image_names'])} в images/, D:\\, cwd)")
    print(f"Образ: {image} ({os.path.getsize(image) / 1024 ** 3:.2f} GiB сжатых)")

    if not is_admin():
        die("запусти скрипт от имени администратора (ПКМ → Запуск от имени администратора)")

    disks = [d for d in list_disks() if not d[3]]
    if not disks:
        die("не найдено ни одного не-системного диска")

    target = None
    if args.disk is not None:
        target = args.disk
        if any(d[0] == target and d[3] for d in list_disks()):
            die("указанный диск является системным")
    else:
        lo, hi = info["size_hint"]
        cand = [d for d in disks if lo <= d[1] <= hi]
        if len(cand) == 1:
            target = cand[0][0]
        else:
            print("Доступные диски:")
            for n, size, model, _ in disks:
                print(f"  [{n}] {size / 1024 ** 3:6.1f} GiB  {model}")
            if len(cand) > 1:
                print("Подходящих под размер образа несколько — выбери номер --disk")
            target = int(input("Номер диска (PhysicalDrive#) для записи: ").strip())

    for n, size, model, _ in disks:
        if n == target:
            print(f"\nЦЕЛЕВОЙ ДИСК: PhysicalDrive{n} — {size / 1024 ** 3:.1f} GiB  {model}")
    if not args.yes:
        ans = input(f"ВСЕ ДАННЫЕ НА ДИСКЕ {target} БУДУТ УНИЧТОЖЕН. Введи номер диска ({target}) для подтверждения: ").strip()
        if ans != str(target):
            die("подтверждение не совпало — отмена")

    stream, total = source_stream(image)
    print(f"Запись{' (сжатый архив — распаковка на лету)' if image.lower().endswith('.xz') else ''}...")
    write_to_disk(target, stream, total)


if __name__ == "__main__":
    main()
