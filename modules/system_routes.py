import sqlite3, json, subprocess, os, platform, glob, logging, shutil, time, threading, re, socket
from datetime import datetime
from pathlib import Path
from flask import render_template, jsonify, request, redirect, url_for, current_app
from concurrent.futures import ThreadPoolExecutor
from core.hardware import (
    detect_platform, emmc_device, hdd_device, thermal_temp,
)
from core import samba_guest

DB = "/opt/lan-discovery/devices.db"
SETTINGS_PATH = "/etc/lan-discovery/settings.json"
DB_BACKUP_DIR = "/srv/backup-db"
DB_BACKUP_PATTERN = "devices_*.db"
DISK_REPLACE_SCRIPT = "/opt/lan-discovery/disk_replace.py"
CLONE_STATE_FILE = "/tmp/lan-discovery-clone.json"
IPTV_DIR = "/srv/media/IPTV"
IPTV_UPDATE_STATUS = "/etc/lan-discovery/iptv-update-status.json"
NETWORK_CONFIG = "/etc/lan-discovery/network.json"

SERVICE_ACTIONS = {
    "transmission": "transmission-daemon",
    "minidlna": "minidlna",
    "smb": "smbd"
}

_settings_cache = {"data": None, "ts": 0}
_about_cache = {"data": None, "ts": 0}
_disk_sizes = {"hdd": None, "sd": None}
_boot_device = None
_status_cache = {"data": None, "ts": 0}
_health_cache = {"data": None, "ts": 0}
_rate_limits = {}
_page_data_cache = {"data": None, "ts": 0}
_iptv_playlists_cache = {"data": None, "ts": 0}
_iptv_update_status_cache = {"data": None, "ts": 0}
_network_config_cache = {"data": None, "ts": 0}


def load_settings():
    now = time.time()
    if _settings_cache["data"] is not None and now - _settings_cache["ts"] < 10:
        return _settings_cache["data"]
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _settings_cache["data"] = data
        _settings_cache["ts"] = now
        return data
    except Exception:
        return {}


def _cfg(section, key, default=None):
    s = load_settings()
    return s.get(section, {}).get(key, default)


def _cmd(cmd, timeout=5):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _read_file(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def _format_dt(dt_str):
    if not dt_str:
        return dt_str
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        pass
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass
    try:
        parts = dt_str.split()
        if len(parts) >= 6:
            cleaned = " ".join(parts[:5]) + " " + parts[-1]
            dt = datetime.strptime(cleaned, "%a %b %d %I:%M:%S %p %Y")
            return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        pass
    try:
        parts = dt_str.split()
        if len(parts) == 3 and len(parts[1]) == 2:
            year = datetime.now().year
            cleaned = f"{parts[0]} {parts[1]} {year} {parts[2]}"
            dt = datetime.strptime(cleaned, "%b %d %Y %H:%M:%S")
            return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        pass
    return dt_str


def _human_size(value):
    try:
        value = float(value)
    except Exception:
        return str(value)

    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0

    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1

    if i == 0:
        return f"{int(value)} {units[i]}"

    return f"{value:.1f} {units[i]}"


def _check_rate(action, cooldown):
    now = time.time()
    last = _rate_limits.get(action, 0)
    if now - last < cooldown:
        return False
    _rate_limits[action] = now
    return True


def load_iptv_update_status():
    now = time.time()
    if _iptv_update_status_cache["data"] is not None and now - _iptv_update_status_cache["ts"] < 60:
        return _iptv_update_status_cache["data"]
    try:
        with open(IPTV_UPDATE_STATUS, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        _iptv_update_status_cache["data"] = data
        _iptv_update_status_cache["ts"] = now
        return data

    except Exception:
        return {}


def load_iptv_playlists():
    now = time.time()
    if _iptv_playlists_cache["data"] is not None and now - _iptv_playlists_cache["ts"] < 60:
        return _iptv_playlists_cache["data"]
    try:
        with open("/etc/lan-discovery/iptv-playlists.json", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        _iptv_playlists_cache["data"] = data
        _iptv_playlists_cache["ts"] = now
        return data

    except Exception:
        return []


def load_network_config():
    now = time.time()
    if _network_config_cache["data"] is not None and now - _network_config_cache["ts"] < 60:
        return _network_config_cache["data"]
    try:
        with open(NETWORK_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        _network_config_cache["data"] = data
        _network_config_cache["ts"] = now
        return data
    except Exception:
        return {"hosts": _cfg("network", "default_hosts", ["google.com", "ya.ru", "192.168.3.1"])}


def playlist_info(item):
    filename = item.get("file", "")
    path = os.path.join(IPTV_DIR, filename)

    if not os.path.exists(path):
        return {
            "size": "-",
            "mtime": "-"
        }

    try:
        size = os.path.getsize(path)

        if size >= 1024**3:
            size_text = f"{size / 1024**3:.1f} GiB"
        elif size >= 1024**2:
            size_text = f"{size / 1024**2:.1f} MiB"
        elif size >= 1024:
            size_text = f"{size / 1024:.1f} KiB"
        else:
            size_text = f"{size} B"

        mtime = datetime.fromtimestamp(
            os.path.getmtime(path)
        ).strftime("%d.%m.%Y %H:%M:%S")

        return {
            "size": size_text,
            "mtime": mtime
        }

    except Exception:
        return {
            "size": "-",
            "mtime": "-"
        }


def board_title():
    """Короткое имя платы для заголовков (напр. 'X96 Max')."""
    m = (_read_file("/proc/device-tree/model") or "").replace("\x00", "").strip()
    if not m:
        return socket.gethostname()
    if "," in m:
        m = m.rsplit(",", 1)[-1].strip()
    junk = {"ltd", "inc", "co", "llc", "gmbh", "sa", "ag", "corp", "corporation", "company", "limited"}
    words = m.split()
    while len(words) > 1 and words[0].lower().rstrip(".") in junk:
        words = words[1:]
    return " ".join(words) or m


def about_data():
    now = time.time()
    if _about_cache["data"] is not None and now - _about_cache["ts"] < 60:
        return _about_cache["data"]

    data = {
        "board": {},
        "os": {},
        "cpu": {},
        "memory": {},
        "disks": [],
        "network": [],
        "network_physical": [],
        "network_virtual": [],
        "usb": [],
        "usb_tree": "",
        "filesystems": [],
        "software": {},
        "services": [],
    }

    model = (_read_file("/proc/device-tree/model") or "").replace("\x00", "").strip()

    data["board"] = {
        "model": model or "Unknown",
        "device_tree": (_read_file("/proc/device-tree/compatible") or "").replace("\x00", " ").strip(),
    }

    os_release = {}

    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os_release[k] = v.strip('"')
    except Exception:
        pass

    data["os"] = {
        "pretty_name": os_release.get("PRETTY_NAME", ""),
        "name": os_release.get("NAME", ""),
        "version": os_release.get("VERSION", ""),
        "version_id": os_release.get("VERSION_ID", ""),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
        "python": platform.python_version(),
        "uptime": _cmd(["uptime", "-p"]).strip(),
    }

    cpuinfo = _cmd(["lscpu"])

    cpu = {
        "architecture": platform.machine(),
        "cores": os.cpu_count(),
        "model": "",
        "vendor": "",
        "min_mhz": "",
        "max_mhz": "",
    }

    for line in cpuinfo.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if key == "Model name":
            cpu["model"] = value
        elif key == "Vendor ID":
            cpu["vendor"] = value
        elif key == "CPU max MHz":
            cpu["max_mhz"] = value
        elif key == "CPU min MHz":
            cpu["min_mhz"] = value
        elif key == "Architecture":
            cpu["architecture"] = value

    if not cpu["model"]:
        cpu["model"] = _read_file("/proc/cpuinfo").splitlines()[0] \
            if _read_file("/proc/cpuinfo") else "Unknown"

    data["cpu"] = cpu

    meminfo = {}

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meminfo[k] = v.strip()
    except Exception:
        pass

    def mem_kib(name):
        value = meminfo.get(name, "0").split()[0]
        try:
            return int(value)
        except Exception:
            return 0

    mem_total = mem_kib("MemTotal") * 1024
    mem_available = mem_kib("MemAvailable") * 1024
    swap_total = mem_kib("SwapTotal") * 1024
    swap_free = mem_kib("SwapFree") * 1024

    data["memory"] = {
        "total": _human_size(mem_total),
        "available": _human_size(mem_available),
        "used": _human_size(max(0, mem_total - mem_available)),
        "swap_total": _human_size(swap_total),
        "swap_used": _human_size(max(0, swap_total - swap_free)),
    }

    lsblk_raw = _cmd([
        "lsblk",
        "-J",
        "-b",
        "-e", "7",
        "-o",
        "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,VENDOR,REV,"
        "TRAN,FSTYPE,FSVER,LABEL,UUID,MOUNTPOINTS,ROTA,RM"
    ])

    try:
        lsblk = json.loads(lsblk_raw)
    except Exception:
        lsblk = {}

    def process_block(dev, parent=None):

        name = dev.get("name", "")
        if not name:
            return

        dtype = dev.get("type", "")

        if dtype not in ("disk", "part"):
            return

        sysbase = Path("/sys/class/block") / name

        item = {
            "name": name,
            "path": dev.get("path") or f"/dev/{name}",
            "type": dtype,
            "size": _human_size(dev.get("size", 0)),
            "size_bytes": dev.get("size", 0),
            "model": (dev.get("model") or "").strip(),
            "vendor": (dev.get("vendor") or "").strip(),
            "serial": (dev.get("serial") or "").strip(),
            "rev": (dev.get("rev") or "").strip(),
            "transport": dev.get("tran") or "",
            "fstype": dev.get("fstype") or "",
            "fsver": dev.get("fsver") or "",
            "label": dev.get("label") or "",
            "uuid": dev.get("uuid") or "",
            "mountpoints": [
                x for x in (dev.get("mountpoints") or [])
                if x
            ],
            "rotational": bool(dev.get("rota")),
            "removable": bool(dev.get("rm")),
        }

        sys_device = sysbase / "device"

        for field in (
            "vendor",
            "model",
            "rev",
            "serial",
            "state",
            "type",
        ):
            value = _read_file(sys_device / field)
            if value and not item.get(field):
                item[field] = value

        logical = _read_file(
            sysbase / "queue" / "logical_block_size"
        )
        physical = _read_file(
            sysbase / "queue" / "physical_block_size"
        )

        item["logical_block_size"] = logical
        item["physical_block_size"] = physical

        if name.startswith("mmcblk") and not "p" in name:

            mmc = {}

            for field in (
                "name",
                "cid",
                "csd",
                "date",
                "fwrev",
                "hwrev",
                "manfid",
                "oemid",
                "serial",
            ):
                value = _read_file(
                    f"/sys/class/block/{name}/device/{field}"
                )
                if value:
                    mmc[field] = value

            item["mmc"] = mmc

        if dtype == "disk":

            udev = _cmd([
                "udevadm",
                "info",
                "--query=property",
                "--name",
                item["path"]
            ])

            for line in udev.splitlines():
                if "=" not in line:
                    continue

                k, v = line.split("=", 1)

                if k == "ID_SERIAL":
                    item["ata_serial"] = v
                elif k == "ID_SERIAL_SHORT":
                    item["ata_serial_short"] = v
                elif k == "ID_MODEL":
                    item["udev_model"] = v
                elif k == "ID_MODEL_FROM_DATABASE":
                    item["model_database"] = v
                elif k == "ID_ATA_ROTATION_RATE_RPM":
                    item["rotation_rpm"] = v
                elif k == "ID_WWN":
                    item["wwn"] = v
                elif k == "ID_BUS":
                    item["bus"] = v
                elif k == "ID_USB_SERIAL":
                    item["usb_serial"] = v
                elif k == "ID_USB_SERIAL_SHORT":
                    item["usb_serial_short"] = v
                elif k == "ID_USB_VENDOR":
                    item["usb_vendor"] = v
                elif k == "ID_USB_VENDOR_ID":
                    item["usb_vendor_id"] = v
                elif k == "ID_USB_MODEL_ID":
                    item["usb_model_id"] = v
                elif k == "ID_USB_REVISION":
                    item["usb_revision"] = v
                elif k == "ID_USB_DRIVER":
                    item["usb_driver"] = v

        data["disks"].append(item)

        for child in dev.get("children", []) or []:
            process_block(child, item)

    for dev in lsblk.get("blockdevices", []) or []:
        process_block(dev)

    ip_raw = _cmd(["ip", "-j", "addr"])

    try:
        interfaces = json.loads(ip_raw)
    except Exception:
        interfaces = []

    for iface in interfaces:

        name = iface.get("ifname", "")
        if not name:
            continue

        info = {
            "name": name,
            "state": iface.get("operstate", ""),
            "mac": iface.get("address", ""),
            "mtu": iface.get("mtu", ""),
            "kind": iface.get("link_type", ""),
            "type": iface.get("linkinfo", {}).get("info_kind", ""),
            "addresses": [],
            "driver": "",
            "speed": "",
        }

        for addr in iface.get("addr_info", []):
            info["addresses"].append({
                "family": addr.get("family", ""),
                "address": addr.get("local", ""),
                "prefix": addr.get("prefixlen", ""),
            })

        driver = _read_file(
            f"/sys/class/net/{name}/device/driver"
        )

        if driver:
            info["driver"] = Path(driver).name

        speed = _read_file(
            f"/sys/class/net/{name}/speed"
        )

        if speed and speed.isdigit():
            info["speed"] = f"{speed} Mbps"

        data["network"].append(info)

        virtual = (
            name == "lo"
            or name.startswith("docker")
            or name.startswith("br-")
            or name.startswith("veth")
            or info["type"] in (
                "bridge",
                "veth",
            )
        )

        if virtual:
            data["network_virtual"].append(info)
        else:
            data["network_physical"].append(info)

    usb_devices = []

    for path in sorted(
        glob.glob("/sys/bus/usb/devices/*")
    ):

        p = Path(path)

        if not (p / "idVendor").exists():
            continue

        item = {
            "sysfs": p.name,
            "manufacturer": _read_file(p / "manufacturer"),
            "product": _read_file(p / "product"),
            "serial": _read_file(p / "serial"),
            "vid": _read_file(p / "idVendor"),
            "pid": _read_file(p / "idProduct"),
            "bcd_device": _read_file(p / "bcdDevice"),
            "speed": _read_file(p / "speed"),
            "bus": _read_file(p / "busnum"),
            "device": _read_file(p / "devnum"),
            "port": p.name,
            "driver": "",
        }

        driver_link = p / "driver"

        try:
            item["driver"] = driver_link.resolve().name
        except Exception:
            pass

        item["class"] = _read_file(p / "bDeviceClass")
        item["subclass"] = _read_file(p / "bDeviceSubClass")
        item["protocol"] = _read_file(p / "bDeviceProtocol")

        usb_devices.append(item)

    data["usb"] = usb_devices

    data["usb_tree"] = _cmd(
        ["lsusb", "-t"],
        timeout=5
    )

    for mount in ("/", "/srv", "/var/log", "/var/log.hdd"):

        try:
            st = os.statvfs(mount)

            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free

            percent = (
                round((used / total) * 100, 1)
                if total
                else 0
            )

            data["filesystems"].append({
                "mount": mount,
                "total": _human_size(total),
                "used": _human_size(used),
                "free": _human_size(free),
                "percent": percent,
            })

        except Exception:
            pass

    try:
        app_size = os.path.getsize("/opt/lan-discovery/app.py")
    except Exception:
        app_size = 0

    try:
        db_size = os.path.getsize("/opt/lan-discovery/devices.db")
    except Exception:
        db_size = 0

    data["software"] = {
        "python": platform.python_version(),
        "flask": getattr(__import__("flask"), "__version__", "unknown"),
        "app": "/opt/lan-discovery/app.py",
        "app_size": _human_size(app_size),
        "database": "/opt/lan-discovery/devices.db",
        "database_size": _human_size(db_size),
    }

    service_names = [
        "lan-discovery",
        "transmission-daemon",
        "smbd",
        "nmbd",
        "minidlna",
        "docker",
        "ssh",
        "chrony",
        "cron",
        "weather-update.timer",
    ]

    def _check_service(svc):
        active = _cmd(["systemctl", "is-active", svc])
        enabled = _cmd(["systemctl", "is-enabled", svc])
        return {"name": svc, "active": active or "unknown", "enabled": enabled or "unknown"}

    with ThreadPoolExecutor(max_workers=5) as pool:
        service_results = list(pool.map(_check_service, service_names))

    data["services"] = service_results

    _about_cache["data"] = data
    _about_cache["ts"] = time.time()

    return data


def backup_test_status():
    try:
        unit = "backup-emmc-test.service"

        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                unit
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.stdout.strip() in ("active", "activating"):
            return "running", None

        status_file = "/var/lib/lan-discovery/backup-test.status"
        if os.path.exists(status_file):
            try:
                with open(status_file) as fh:
                    parts = fh.read().strip().split(None, 1)
                if len(parts) == 2 and parts[0] in ("ok", "error"):
                    return parts[0], parts[1]
            except Exception:
                pass

        result = subprocess.run(
            [
                "journalctl",
                "-u", unit,
                "-n", "30",
                "--no-pager",
                "-o", "short"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        lines = result.stdout.splitlines()

        for line in reversed(lines):

            if "Finished backup-emmc-test.service" in line:
                timestamp = line[:15].strip()

                for check_line in reversed(lines):
                    if "Deactivated successfully" in check_line:
                        return "ok", timestamp

                return "error", timestamp

            if "Failed with result" in line:
                timestamp = line[:15].strip()
                return "error", timestamp

        return "never", None

    except Exception:
        return "never", None


def iptv_update_status():
    try:
        state = service_state("update-iptv.service")

        if state == "active":
            return "running", None

        result = subprocess.run(
            [
                "journalctl",
                "-u", "update-iptv.service",
                "-n", "50",
                "--no-pager",
                "-o", "short"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        lines = result.stdout.splitlines()

        for line in reversed(lines):

            if "=== IPTV UPDATE OK:" in line:
                timestamp = line.split(
                    "=== IPTV UPDATE OK:",
                    1
                )[1].strip().rstrip("=").strip()

                return "ok", timestamp

            if "=== IPTV UPDATE WITH ERRORS:" in line:
                timestamp = line.split(
                    "=== IPTV UPDATE WITH ERRORS:",
                    1
                )[1].strip().rstrip("=").strip()

                return "error", timestamp

        try:
            with open(IPTV_UPDATE_STATUS, "r", encoding="utf-8") as f:
                status_data = json.load(f)
            latest = None
            for key, val in status_data.items():
                if isinstance(val, dict) and "status" in val:
                    if latest is None or val.get("time", "") > latest.get("time", ""):
                        latest = val
            if latest:
                return latest.get("status", "never"), latest.get("time")
        except Exception:
            pass

        return "never", None

    except Exception:
        return "never", None


def service_state(service):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def service_last_log(service, success_text):
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", service,
                "-n", "50",
                "--no-pager",
                "-o", "short"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        lines = result.stdout.splitlines()

        for line in reversed(lines):
            if success_text in line:
                return line

        return None

    except Exception:
        return None


def _root_mount_source():
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                p = line.split()
                if len(p) >= 2 and p[1] == "/":
                    return p[0]
    except Exception:
        pass
    return ""


def _block_kind(disk_name):
    """Тип блочного диска: SD / MMC (eMMC) / HDD / None."""
    if re.match(r"^mmcblk\d+$", disk_name):
        try:
            with open("/sys/block/%s/device/type" % disk_name, "r") as fh:
                t = fh.read().strip()
            return t if t in ("SD", "MMC") else None
        except Exception:
            return None
    if re.match(r"^sd[a-z]$", disk_name):
        return "HDD"
    return None


def find_typed_block(kind):
    """Первый блочный девайс типа kind (SD/MMC) — не зависит от номера mmcblk0/1/2."""
    try:
        for name in sorted(os.listdir("/sys/block")):
            if re.match(r"^mmcblk\d+$", name) and _block_kind(name) == kind:
                return name
    except Exception:
        pass
    return None


def root_on_sd():
    m = re.match(r"(mmcblk\d+)", os.path.basename(_root_mount_source()))
    return bool(m) and _block_kind(m.group(1)) == "SD"


def fs_disk(path):
    """Носитель точки монтирования → (dev, label, size)."""
    src = None
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                p = line.split()
                if len(p) >= 2 and p[1] == path:
                    src = p[0]
    except Exception:
        pass
    if src is None and path != "/":
        return fs_disk("/")
    if not src:
        return None, "", None
    m = re.match(r"^(mmcblk\d+|sd[a-z])", os.path.basename(src))
    if not m:
        return None, "", None
    disk = m.group(1)
    label = {"SD": "SD-карта", "MMC": "eMMC", "HDD": "HDD"}.get(_block_kind(disk), "")
    size = None
    out = _cmd(["lsblk", "-dno", "SIZE", "/dev/" + disk], timeout=5)
    lines = out.strip().splitlines()
    if lines and lines[-1].strip():
        size = lines[-1].strip()
    return "/dev/" + disk, label, size


def emmc_backup_guard():
    """Политика: бэкап eMMC выполняется только при загрузке с eMMC И подключённом HDD.

    Загрузка с SD обычно означает проблемы с eMMC или тестовый запуск —
    в этом случае eMMC бэкапить нельзя. Без HDD копию eMMC сохранять некуда.
    """
    if root_on_sd():
        return False, "система загружена с SD-карты (проблемы с eMMC или тестовый запуск)"

    try:
        if any(name.startswith("sd") for name in os.listdir("/sys/block")):
            return True, ""
    except Exception:
        pass

    return False, "HDD не подключен — копию eMMC некуда сохранять"


def clone_guard():
    """Клонирование eMMC→SD: нельзя при загрузке с SD — перезапишет работающую систему."""
    if root_on_sd():
        return False, "недоступно при загрузке с SD-карты"
    if not find_typed_block("SD"):
        return False, "SD-карта не обнаружена"
    if not find_typed_block("MMC"):
        return False, "eMMC не обнаружена"
    return True, ""


def backup_file_status(path):
    try:
        p = Path(path)

        if not p.exists():
            return "missing"

        if not p.is_file():
            return "invalid"

        size = p.stat().st_size

        if size <= 0:
            return "invalid"

        return "ok"

    except Exception:
        return "invalid"


def file_info(path):
    try:
        p = Path(path)

        if not p.exists():
            return None

        size = p.stat().st_size

        if size >= 1024**3:
            return f"{size / 1024**3:.1f} GiB"

        if size >= 1024**2:
            return f"{size / 1024**2:.1f} MiB"

        return f"{size / 1024:.1f} KiB"

    except Exception:
        return None


def db_backup_list():
    backups = []
    try:
        d = Path(DB_BACKUP_DIR)
        if not d.exists():
            return backups
        for f in sorted(d.glob(DB_BACKUP_PATTERN), reverse=True):
            name = f.name
            size = f.stat().st_size
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")
            backups.append({"name": name, "size": size, "mtime": mtime})
    except Exception:
        pass
    return backups


def db_backup_size_human(size_bytes):
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f} MiB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    return f"{size_bytes} B"


def db_backup_last():
    backups = db_backup_list()
    if backups:
        return backups[0]
    return None


def db_backup_running():
    return service_state("backup-db.service") in ("active", "activating")


def db_restore_running():
    return service_state("db-restore.service") in ("active", "activating")


def emmc_restore_running():
    return service_state("emmc-restore.service") in ("active", "activating")


def timer_next_run(timer):
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                timer,
                "--property=NextElapseUSecRealtime",
                "--value"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        value = result.stdout.strip()

        if not value or value == "n/a":
            return "-"

        try:
            parts = value.split()
            if len(parts) >= 3:
                dt_str = parts[1] + " " + parts[2]
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            pass

        return value

    except Exception:
        return "-"


def _clone_dd_alive():
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            if cmd.startswith("dd ") and "of=/dev/mmcblk" in cmd:
                return True
    except Exception:
        return True
    return False


def _dd_progress_watcher(proc):
    dev = None
    try:
        with open("/proc/%d/cmdline" % proc.pid, "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        m = re.search(r"of=/dev/(mmcblk\d+|sd[a-z])", cmd)
        if m:
            dev = m.group(1)
    except Exception:
        dev = None
    if not dev:
        dev = emmc_device()
    if not dev:
        return
    try:
        with open(f"/sys/block/{dev}/size", "r", encoding="utf-8") as f:
            total = int(f.read().strip()) * 512
    except Exception:
        return

    if not total:
        return

    while proc.poll() is None:
        try:
            rchar = 0
            with open("/proc/%d/io" % proc.pid, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("rchar:"):
                        rchar = int(line.split()[1])
                        break
            if not _load_clone_state().get("running"):
                break
            percent = 5 + int(90 * min(rchar, total) / total)
            _update_clone_state(
                percent=percent,
                text="Копирование eMMC... %d%%" % percent
            )
        except Exception:
            pass
        time.sleep(3)


def _load_clone_state():
    try:
        with open(CLONE_STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        return {"running": False, "ok": False, "error": None, "percent": 0, "text": ""}

    if state.get("running"):
        try:
            started = float(state.get("started") or os.path.getmtime(CLONE_STATE_FILE))
        except Exception:
            started = time.time()

        if time.time() - started > 60 and not _clone_dd_alive():
            state.update(running=False, error="Клонирование прервано (процесс dd не найден)")
            _save_clone_state(state)

    return state


def _save_clone_state(state):
    try:
        with open(CLONE_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _update_clone_state(**kwargs):
    state = _load_clone_state()
    state.update(kwargs)
    _save_clone_state(state)
    return state


def register_routes(app):
    from modules.auth import login_required, admin_required

    @app.context_processor
    def _inject_board_title():
        return {"board_title": board_title()}

    def page_data():
        now = time.time()
        if _page_data_cache["data"] is not None and now - _page_data_cache["ts"] < 10:
            return _page_data_cache["data"]

        from app import check_internet_cached, weather_current
        data = {
            "internet": check_internet_cached(),
            "interval": int(_cfg("network", "scan_interval", 30) or 30),
            "max_misses": int(_cfg("network", "max_misses", 6) or 6),
            "weather": weather_current()
        }
        _page_data_cache["data"] = data
        _page_data_cache["ts"] = now
        return data

    @app.route("/api/service/<service>/<action>", methods=["POST"])
    @admin_required
    @login_required
    def api_service_action(service, action):

        allowed_actions = {
            "restart": "restart",
            "start": "start",
            "stop": "stop"
        }

        service_name = SERVICE_ACTIONS.get(service)
        systemctl_action = allowed_actions.get(action)

        if service_name is None or systemctl_action is None:
            return {
                "ok": False,
                "error": "Недопустимая операция"
            }, 400

        try:

            result = subprocess.run(
                [
                    "systemctl",
                    systemctl_action,
                    service_name
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15
            )

            if result.returncode != 0:

                return {
                    "ok": False,
                    "error": (
                        result.stderr.strip()
                        or "systemctl завершился с ошибкой"
                    )
                }, 500

            return {
                "ok": True,
                "service": service_name,
                "action": systemctl_action
            }

        except subprocess.TimeoutExpired:

            return {
                "ok": False,
                "error": "Превышено время ожидания"
            }, 504

        except Exception as e:

            return {
                "ok": False,
                "error": str(e)
            }, 500

    @app.route("/api/samba/guest", methods=["GET"])
    @login_required
    def api_samba_guest_state():
        try:
            state = samba_guest.guest_state()
        except FileNotFoundError:
            return jsonify({
                "ok": False,
                "enabled": False,
                "shares": {},
                "error": "%s не найден" % samba_guest.SMB_CONF
            }), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, **state})

    @app.route("/api/samba/guest", methods=["POST"])
    @admin_required
    @login_required
    def api_samba_guest_toggle():
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({
                "ok": False,
                "error": 'Ожидается JSON {"enabled": true|false}'
            }), 400
        try:
            result = samba_guest.apply_samba_guest(enabled)
        except FileNotFoundError:
            return jsonify({
                "ok": False,
                "error": "%s не найден" % samba_guest.SMB_CONF
            }), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if not result.get("ok"):
            return jsonify(result), 500
        return jsonify(result)

    @app.route("/api/status")
    @login_required
    def api_status():
        now = time.time()
        if _status_cache["data"] is not None and now - _status_cache["ts"] < 2:
            return _status_cache["data"]

        result = {
            "temperature": None,
            "cpu_times": None,
            "ram_percent": None,
            "root_free": None,
            "root_total": None,
            "root_percent": None,
            "srv_free": None,
            "srv_total": None,
            "srv_percent": None,
            "sd_free": None,
            "sd_total": None,
            "sd_present": False,
            "sd_size": None,
            "sd_writing": False,
            "emmc_io_ticks": None,
            "hdd_io_ticks": None,
            "lan_rx_bytes": None,
            "lan_tx_bytes": None,
            "wifi_rx_bytes": None,
            "wifi_tx_bytes": None,
            "rx_bytes": None,
            "tx_bytes": None,
            "uptime": None,
            "load1": None,
            "load5": None,
            "load15": None,
            "services": {}
        }

        _t = thermal_temp()
        if _t is not None:
            result["temperature"] = _t

        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                first = f.readline().split()

            if first[0] == "cpu":
                result["cpu_times"] = [
                    int(value) for value in first[1:]
                ]
        except Exception:
            pass

        try:
            mem = {}

            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    key, value = line.split(":", 1)
                    mem[key] = int(value.strip().split()[0])

            total = mem.get("MemTotal", 0)
            available = mem.get("MemAvailable", 0)

            if total:
                result["ram_percent"] = round(
                    (total - available) * 100 / total
                )
        except Exception:
            pass

        sd_dev = None
        emmc_dev = None
        try:
            for _name in os.listdir("/sys/block"):
                if not _name.startswith("mmcblk") or "boot" in _name:
                    continue
                try:
                    with open(f"/sys/block/{_name}/device/type", "r", encoding="utf-8") as _f:
                        _t = _f.read().strip()
                except Exception:
                    continue
                if _t == "SD":
                    sd_dev = _name
                elif _t == "MMC":
                    emmc_dev = _name
        except Exception:
            pass
        _root_dev = ""
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as _f:
                for _line in _f:
                    _p = _line.split()
                    if len(_p) > 1 and _p[1] == "/":
                        _root_dev = _p[0]
                        break
        except Exception:
            pass
        _root_is_sd = bool(sd_dev) and ("/dev/" + sd_dev) in _root_dev

        for key_free, key_total, path in (
            ("root_free", "root_total", "/"),
            ("srv_free", "srv_total", "/srv")
        ):
            try:
                if key_free == "srv_free":
                    import stat as _stat_mod
                    if os.stat("/srv").st_dev == os.stat("/").st_dev:
                        continue

                stat = os.statvfs(path)

                total = stat.f_blocks * stat.f_frsize
                free = stat.f_bavail * stat.f_frsize

                if total:
                    result[key_free] = free
                    result[key_total] = total

                    percent_key = (
                        "root_percent"
                        if key_free == "root_free"
                        else "srv_percent"
                    )

                    result[percent_key] = round(
                        (total - free) * 100 / total
                    )
            except Exception:
                pass

        for sd_path in ("/media", "/mnt"):
            try:
                entries = os.listdir(sd_path)

                for entry in entries:
                    mount = os.path.join(sd_path, entry)

                    if os.path.ismount(mount):
                        stat = os.statvfs(mount)
                        result["sd_free"] = stat.f_bavail * stat.f_frsize
                        result["sd_total"] = stat.f_blocks * stat.f_frsize
                        break

                if result["sd_total"] is not None:
                    break

            except Exception:
                pass

        try:
            if sd_dev and os.path.exists("/dev/" + sd_dev):
                result["sd_present"] = True

                with open(f"/sys/block/{sd_dev}/size", "r", encoding="utf-8") as f:
                    result["sd_size"] = int(f.read().strip()) * 512

                if result["sd_total"] is None and _root_is_sd:
                    _st = os.statvfs("/")
                    result["sd_free"] = _st.f_bavail * _st.f_frsize
                    result["sd_total"] = _st.f_blocks * _st.f_frsize

                if result["sd_total"] is None and _load_clone_state().get("running"):
                    result["sd_writing"] = True
        except Exception:
            pass

        for device, result_key in (
            (emmc_dev, "emmc_io_ticks"),
            (hdd_device(), "hdd_io_ticks")
        ):
            if not device:
                continue
            try:
                with open(
                    f"/sys/block/{device}/stat",
                    "r",
                    encoding="utf-8"
                ) as f:
                    fields = f.read().split()

                if len(fields) >= 13:
                    result[result_key] = int(fields[9])

            except Exception:
                pass

        for interface in _cfg("network", "traffic_ifaces",
                              ["end0", "eth0", "wlan1", "wlan0"]):
            if interface.startswith("wlan"):
                rx_key, tx_key = "wifi_rx_bytes", "wifi_tx_bytes"
            else:
                rx_key, tx_key = "lan_rx_bytes", "lan_tx_bytes"
            if result.get(rx_key) is not None:
                continue
            try:
                with open(
                    f"/sys/class/net/{interface}/statistics/rx_bytes",
                    "r",
                    encoding="utf-8"
                ) as f:
                    result[rx_key] = int(f.read().strip())

                with open(
                    f"/sys/class/net/{interface}/statistics/tx_bytes",
                    "r",
                    encoding="utf-8"
                ) as f:
                    result[tx_key] = int(f.read().strip())

            except Exception:
                pass

        result["rx_bytes"] = result["lan_rx_bytes"]
        result["tx_bytes"] = result["lan_tx_bytes"]

        try:
            with open(
                "/proc/uptime",
                "r",
                encoding="utf-8"
            ) as f:
                uptime_seconds = float(
                    f.read().split()[0]
                )

            result["uptime"] = int(
                uptime_seconds
            )

        except Exception:
            pass

        try:
            with open(
                "/proc/loadavg",
                "r",
                encoding="utf-8"
            ) as f:
                load = f.read().split()

            result["load1"] = float(load[0])
            result["load5"] = float(load[1])
            result["load15"] = float(load[2])

        except Exception:
            pass

        for service in (
            "transmission-daemon",
            "minidlna",
            "smbd",
            "lan-discovery"
        ):
            try:
                result["services"][service] = (
                    subprocess.run(
                        [
                            "systemctl",
                            "is-active",
                            service
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2
                    ).returncode == 0
                )
            except Exception:
                result["services"][service] = False

        _status_cache["data"] = result
        _status_cache["ts"] = time.time()
        return result

    @app.route("/api/system/health")
    @login_required
    def api_system_health():
        now = time.time()
        if _health_cache["data"] is not None and now - _health_cache["ts"] < 30:
            return _health_cache["data"]

        warnings = []

        cpu_temp = thermal_temp()
        if cpu_temp is not None:
            if cpu_temp >= 80:
                warnings.append({"level": "critical", "text": "CPU перегрев: %.1f°C" % cpu_temp, "icon": "🔥"})
            elif cpu_temp >= 70:
                warnings.append({"level": "warning", "text": "CPU нагрев: %.1f°C" % cpu_temp, "icon": "⚠️"})

        try:
            st = os.statvfs("/")
            root_pct = round((1 - st.f_bavail / st.f_blocks) * 100, 1) if st.f_blocks else 0
            if root_pct >= 95:
                warnings.append({"level": "critical", "text": "Корневой диск заполнен на %.1f%%" % root_pct, "icon": "💾"})
            elif root_pct >= 90:
                warnings.append({"level": "warning", "text": "Корневой диск: %.1f%%" % root_pct, "icon": "💾"})
        except Exception:
            pass

        try:
            if os.stat("/srv").st_dev != os.stat("/").st_dev:
                st = os.statvfs("/srv")
                srv_pct = round((1 - st.f_bavail / st.f_blocks) * 100, 1) if st.f_blocks else 0
                if srv_pct >= 95:
                    warnings.append({"level": "critical", "text": "HDD (/srv) заполнен на %.1f%%" % srv_pct, "icon": "💿"})
                elif srv_pct >= 90:
                    warnings.append({"level": "warning", "text": "HDD (/srv): %.1f%%" % srv_pct, "icon": "💿"})
        except Exception:
            pass

        try:
            with open("/proc/meminfo", "r") as f:
                mem = {}
                for line in f:
                    key, val = line.split(":", 1)
                    mem[key] = int(val.strip().split()[0])
            total = mem.get("MemTotal", 1)
            avail = mem.get("MemAvailable", 0)
            ram_pct = round((1 - avail / total) * 100, 1)
            if ram_pct >= 95:
                warnings.append({"level": "critical", "text": "RAM использована на %.1f%%" % ram_pct, "icon": "🧠"})
            elif ram_pct >= 85:
                warnings.append({"level": "warning", "text": "RAM: %.1f%%" % ram_pct, "icon": "🧠"})
        except Exception:
            pass

        try:
            load1 = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            if load1 >= cores * 2:
                warnings.append({"level": "critical", "text": "Нагрузка CPU: %.1f (%d ядра)" % (load1, cores), "icon": "⚡"})
            elif load1 >= cores:
                warnings.append({"level": "warning", "text": "Нагрузка CPU: %.1f" % load1, "icon": "⚡"})
        except Exception:
            pass

        hdd_disks = []
        try:
            hdd_disks = sorted(n for n in os.listdir("/sys/block") if re.match(r"^sd[a-z]$", n))
        except Exception:
            pass

        for _disk in hdd_disks:
            try:
                r = subprocess.run(
                    ["smartctl", "-H", "/dev/" + _disk],
                    capture_output=True, text=True, timeout=10
                )
                smart_out = r.stdout + r.stderr
                if "FAILED" in smart_out.upper() or "PASSED" not in smart_out.upper():
                    warnings.append({"level": "critical", "text": "SMART: диск /dev/%s требует проверки" % _disk, "icon": "🔧"})
            except Exception:
                pass

            try:
                r = subprocess.run(
                    ["smartctl", "-A", "/dev/" + _disk],
                    capture_output=True, text=True, timeout=10
                )
                for line in r.stdout.splitlines():
                    if "Temperature" in line and "Celsius" in line:
                        parts = line.split()
                        raw_val = parts[-1] if parts else ""
                        try:
                            hdd_temp = int(raw_val.split("(")[0])
                            if hdd_temp >= 55:
                                warnings.append({"level": "critical", "text": "HDD перегрев: %d°C" % hdd_temp, "icon": "🔥"})
                            elif hdd_temp >= 45:
                                warnings.append({"level": "warning", "text": "HDD нагрев: %d°C" % hdd_temp, "icon": "⚠️"})
                        except ValueError:
                            pass
                        break
            except Exception:
                pass

        result = {
            "ok": len(warnings) == 0,
            "warnings": warnings,
            "checked_at": time.strftime("%H:%M:%S")
        }
        _health_cache["data"] = result
        _health_cache["ts"] = time.time()
        return result

    @app.route("/api/health")
    def api_health():
        checks = {}
        ok = True
        db_status = {}

        try:
            con = sqlite3.connect(DB, timeout=5)
            try:
                con.execute("SELECT 1")
                db_status["status"] = "ok"
                db_status["path"] = DB
                db_status["size_bytes"] = os.path.getsize(DB)
                db_status["journal_mode"] = con.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                db_status["user_version"] = con.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                db_status["devices"] = con.execute(
                    "SELECT COUNT(*) FROM devices"
                ).fetchone()[0]
                db_status["events"] = con.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
            finally:
                con.close()
            checks["database"] = "ok"
        except Exception as e:
            db_status["status"] = "error: %s" % e
            checks["database"] = str(e)
            ok = False

        try:
            st = os.statvfs("/")
            free_pct = (st.f_bavail / st.f_blocks) * 100 if st.f_blocks else 0
            if free_pct < 5:
                checks["disk"] = "critical (%.1f%% free)" % free_pct
                ok = False
            elif free_pct < 15:
                checks["disk"] = "warning (%.1f%% free)" % free_pct
            else:
                checks["disk"] = "ok (%.1f%% free)" % free_pct
        except Exception as e:
            checks["disk"] = str(e)
            ok = False

        try:
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    k, v = line.split(":", 1)
                    mem[k] = int(v.strip().split()[0])
            total = mem.get("MemTotal", 1)
            avail = mem.get("MemAvailable", 0)
            used_pct = round((1 - avail / total) * 100, 1)
            if used_pct >= 95:
                checks["ram"] = "critical (%.1f%%)" % used_pct
                ok = False
            elif used_pct >= 85:
                checks["ram"] = "warning (%.1f%%)" % used_pct
            else:
                checks["ram"] = "ok (%.1f%%)" % used_pct
        except Exception as e:
            checks["ram"] = str(e)

        temp = thermal_temp()
        if temp is None:
            checks["cpu_temp"] = "unavailable"
        elif temp >= 80:
            checks["cpu_temp"] = "critical (%.1f°C)" % temp
            ok = False
        elif temp >= 70:
            checks["cpu_temp"] = "warning (%.1f°C)" % temp
        else:
            checks["cpu_temp"] = "ok (%.1f°C)" % temp

        status_code = 200 if ok else 503

        from app import APP_VERSION, _SERVICE_START
        from modules.devices_routes import get_scan_status

        try:
            with open("/proc/uptime") as f:
                host_uptime = round(float(f.read().split()[0]), 1)
        except Exception:
            host_uptime = None

        scan_st = get_scan_status()
        caps = current_app.config.get("CAPABILITIES") or {}

        return jsonify({
            "ok": ok,
            "checks": checks,
            "timestamp": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "version": APP_VERSION,
            "uptime": {
                "host_sec": host_uptime,
                "service_sec": round(time.time() - _SERVICE_START, 1),
            },
            "last_discovery": {
                "scan": scan_st["last_scan"],
                "ok": scan_st["last_ok"],
                "error": scan_st["last_error"],
                "errors": scan_st["errors"],
                "interval_sec": scan_st["interval_sec"],
                "scan_enabled": scan_st["scan_enabled"],
                "thread_alive": scan_st["thread_alive"],
            },
            "db": db_status,
            "capabilities": caps,
            "missing_deps": [n for n, ok_flag in caps.items() if not ok_flag],
            "platform": detect_platform(),
        }), status_code

    @app.route("/system")
    @login_required
    def system():

        global _boot_device
        if _boot_device is None:
            boot_device = "—"
            try:
                root_dev = _cmd(["findmnt", "-n", "-o", "SOURCE", "/"], timeout=5)
                if root_dev:
                    m = re.search(r'/dev/(\S+)', root_dev)
                    if m:
                        dev = m.group(1)
                        bm = re.match(r"^(mmcblk\d+|sd[a-z])", dev)
                        kind = _block_kind(bm.group(1)) if bm else None
                        if kind == "SD":
                            boot_device = "SD-карта (/dev/" + dev + ")"
                        elif kind == "MMC":
                            boot_device = "eMMC (/dev/" + dev + ")"
                        elif kind == "HDD":
                            boot_device = "HDD (/dev/" + dev + ")"
                        else:
                            boot_device = "/dev/" + dev
            except Exception:
                pass
            _boot_device = boot_device
        boot_device = _boot_device

        backup_file = "/srv/backup-system/emmc.img.zst"

        def _get_backup_state():
            return service_state("backup-emmc.service")

        def _get_iptv_state():
            return service_state("update-iptv.service")

        def _journal_backup():
            return service_last_log("backup-emmc.service", "=== eMMC BACKUP OK:")

        def _journal_iptv():
            return iptv_update_status()

        def _backup_test():
            return backup_test_status()

        def _timer_backup():
            return timer_next_run("backup-emmc.timer")

        def _timer_db():
            return timer_next_run("backup-db.timer")

        def _timer_iptv():
            return timer_next_run("update-iptv.timer")

        with ThreadPoolExecutor(max_workers=8) as ex:
            f_bstate = ex.submit(_get_backup_state)
            f_iptvstate = ex.submit(_get_iptv_state)
            f_log = ex.submit(_journal_backup)
            f_iptv = ex.submit(_journal_iptv)
            f_test = ex.submit(_backup_test)
            f_t_backup = ex.submit(_timer_backup)
            f_t_db = ex.submit(_timer_db)
            f_t_iptv = ex.submit(_timer_iptv)

        backup_state = f_bstate.result()
        iptv_state = f_iptvstate.result()
        backup_log = f_log.result()
        iptv_status, iptv_time = f_iptv.result()
        iptv_time = _format_dt(iptv_time)
        backup_test_state, backup_test_time = f_test.result()
        backup_test_time = _format_dt(backup_test_time)
        backup_next = f_t_backup.result()
        db_next = f_t_db.result()
        iptv_next = f_t_iptv.result()

        backup_file_state = backup_file_status(backup_file)

        backup_time = None

        if backup_log:
            raw_time = backup_log.split(
                "=== eMMC BACKUP OK:",
                1
            )[-1].strip().rstrip("=").strip()
            backup_time = _format_dt(raw_time)
        elif backup_file_state == "ok":
            try:
                mtime = os.path.getmtime(backup_file)
                backup_time = datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")
            except Exception:
                pass

        playlists = []
        update_status = load_iptv_update_status()

        for index, item in enumerate(load_iptv_playlists()):

            info = playlist_info(item)
            update_info = update_status.get(str(index), {})

            playlists.append({
                "name": item.get("name", "Без названия"),
                "url": item.get("url", ""),
                "file": item.get("file", ""),
                "enabled": bool(item.get("enabled", True)),
                "size": info["size"],
                "mtime": info["mtime"],
                "update_status": update_info.get("status", "never"),
                "update_time": _format_dt(update_info.get("time")),
                "update_exit_code": update_info.get("exit_code")
            })

        if _disk_sizes["hdd"] is None:
            _hdd_dev = hdd_device()
            if _hdd_dev:
                hdd_size_out = _cmd(["lsblk", "-dpo", "SIZE", "/dev/" + _hdd_dev], timeout=5)
                hdd_size_lines = hdd_size_out.strip().splitlines()
                _disk_sizes["hdd"] = hdd_size_lines[-1].strip() if hdd_size_lines else "?"
            else:
                _disk_sizes["hdd"] = "?"
        hdd_size = _disk_sizes["hdd"]

        sd_dev = None
        try:
            for _name in os.listdir("/sys/block"):
                if not _name.startswith("mmcblk") or "boot" in _name:
                    continue
                try:
                    with open(f"/sys/block/{_name}/device/type", "r", encoding="utf-8") as _f:
                        if _f.read().strip() == "SD":
                            sd_dev = _name
                except Exception:
                    pass
        except Exception:
            pass

        if _disk_sizes["sd"] is None and sd_dev and os.path.exists("/dev/" + sd_dev):
            sd_size_out = _cmd(["lsblk", "-dno", "SIZE", "/dev/" + sd_dev], timeout=5)
            sd_size_lines = sd_size_out.strip().splitlines()
            _disk_sizes["sd"] = sd_size_lines[-1].strip() if sd_size_lines else None
        sd_size = _disk_sizes["sd"]

        network_hosts = load_network_config().get("hosts", _cfg("network", "default_hosts", ["google.com", "ya.ru", "192.168.3.1"]))

        emmc_backup_allowed, emmc_backup_reason = emmc_backup_guard()

        current_disk, current_disk_label, current_disk_size = fs_disk("/srv")
        clone_allowed, clone_reason = clone_guard()

        return render_template("system.html",

            boot_device=boot_device,
            hdd_size=hdd_size,
            sd_size=sd_size,
            network_hosts=network_hosts,
            emmc_backup_allowed=emmc_backup_allowed,
            emmc_backup_reason=emmc_backup_reason,
            current_disk=current_disk,
            current_disk_label=current_disk_label,
            current_disk_size=current_disk_size,
            clone_allowed=clone_allowed,
            clone_reason=clone_reason,
            backup_running=(backup_state == "active"),
            backup_status=(
                "running"
                if backup_state == "active"
                else (
                    "ok"
                    if (backup_log and backup_file_state == "ok")
                    or backup_file_state == "ok"
                    else "error"
                )
            ),
            backup_time=backup_time,
            backup_size=file_info(backup_file),
            backup_next=backup_next,

            backup_test_state=backup_test_state,
            backup_test_time=backup_test_time,

            db_backups=db_backup_list(),
            db_backup_last=db_backup_last,
            db_backup_running=db_backup_running,
            db_backup_size_human=db_backup_size_human,
            db_backup_next=db_next,

            iptv_running=(iptv_state == "active"),
            iptv_status=iptv_status,
            iptv_time=iptv_time,
            playlists=playlists,
            iptv_next=iptv_next,

            **page_data()
        )

    @app.route("/system/backup", methods=["POST"])
    @admin_required
    @login_required
    def system_backup():
        allowed, reason = emmc_backup_guard()
        if not allowed:
            return jsonify({"ok": False, "reason": reason}), 409

        subprocess.Popen(
            ["systemctl", "start", "backup-emmc.service"]
        )

        return jsonify({"ok": True})

    @app.route("/system/backup-test", methods=["POST"])
    @admin_required
    @login_required
    def system_backup_test():

        if service_state("backup-emmc-test.service") == "active":
            return redirect(url_for("system"))

        subprocess.Popen(
            [
                "systemd-run",
                "--unit=backup-emmc-test",
                "--property=Type=oneshot",
                "/usr/bin/zstd",
                "-t",
                "/srv/backup-system/emmc.img.zst"
            ]
        )

        return redirect(url_for("system"))

    @app.route("/system/db-backup", methods=["POST"])
    @admin_required
    @login_required
    def system_db_backup():

        subprocess.Popen(
            ["systemctl", "start", "backup-db.service"]
        )

        return jsonify({"ok": True})

    @app.route("/system/db-restore", methods=["POST"])
    @admin_required
    @login_required
    def system_db_restore():
        if not _check_rate("db_restore", 60):
            return jsonify({"ok": False, "error": "too fast, wait 60s"}), 429

        data = request.get_json(silent=True) or {}
        filename = data.get("file", "")

        if not filename or "/" in filename or ".." in filename:
            return jsonify({"ok": False, "error": "Недопустимое имя файла"})

        backup_path = os.path.join(DB_BACKUP_DIR, filename)
        if not os.path.isfile(backup_path):
            return jsonify({"ok": False, "error": "Файл не найден"})

        try:
            result = subprocess.run(
                ["sqlite3", backup_path, "PRAGMA integrity_check;"],
                capture_output=True, text=True, timeout=10
            )
            if "ok" not in result.stdout:
                return jsonify({"ok": False, "error": "Backup повреждён"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

        try:
            subprocess.run(["systemctl", "stop", "lan-discovery"], timeout=15)
            time.sleep(1)
            shutil.copy2(backup_path, DB)
            subprocess.Popen(["systemctl", "start", "lan-discovery"])
            return jsonify({"ok": True})
        except Exception as e:
            subprocess.Popen(["systemctl", "start", "lan-discovery"])
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/system/emmc-restore", methods=["POST"])
    @admin_required
    @login_required
    def system_emmc_restore():

        subprocess.Popen(
            ["systemctl", "start", "backup-emmc-restore.service"]
        )

        return jsonify({"ok": True})

    @app.route("/api/backup-status")
    @login_required
    def api_backup_status():

        emmc_allowed, emmc_reason = emmc_backup_guard()

        return jsonify({
            "emmc_running": service_state("backup-emmc.service") in ("active", "activating"),
            "emmc_ok": backup_file_status("/srv/backup-system/emmc.img.zst") == "ok",
            "emmc_allowed": emmc_allowed,
            "emmc_reason": emmc_reason,
            "db_running": db_backup_running(),
            "db_ok": db_backup_last() is not None,
            "db_restore_running": db_restore_running(),
            "emmc_restore_running": emmc_restore_running(),
        })

    @app.route("/about")
    @login_required
    def about():
        page = page_data()
        page["about"] = about_data()

        return render_template("about.html",
            **page
        )

    @app.route("/api/disk/check")
    @login_required
    def api_disk_check():
        try:
            out = _cmd(["python3", DISK_REPLACE_SCRIPT, "check"], timeout=10)
            return jsonify(json.loads(out))
        except Exception as e:
            return jsonify({"found": False, "error": str(e)})

    @app.route("/api/disk/info")
    @login_required
    def api_disk_info():
        try:
            out = _cmd(["python3", DISK_REPLACE_SCRIPT, "info"], timeout=10)
            return jsonify(json.loads(out))
        except Exception as e:
            return jsonify({"error": str(e)})

    @app.route("/api/disk/prepare", methods=["POST"])
    @admin_required
    @login_required
    def api_disk_prepare():
        if not _check_rate("disk_prepare", 300):
            return jsonify({"error": "too fast, wait 5 min"}), 429
        data = request.get_json() or {}
        target = data.get("target", "")
        if not target:
            return jsonify({"error": "no target"}), 400
        try:
            out = _cmd(["python3", DISK_REPLACE_SCRIPT, "prepare", target], timeout=60)
            return jsonify(json.loads(out))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/disk/transfer", methods=["POST"])
    @admin_required
    @login_required
    def api_disk_transfer():
        if not _check_rate("disk_transfer", 600):
            return jsonify({"error": "too fast, wait 10 min"}), 429
        data = request.get_json() or {}
        target = data.get("target", "")
        if not target:
            return jsonify({"error": "no target"}), 400
        try:
            out = _cmd(["python3", DISK_REPLACE_SCRIPT, "transfer", target], timeout=7200)
            return jsonify(json.loads(out))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/disk/verify", methods=["POST"])
    @admin_required
    @login_required
    def api_disk_verify():
        data = request.get_json() or {}
        target = data.get("target", "")
        if not target:
            return jsonify({"error": "no target"}), 400
        try:
            out = _cmd(["python3", DISK_REPLACE_SCRIPT, "verify", target], timeout=300)
            return jsonify(json.loads(out))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/disk/poweroff", methods=["POST"])
    @admin_required
    @login_required
    def api_disk_poweroff():
        if not _check_rate("poweroff", 60):
            return jsonify({"error": "too fast, wait 60s"}), 429
        try:
            def do_off():
                time.sleep(2)
                _cmd(["shutdown", "-h", "now"], timeout=5)
            t = threading.Thread(target=do_off, daemon=True)
            t.start()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/reboot", methods=["POST"])
    @admin_required
    @login_required
    def api_reboot():
        if not _check_rate("reboot", 60):
            return jsonify({"error": "too fast, wait 60s"}), 429
        try:
            def do_reboot():
                time.sleep(2)
                _cmd(["shutdown", "-r", "now"], timeout=5)
            t = threading.Thread(target=do_reboot, daemon=True)
            t.start()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/clone/start", methods=["POST"])
    @admin_required
    @login_required
    def api_clone_start():
        state = _load_clone_state()
        if state.get("running"):
            return jsonify({"ok": True, "running": True})

        allowed, reason = clone_guard()
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 409

        if not _check_rate("clone", 3600):
            return jsonify({"error": "too fast, wait 1 hour"}), 429
        sd_name = find_typed_block("SD")
        emmc_name = find_typed_block("MMC")
        if not sd_name or not os.path.exists("/dev/" + sd_name):
            return jsonify({"error": "SD карта не обнаружена"})
        if not emmc_name or not os.path.exists("/dev/" + emmc_name):
            return jsonify({"error": "eMMC не обнаружена"})
        sd_dev = "/dev/" + sd_name
        _update_clone_state(running=True, ok=False, error=None, percent=0, started=time.time(), text="Подготовка...")
        def do_clone():
            try:
                _cmd(["sync"], timeout=10)
                _update_clone_state(percent=5, text="Копирование eMMC...")
                cmd = ["dd", "if=/dev/" + emmc_name, "of=" + sd_dev, "bs=4M", "status=progress"]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                watcher = threading.Thread(
                    target=_dd_progress_watcher,
                    args=(proc,),
                    daemon=True
                )
                watcher.start()
                _, stderr = proc.communicate(timeout=3600)
                watcher.join(timeout=10)
                if proc.returncode == 0:
                    _cmd(["sync"], timeout=30)
                    _update_clone_state(running=False, ok=True, percent=100, text="Готово!")
                else:
                    _update_clone_state(running=False, error=stderr[:200] if stderr else "Ошибка dd")
            except Exception as e:
                _update_clone_state(running=False, error=str(e))
        t = threading.Thread(target=do_clone, daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/sd-info")
    @login_required
    def api_sd_info():
        result = {"found": False, "mountpoint": None, "used": None, "total": None}
        sd_name = find_typed_block("SD")
        for dev in (["/dev/" + sd_name] if sd_name else []):
            if not os.path.exists(dev):
                continue
            result["found"] = True
            result["device"] = dev
            try:
                out = _cmd(["lsblk", "-dno", "SIZE,MODEL,FSTYPE", dev], timeout=5)
                parts = out.split()
                if len(parts) >= 1:
                    result["size"] = parts[0]
                if len(parts) >= 2:
                    result["model"] = parts[1]
                if len(parts) >= 3:
                    result["fs"] = parts[2]
            except Exception:
                pass
            for mount_base in ("/media", "/mnt"):
                try:
                    for entry in os.listdir(mount_base):
                        mount = os.path.join(mount_base, entry)
                        if os.path.ismount(mount) and dev in _cmd(["findmnt", "-n", "-o", "SOURCE", mount], timeout=3):
                            stat = os.statvfs(mount)
                            result["mountpoint"] = mount
                            result["total"] = stat.f_blocks * stat.f_frsize
                            result["used"] = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
                            break
                except Exception:
                    pass
                if result.get("mountpoint"):
                    break
            break
        return jsonify(result)

    @app.route("/api/clone/status")
    @login_required
    def api_clone_status():
        return jsonify(_load_clone_state())

    @app.route("/apps/disks")
    @login_required
    def app_disks():
        return render_template("apps/disks.html", **page_data())

    @app.route("/api/disks")
    @login_required
    def api_disks():
        try:
            lsblk = subprocess.run(
                ["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL"],
                capture_output=True, text=True, timeout=10
            ).stdout
            df = subprocess.run(
                ["df", "-h"],
                capture_output=True, text=True, timeout=10
            ).stdout
            smart = ""
            _smart_dev = hdd_device()
            if not _smart_dev:
                smart = "диск не обнаружен"
            else:
                try:
                    r = subprocess.run(
                        ["smartctl", "-a", "/dev/" + _smart_dev],
                        capture_output=True, text=True, timeout=10
                    )
                    smart = r.stdout or r.stderr
                except Exception:
                    smart = "smartctl не установлен"
            return {"ok": True, "lsblk": lsblk, "df": df, "smart": smart}
        except Exception as e:
            return {"ok": False, "error": str(e)}
