# -*- coding: utf-8 -*-
"""Discovery engine (PHASE 6): scanner -> normalizer -> reconcile -> events.

- `run_scan` — scanner: один проход nmap -sn -PR по интерфейсам
  `network.scan_ifaces`, возвращает **raw-вывод nmap** (без двойного
  парсинга/синтеза, как было в devices_routes) либо None (nmap отсутствует /
  пусто).
- `parse_scan` — normalizer: вывод nmap -> `{ip: {hostname, mac, vendor}}`.
- `reconcile(con, current, now)` — DB-слой: upsert, NEW/ONLINE/OFFLINE/misses,
  трассировка смены MAC (MAC_CHANGED).

UI-роуты остались в `modules/devices_routes.py` (реэкспортирует символы для
обратной совместимости: app.py/system_routes импортируют их оттуда).
"""
import re
import socket
import subprocess
import threading
import time
import logging
from datetime import datetime

from core.events import add_event

log = logging.getLogger("lan-discovery")

_DEFAULT_IFACES = ["end0", "eth0", "wlan1", "wlan0"]
_DEFAULT_SELF_IPS = ["192.168.3.234", "192.168.3.235"]


def _cfg_net(key, default=None):
    from app import _cfg
    return _cfg("network", key, default)


def _subnet():
    return _cfg_net("subnet", "192.168.3.0/24")


def _scan_ifaces():
    v = _cfg_net("scan_ifaces")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    return list(_DEFAULT_IFACES)


def _scan_interval():
    return int(_cfg_net("scan_interval", 30) or 30)


def _scan_enabled():
    """Фоновый скан включён? (PHASE 16 №59 — одна ведущая копия).

    `network.scan_enabled: false` → scan_loop спит и не опрашивает
    сеть (нужно, когда одну подсеть ведёт другая панель, например OP
    на 1.0 отдаёт лидерство X96). Ручной POST /api/scan и статус
    при этом остаются доступными.
    """
    v = _cfg_net("scan_enabled", True)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _max_misses():
    return int(_cfg_net("max_misses", 6) or 6)


def _self_ips():
    v = _cfg_net("self_ips")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    return list(_DEFAULT_SELF_IPS)


_hostname_cache = {}


def get_hostname(ip):
    if ip in _hostname_cache:
        return _hostname_cache[ip]

    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(1.5)
        result = socket.gethostbyaddr(ip)[0]
        _hostname_cache[ip] = result
        return result
    except Exception:
        _hostname_cache[ip] = None
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


def parse_scan(output):
    """Normalizer: вывод nmap -> {ip: {hostname, mac, vendor}}."""
    devices = {}
    current_ip = None

    for line in output.splitlines():
        line = line.strip()

        match = re.search(r"Nmap scan report for (.+)", line)
        if match:
            value = match.group(1)
            ip_match = re.search(r"\(([\d.]+)\)", value)
            if ip_match:
                hostname = value.rsplit("(", 1)[0].strip()
                ip = ip_match.group(1)
            else:
                hostname = None
                ip = value.strip()

            current_ip = ip
            devices[ip] = {
                "hostname": hostname,
                "mac": None,
                "vendor": None,
            }
            continue

        if current_ip:
            match = re.search(
                r"MAC Address:\s+([0-9A-Fa-f:]{17})\s+\((.*?)\)",
                line,
            )
            if match:
                devices[current_ip]["mac"] = match.group(1).upper()
                devices[current_ip]["vendor"] = match.group(2)

    return devices


def run_scan(subnet=None, ifaces=None):
    """Scanner: raw-вывод nmap для первой успешной пары (interface, subnet).

    None — сканирование недоступно (nmap отсутствует или все прогоны упали
    либо не принесли ни одного хоста, включая self_ips).
    """
    subnet = subnet or _subnet()
    if ifaces:
        ifaces = [str(i) for i in ifaces]
    else:
        ifaces = _scan_ifaces()

    output = None
    any_ok = False

    for iface in ifaces:
        try:
            r = subprocess.run(
                ["nmap", "-sn", "-PR", "-e", iface, "--host-timeout", "3s",
                 subnet],
                capture_output=True, text=True, timeout=45,
            )
        except FileNotFoundError:
            log.error("SCAN: nmap не установлен — сканирование недоступно")
            return None
        except Exception:
            continue
        if r.returncode == 0:
            any_ok = True
            if r.stdout.strip():
                output = r.stdout
                break

    if not any_ok:
        log.warning("SCAN: nmap failed on all interfaces")
        return None

    output = output or ""

    # self_ips — машины панели добавляются, даже если nmap их не показал
    extra = ""
    known = parse_scan(output)
    for self_ip in _self_ips():
        if self_ip not in known:
            extra += f"\nNmap scan report for {self_ip}\nHost is up.\n"
    result = (output + extra).strip()

    if not parse_scan(result):
        log.warning("SCAN: no hosts found")
        return None

    return result


_scan_status = {"last_scan": None, "last_ok": None, "last_error": None,
                "errors": 0}


def get_scan_status():
    """Статус скан-потока для /api/health (P1-9)."""
    st = dict(_scan_status)
    st["interval_sec"] = _scan_interval()
    st["scan_enabled"] = _scan_enabled()
    st["thread_alive"] = bool(_scan_thread and _scan_thread.is_alive())
    return st


def reconcile(con, current_devices, now=None):
    """DB-слой скана: upsert обнаруженных, misses/ONLINE/OFFLINE пропавших.

    Смена MAC у известного устройства дополнительно логируется событием
    MAC_CHANGED (name/device_type при этом обнуляются, как раньше).
    Возвращает статистику {new, online, offline, mac_changed}.
    """
    if now is None:
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    stats = {"new": 0, "online": 0, "offline": 0, "mac_changed": 0}

    previous = {
        row[0]: {"online": bool(row[1]), "misses": row[2]}
        for row in con.execute(
            "SELECT ip, online, misses FROM devices"
        ).fetchall()
    }

    # ОБНАРУЖЕННЫЕ УСТРОЙСТВА
    for ip, info in current_devices.items():
        hostname = info.get("hostname") or get_hostname(ip)
        mac = info.get("mac")
        vendor = info.get("vendor")

        row = con.execute(
            """
            SELECT online, hostname, mac, vendor, first_seen, appearances,
                   misses, name
            FROM devices
            WHERE ip=?
            """,
            (ip,),
        ).fetchone()

        if row:
            was_online = bool(row[0])
            old_mac = row[2]
            mac_changed = (
                bool(mac) and bool(old_mac)
                and str(mac).lower() != str(old_mac).lower()
            )

            con.execute(
                """
                UPDATE devices
                SET online=1,
                    hostname=?,
                    mac=COALESCE(?, mac),
                    vendor=COALESCE(?, vendor),
                    last_seen=?,
                    misses=0,
                    appearances=appearances+1,
                    name=CASE WHEN ?=1 THEN NULL ELSE name END,
                    device_type=CASE WHEN ?=1 THEN NULL ELSE device_type END
                WHERE ip=?
                """,
                (hostname, mac, vendor, now,
                 1 if mac_changed else 0, 1 if mac_changed else 0, ip),
            )

            if mac_changed:
                add_event(con, ip, hostname, mac, "MAC_CHANGED",
                          timestamp=now)
                stats["mac_changed"] += 1

            if not was_online:
                add_event(con, ip, hostname, mac, "ONLINE", timestamp=now)
                stats["online"] += 1
        else:
            con.execute(
                """
                INSERT INTO devices
                (ip, online, hostname, mac, vendor, first_seen, last_seen,
                 is_new, appearances, misses, name)
                VALUES (?, 1, ?, ?, ?, ?, ?, 1, 1, 0, NULL)
                """,
                (ip, hostname, mac, vendor, now, now),
            )
            add_event(con, ip, hostname, mac, "NEW", timestamp=now)
            stats["new"] += 1

    # НЕ ОБНАРУЖЕННЫЕ УСТРОЙСТВА
    for ip, state in previous.items():
        if ip in current_devices:
            continue
        if not state["online"]:
            continue

        new_misses = state["misses"] + 1

        if new_misses >= _max_misses():
            con.execute(
                "UPDATE devices SET online=0, misses=? WHERE ip=?",
                (new_misses, ip),
            )
            row = con.execute(
                "SELECT hostname, mac FROM devices WHERE ip=?", (ip,)
            ).fetchone()
            add_event(con, ip, row[0] if row else None,
                      row[1] if row else None, "OFFLINE", timestamp=now)
            stats["offline"] += 1
        else:
            con.execute(
                "UPDATE devices SET misses=? WHERE ip=?",
                (new_misses, ip),
            )

    return stats


def scan_loop():
    while True:
        if not _scan_enabled():
            # PHASE 16 №59: ведёт другая копия — просто ждём, сеть не трогаем
            time.sleep(_scan_interval())
            continue
        con = None
        try:
            output = run_scan()

            if output is None:
                _scan_status["last_scan"] = datetime.now().strftime(
                    "%d.%m.%Y %H:%M:%S"
                )
                _scan_status["last_error"] = (
                    "сканирование недоступно (см. журнал)"
                )
                _scan_status["errors"] += 1
                time.sleep(_scan_interval())
                continue

            current_devices = parse_scan(output)
            now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

            from modules.devices_routes import get_db

            con = get_db()
            reconcile(con, current_devices, now)
            con.commit()

            _scan_status["last_scan"] = now
            _scan_status["last_ok"] = now
            _scan_status["last_error"] = None

            log.info(f"SCAN OK: {len(current_devices)} devices")

        except Exception as e:
            _scan_status["last_scan"] = datetime.now().strftime(
                "%d.%m.%Y %H:%M:%S"
            )
            _scan_status["last_error"] = str(e)
            _scan_status["errors"] += 1
            log.error(f"SCAN ERROR: {e}")
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

        time.sleep(_scan_interval())


_scan_lock = threading.Lock()
_scan_thread = None


def start_scan_thread():
    """Запустить скан-поток; повторный вызов — no-op (P1-8 guard)."""
    global _scan_thread
    with _scan_lock:
        if _scan_thread is not None and _scan_thread.is_alive():
            return _scan_thread
        _scan_thread = threading.Thread(target=scan_loop, daemon=True)
        _scan_thread.start()
        return _scan_thread
