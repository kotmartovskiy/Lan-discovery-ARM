import os
import json
import time
import subprocess
import threading
import shutil
import socket
import struct
import select
from pathlib import Path
from datetime import datetime


# ==================== Cache dicts ====================

_settings_cache = {"data": None, "ts": 0}
_iptv_update_status_cache = {"data": None, "ts": 0}
_rate_limits = {}

IPTV_UPDATE_STATUS = "/etc/lan-discovery/iptv-update-status.json"
NETWORK_CONFIG = "/etc/lan-discovery/network.json"
NOTES_DIR = "/etc/lan-discovery/notes"
SECRETS_DIR = "/etc/lan-discovery/secrets"
SECRETS_KEY_PATH = "/etc/lan-discovery/secret.key"
TRANSMISSION_URL = "http://localhost:9091/transmission/rpc"
TRANSMISSION_USER = ""
TRANSMISSION_PASS = ""
FILEMANAGER_ROOT = "/"

os.makedirs(NOTES_DIR, exist_ok=True)
os.makedirs(SECRETS_DIR, exist_ok=True)


# ==================== Core utility functions ====================

def load_settings():
    from app import SETTINGS_PATH
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


def save_settings(data):
    from app import SETTINGS_PATH
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _settings_cache["data"] = data
        _settings_cache["ts"] = time.time()
        return True
    except Exception:
        return False


def _check_rate(action, cooldown):
    now = time.time()
    last = _rate_limits.get(action, 0)
    if now - last < cooldown:
        return False
    _rate_limits[action] = now
    return True


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


# ==================== IPTV update status ====================

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


def save_iptv_update_status(data):
    path = Path(IPTV_UPDATE_STATUS)

    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=4
        )
        f.write("\n")

    tmp.replace(path)


# ==================== User helpers ====================
# Единственный источник — modules.auth (P0-5: enabled/TTL/bcrypt-lazy-rehash).
# Дубликаты удалены, чтобы контекст-процессор и API не расходились с decorators.

from modules.auth import (  # noqa: F401
    _hash,
    _verify_hash,
    get_current_username,
    get_current_user,
    load_users,
    save_users,
)


# ==================== Context processor ====================

def _inject_user():
    from types import SimpleNamespace
    u = get_current_user()
    if not u:
        u = SimpleNamespace(username="", role="guest", enabled=False, display_name="Гость")
    return {"current_user": u, "current_username": get_current_username()}


# ==================== Currency / recycling helpers ====================

_currency_cache = {"data": None, "recycling": None, "ts": 0, "updating": False}


def _refresh_currency_cache():
    if _currency_cache["updating"]:
        return
    _currency_cache["updating"] = True
    def _do():
        try:
            from modules.currencies import get_latest as get_currency_latest_fn
            from modules.recycling import get_latest as get_recycling_latest_fn
            d = get_currency_latest_fn()
            r = get_recycling_latest_fn()
            _currency_cache["data"] = d
            _currency_cache["recycling"] = r
        except Exception:
            pass
        _currency_cache["updating"] = False
    threading.Thread(target=_do, daemon=True).start()


def get_currency_cached():
    now = time.time()
    if now - _currency_cache["ts"] < 60 and _currency_cache["data"] is not None:
        return _currency_cache["data"], _currency_cache["recycling"]
    _refresh_currency_cache()
    return _currency_cache["data"] or {}, _currency_cache["recycling"] or {}


# ==================== Internet / weather / page_data ====================

def page_data():
    """Делегируется app.page_data — единый источник (P1-8: нет дублей)."""
    from app import page_data as _app_page_data
    return _app_page_data()


def _help_facts():
    """Данные «эта панель» + накопители для templates/help.html.

    Справка генерируется по фактическим данным хоста (device-tree, ip,
    lsblk, settings), а не зашита под X96 Max — корректна на X96,
    Orange Pi и generic Debian.
    """
    import re as _re
    from modules.system_routes import (
        about_data, board_title, _block_kind, _root_mount_source,
        find_typed_block,
    )

    about = about_data()

    ipv4 = []
    for iface in about.get("network_physical", []):
        for a in iface.get("addresses", []):
            addr = a.get("address", "")
            if a.get("family") == "inet" and addr \
                    and not addr.startswith("127."):
                ipv4.append(addr)
    primary = next((ip for ip in ipv4 if ip.startswith("192.168.")),
                   ipv4[0] if ipv4 else "127.0.0.1")

    settings = load_settings()
    port = (settings.get("web") or {}).get("flask_port", 8080)
    subnet = (settings.get("network") or {}).get("subnet", "192.168.3.0/24")
    w = settings.get("weather") or {}
    lat = float(w.get("latitude", 57.0))
    lon = float(w.get("longitude", 41.0))
    coords = "%.1f\u00b0%s, %.1f\u00b0%s" % (
        abs(lat), "N" if lat >= 0 else "S",
        abs(lon), "E" if lon >= 0 else "W",
    )

    root_src = _root_mount_source()
    m = _re.match(r"(mmcblk\d+|sd[a-z])", root_src.rsplit("/", 1)[-1])
    root_disk = m.group(1) if m else ""
    root_kind = _block_kind(root_disk) if root_disk else None
    kind_labels = {"SD": "SD", "MMC": "eMMC", "HDD": "HDD"}
    root_kind_label = kind_labels.get(root_kind, root_kind or "\u2014")

    def _kind_label(name, rotational):
        k = _block_kind(name)
        if k in kind_labels:
            return kind_labels[k]
        return "HDD" if rotational else "SSD"

    disks = []
    for d in about.get("disks", []):
        if d.get("type") != "disk":
            continue
        name = d["name"]
        if not _re.match(r"^(mmcblk\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)$",
                         name):
            continue
        parts = [p for p in about.get("disks", [])
                 if p.get("type") == "part" and p["name"].startswith(name)]
        mounts = list(d.get("mountpoints") or [])
        for p in parts:
            mounts.extend(p.get("mountpoints") or [])
        if name == root_disk:
            role = "системный диск (панель, данные)"
        elif not mounts:
            role = "пустой (не используется)"
        else:
            role = "данные"
        disks.append({
            "dev": "/dev/" + name,
            "kind": _kind_label(name, d.get("rotational")),
            "size": d.get("size", "\u2014"),
            "mounts": ", ".join(mounts) or "\u2014",
            "role": role,
        })

    lan = [
        {"title": "X96 Max (панель)", "ip": "192.168.3.243",
         "desc": "Панель X96 Max (основной сервер)"},
        {"title": "X96 Max (WiFi)", "ip": "192.168.3.244",
         "desc": "WiFi-интерфейс X96 Max"},
        {"title": "Orange Pi (LAN)", "ip": "192.168.3.234",
         "desc": "Удалённый сервер (кабель отвален \u2014 не отвечает)"},
        {"title": "Orange Pi (WiFi AP)", "ip": "192.168.3.235",
         "desc": "Доступ к Orange Pi через его WiFi AP"},
        {"title": "ThinkPad T480", "ip": "192.168.3.236",
         "display": "192.168.3.236 / .239", "desc": "Рабочая станция"},
        {"title": "Роутер", "ip": "192.168.3.1",
         "desc": "Шлюз, веб-интерфейс"},
        {"title": "DNS-сервер", "ip": "192.168.3.51",
         "desc": "Локальный DNS"},
    ]
    for n in lan:
        n["here"] = (n["ip"] == primary)
        n.setdefault("display", n["ip"])

    sd_dev = find_typed_block("SD")
    emmc_dev = find_typed_block("MMC")

    from core.module_loader import discover_modules, module_status
    enabled_ids = set()
    for m in discover_modules():
        installed, on = module_status(m["id"])
        if installed and on:
            enabled_ids.add(m["id"])

    return {
        "hf": {
            "board": board_title(),
            "hostname": about.get("os", {}).get("hostname", ""),
            "os": about.get("os", {}).get("pretty_name", ""),
            "primary_ip": primary,
            "all_ips": ipv4,
            "port": port,
            "subnet": subnet,
            "panel_url": "http://%s:%s" % (primary, port),
            "restore_url": "http://%s:8081" % primary,
            "coords": coords,
            "lat": lat,
            "lon": lon,
            "region": w.get("region_name", "Иваново"),
            "disks": disks,
            "lan": lan,
            "root_src": root_src or "\u2014",
            "root_kind": root_kind or "",
            "root_kind_label": root_kind_label,
            "sd_dev": sd_dev or "",
            "emmc_dev": emmc_dev or "",
            "enabled": enabled_ids,
        }
    }


# ==================== Routes ====================

def register_routes(app):
    from flask import render_template, request, redirect, url_for, jsonify, send_file
    from app import login_required, admin_required, can_edit, GAMES_DIR

    # --- Games ---

    @app.route("/games/<path:filename>")
    @login_required
    def serve_game(filename):
        safe = os.path.normpath(filename)
        if safe.startswith("..") or os.path.isabs(safe):
            return "Forbidden", 403
        filepath = os.path.join(GAMES_DIR, safe)
        if not os.path.isfile(filepath):
            return "Not found", 404
        return send_file(filepath, mimetype="text/html")

    # --- Apps page ---

    @app.route("/apps")
    @login_required
    def apps_page():
        return render_template("apps.html", **page_data())

    # --- App pages ---

    @app.route("/torrent")
    @login_required
    def torrent_page():
        return render_template("torrent.html", **page_data())

    @app.route("/apps/notes")
    @login_required
    def app_notes():
        return render_template("apps/notes.html", **page_data())

    @app.route("/apps/passwords")
    @login_required
    def app_passwords():
        return render_template("apps/passwords.html", **page_data())

    @app.route("/apps/filemanager")
    @login_required
    def app_filemanager():
        return render_template("apps/filemanager.html", **page_data())

    @app.route("/apps/terminal")
    @login_required
    def app_terminal():
        return render_template("apps/terminal.html", **page_data())

    # --- Help ---

    @app.route("/help")
    @login_required
    def help_page():
        data = dict(page_data())
        data.update(_help_facts())

        return render_template("help.html",
            **data
        )

    # --- Currencies page ---

    @app.route("/currencies")
    @login_required
    def currencies():
        data = page_data()

        currency_data, recycling_data = get_currency_cached()

        currency_updated = ""
        if currency_data:
            for cat_items in currency_data.values():
                if cat_items:
                    currency_updated = cat_items[0].get("fetched_at", "")
                    break

        data["currency_data"] = currency_data
        data["recycling_data"] = recycling_data
        data["currency_updated"] = currency_updated

        return render_template("currencies.html",
            **data
        )

    @app.route("/api/currencies")
    @login_required
    def api_currencies():
        from modules.currencies import get_latest as get_currency_latest
        return jsonify({"usd": get_currency_latest("usd") or {}, "eur": get_currency_latest("eur") or {}})

    # --- Settings ---

    @app.route("/api/settings")
    @login_required
    @admin_required
    def api_settings_get():
        return jsonify(load_settings())

    @app.route("/api/settings", methods=["POST"])
    @admin_required
    @login_required
    def api_settings_save():
        data = request.get_json() or {}
        if not data:
            return jsonify({"error": "empty"}), 400
        old = load_settings()
        old.update(data)
        if save_settings(old):
            _settings_cache["ts"] = 0
            return jsonify({"ok": True})
        return jsonify({"error": "save failed"}), 500

    # --- Users ---

    @app.route("/api/users")
    @admin_required
    def api_users_list():
        users = load_users()
        safe = {}
        for name, u in users.items():
            safe[name] = {
                "role": u.get("role"),
                "enabled": u.get("enabled"),
                "display_name": u.get("display_name", name)
            }
        return jsonify(safe)

    @app.route("/api/users/<username>/password", methods=["POST"])
    @admin_required
    def api_user_password(username):
        users = load_users()
        if username not in users:
            return jsonify({"error": "not found"}), 404
        data = request.get_json() or {}
        pw = data.get("password", "").strip()
        if len(pw) < 8:
            return jsonify({"error": "password too short (min 8)"}), 400
        users[username]["password_hash"] = _hash(pw)
        save_users(users)
        return jsonify({"ok": True})

    @app.route("/api/users/<username>/toggle", methods=["POST"])
    @admin_required
    def api_user_toggle(username):
        users = load_users()
        if username not in users:
            return jsonify({"error": "not found"}), 404
        if users[username].get("role") == "admin":
            return jsonify({"error": "cannot disable admin"}), 400
        users[username]["enabled"] = not users[username].get("enabled", True)
        save_users(users)
        return jsonify({"ok": True, "enabled": users[username]["enabled"]})

    # --- Notes ---

    @app.route("/api/notes", methods=["GET"])
    @login_required
    def api_notes_list():
        idx = _notes_index()
        return {"ok": True, "notes": idx}

    @app.route("/api/notes", methods=["POST"])
    @login_required
    def api_notes_create():
        data = request.json
        title = data.get("title", "Без названия")
        content = data.get("content", "")
        idx = _notes_index()
        note_id = max([n["id"] for n in idx], default=0) + 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note = {"id": note_id, "title": title, "content": content, "created": now, "updated": now}
        idx.append(note)
        _notes_save_index(idx)
        with open(os.path.join(NOTES_DIR, "%d.md" % note_id), "w") as f:
            f.write(content)
        return {"ok": True, "id": note_id}

    @app.route("/api/notes/<int:note_id>", methods=["PUT"])
    @login_required
    def api_notes_update(note_id):
        data = request.json
        idx = _notes_index()
        for n in idx:
            if n["id"] == note_id:
                n["title"] = data.get("title", n["title"])
                n["content"] = data.get("content", n["content"])
                n["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _notes_save_index(idx)
                with open(os.path.join(NOTES_DIR, "%d.md" % note_id), "w") as f:
                    f.write(n["content"])
                return {"ok": True}
        return {"ok": False, "error": "Not found"}, 404

    @app.route("/api/notes/<int:note_id>", methods=["DELETE"])
    @login_required
    def api_notes_delete(note_id):
        idx = _notes_index()
        idx = [n for n in idx if n["id"] != note_id]
        _notes_save_index(idx)
        try:
            os.remove(os.path.join(NOTES_DIR, "%d.md" % note_id))
        except Exception:
            pass
        return {"ok": True}

    # --- Secrets / Passwords ---

    @app.route("/api/secrets", methods=["GET"])
    @login_required
    @can_edit
    def api_secrets_list():
        secrets = _load_secrets()
        return {"ok": True, "secrets": secrets}

    @app.route("/api/secrets", methods=["POST"])
    @login_required
    @can_edit
    def api_secrets_create():
        data = request.json
        secrets = _load_secrets()
        secret_id = max([s["id"] for s in secrets], default=0) + 1
        _enc_ids.discard(secret_id)
        secret = {
            "id": secret_id,
            "name": data.get("name", ""),
            "login": data.get("login", ""),
            "password": data.get("password", ""),
            "url": data.get("url", ""),
            "notes": data.get("notes", "")
        }
        secrets.append(secret)
        _save_secrets(secrets)
        return {"ok": True, "id": secret_id}

    @app.route("/api/secrets/<int:secret_id>", methods=["PUT"])
    @login_required
    @can_edit
    def api_secrets_update(secret_id):
        data = request.json
        secrets = _load_secrets()
        for s in secrets:
            if s["id"] == secret_id:
                s["name"] = data.get("name", s["name"])
                s["login"] = data.get("login", s["login"])
                if "password" in data:
                    s["password"] = data["password"]
                    _enc_ids.discard(secret_id)
                s["url"] = data.get("url", s["url"])
                s["notes"] = data.get("notes", s["notes"])
                _save_secrets(secrets)
                return {"ok": True}
        return {"ok": False, "error": "Not found"}, 404

    @app.route("/api/secrets/<int:secret_id>", methods=["DELETE"])
    @login_required
    @can_edit
    def api_secrets_delete(secret_id):
        secrets = _load_secrets()
        secrets = [s for s in secrets if s["id"] != secret_id]
        _save_secrets(secrets)
        return {"ok": True}

    # --- File Manager ---

    def _fm_path(raw):
        """Нормализация пути: строка, без null-байт, абсолютный, без '..'."""
        if not raw or not isinstance(raw, str) or "\x00" in raw:
            return None
        p = os.path.normpath(raw)
        if not os.path.isabs(p):
            return None
        return p

    @app.route("/api/filemanager/list")
    @admin_required
    def api_filemanager_list():
        path = _fm_path(request.args.get("path", "/"))
        if path is None:
            return {"ok": False, "error": "Invalid path"}, 400
        if not os.path.exists(path):
            return {"ok": False, "error": "Path not found"}, 404
        if not os.path.isdir(path):
            return {"ok": False, "error": "Not a directory"}, 400
        try:
            entries = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                    is_dir = os.path.isdir(full)
                    entries.append({
                        "name": name,
                        "is_dir": is_dir,
                        "size": st.st_size if not is_dir else 0,
                        "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "permissions": oct(st.st_mode)[-3:] if os.path.islink(full) else ""
                    })
                except Exception:
                    entries.append({
                        "name": name,
                        "is_dir": False,
                        "size": 0,
                        "modified": "",
                        "permissions": ""
                    })
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return {"ok": True, "path": path, "files": entries}
        except PermissionError:
            return {"ok": False, "error": "Permission denied"}, 403
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/read")
    @admin_required
    def api_filemanager_read():
        path = _fm_path(request.args.get("path", ""))
        if path is None:
            return {"ok": False, "error": "Invalid path"}, 400
        if not os.path.exists(path):
            return {"ok": False, "error": "File not found"}, 404
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read(512000)
            return {"ok": True, "content": content, "name": os.path.basename(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/mkdir", methods=["POST"])
    @admin_required
    def api_filemanager_mkdir():
        path = _fm_path(request.json.get("path", ""))
        if path is None:
            return {"ok": False, "error": "Invalid path"}, 400
        try:
            os.makedirs(path, exist_ok=True)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/delete", methods=["POST"])
    @admin_required
    def api_filemanager_delete():
        path = _fm_path(request.json.get("path", ""))
        if path is None:
            return {"ok": False, "error": "Invalid path"}, 400
        if path == "/":
            return {"ok": False, "error": "Refusing to delete /"}, 400
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/rename", methods=["POST"])
    @admin_required
    def api_filemanager_rename():
        data = request.json
        old_path = _fm_path(data.get("old_path", ""))
        new_path = _fm_path(data.get("new_path", ""))
        if old_path is None or new_path is None:
            return {"ok": False, "error": "Invalid path"}, 400
        try:
            os.rename(old_path, new_path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/copy", methods=["POST"])
    @admin_required
    def api_filemanager_copy():
        data = request.json
        src = _fm_path(data.get("src", ""))
        dst = _fm_path(data.get("dst", ""))
        if src is None or dst is None:
            return {"ok": False, "error": "Invalid path"}, 400
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/api/filemanager/move", methods=["POST"])
    @admin_required
    def api_filemanager_move():
        data = request.json
        src = _fm_path(data.get("src", ""))
        dst = _fm_path(data.get("dst", ""))
        if src is None or dst is None:
            return {"ok": False, "error": "Invalid path"}, 400
        if src == "/":
            return {"ok": False, "error": "Refusing to move /"}, 400
        try:
            shutil.move(src, dst)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ==================== Notes helpers ====================

def _notes_index():
    idx_path = os.path.join(NOTES_DIR, "index.json")
    if os.path.exists(idx_path):
        with open(idx_path, "r") as f:
            return json.load(f)
    return []


def _notes_save_index(idx):
    idx_path = os.path.join(NOTES_DIR, "index.json")
    with open(idx_path, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ==================== Secrets helpers ====================
# Сейф: Fernet (AES-128-CBC + HMAC-SHA256, authenticated encryption).
# Ключ — отдельный файл SECRETS_KEY_PATH (chmod 600), не в коде/БД.
# Старый формат base64(HMAC[:16] || plaintext): HMAC реально проверяется,
# запись мигрирует в Fernet при первом сохранении. Битые/нечитаемые записи
# (id в _enc_ids) пишутся обратно без изменений — без двойного шифрования.

_enc_ids = set()
_fernet_cache = {}


def _get_secrets_key():
    if os.path.exists(SECRETS_KEY_PATH):
        with open(SECRETS_KEY_PATH, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    key = os.urandom(32)
    os.makedirs(os.path.dirname(SECRETS_KEY_PATH), exist_ok=True)
    with open(SECRETS_KEY_PATH, "wb") as f:
        f.write(key)
    try:
        os.chmod(SECRETS_KEY_PATH, 0o600)
    except OSError:
        pass
    return key


def _secrets_fernet():
    from cryptography.fernet import Fernet
    raw = _get_secrets_key()
    hit = _fernet_cache.get(raw)
    if hit is not None:
        return hit
    import base64
    import hashlib
    if len(raw) == 32:
        key32 = raw
    else:
        key32 = None
        stripped = raw.strip()
        if len(stripped) == 64:
            try:
                key32 = bytes.fromhex(stripped.decode("ascii"))
            except Exception:
                key32 = None
        if key32 is None:
            key32 = hashlib.sha256(raw).digest()
    f = Fernet(base64.urlsafe_b64encode(key32))
    _fernet_cache[raw] = f
    return f


def _secrets_encrypt(text):
    return _secrets_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def _secrets_decrypt(data):
    try:
        return _secrets_fernet().decrypt(data.encode("ascii")).decode("utf-8")
    except Exception:
        pass
    from base64 import b64decode
    from hashlib import sha256
    from hmac import HMAC
    try:
        raw = b64decode(data, validate=True)
    except Exception:
        raise ValueError("secrets: corrupt entry")
    if len(raw) < 16:
        raise ValueError("secrets: corrupt entry")
    body = raw[16:]
    if HMAC(_get_secrets_key(), body, sha256).digest()[:16] != raw[:16]:
        raise ValueError("secrets: hmac mismatch")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("secrets: corrupt entry")


def _secrets_file():
    return os.path.join(SECRETS_DIR, "secrets.json")


def _load_secrets():
    global _enc_ids
    _enc_ids = set()
    path = _secrets_file()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    need_migration = False
    for s in data:
        pw = s.get("password")
        if not pw:
            continue
        try:
            s["password"] = _secrets_decrypt(pw)
            if not pw.startswith("gAAAA"):
                need_migration = True
        except Exception:
            _enc_ids.add(s.get("id"))
    if need_migration:
        backup = path + ".backup-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        _save_secrets(data)
    return data


def _save_secrets(secrets):
    path = _secrets_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = []
    for s in secrets:
        s2 = dict(s)
        if s2.get("password") and s2.get("id") not in _enc_ids:
            s2["password"] = _secrets_encrypt(s2["password"])
        out.append(s2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ==================== Terminal / SocketIO ====================

_terminal_sessions = {}


def _socket_is_admin():
    """Текущий socketio-сеанс — включённый администратор (см. modules/auth)."""
    try:
        from modules.auth import get_current_user
        u = get_current_user()
        return bool(u and u.role == "admin" and getattr(u, "enabled", False))
    except Exception:
        return False


def _terminal_drop(sid):
    """Закрыть терминальную сессию (kill процесса + fd), если она есть."""
    sess = _terminal_sessions.pop(sid, None)
    if not sess:
        return
    try:
        os.kill(sess["pid"], 9)
    except Exception:
        pass
    try:
        os.close(sess["fd"])
    except Exception:
        pass


def register_socketio_handlers(socketio):
    from flask import request

    @socketio.on("connect")
    def terminal_connect():
        return _socket_is_admin()

    @socketio.on("terminal_input")
    def terminal_input(data):
        sid = request.sid
        if not _socket_is_admin():
            _terminal_drop(sid)
            return
        if sid not in _terminal_sessions:
            return
        fd = _terminal_sessions[sid]["fd"]
        try:
            os.write(fd, data.encode())
        except Exception:
            pass

    @socketio.on("terminal_resize")
    def terminal_resize(data):
        sid = request.sid
        if not _socket_is_admin():
            _terminal_drop(sid)
            return
        if sid not in _terminal_sessions:
            return
        fd = _terminal_sessions[sid]["fd"]
        try:
            import fcntl, termios
            winsize = struct.pack("HHHH", data.get("rows", 24), data.get("cols", 80), 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    @socketio.on("terminal_start")
    def terminal_start(data=None):
        import pty, fcntl, termios
        sid = request.sid

        if not _socket_is_admin():
            return

        if sid in _terminal_sessions:
            return

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(slave_fd)
            os.environ["TERM"] = "xterm-256color"
            os.execvp("/bin/bash", ["/bin/bash", "--login"])
        else:
            os.close(slave_fd)
            _terminal_sessions[sid] = {"fd": master_fd, "pid": pid}

            def read_output():
                while sid in _terminal_sessions:
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if r:
                            data = os.read(master_fd, 4096)
                            if data:
                                socketio.emit("terminal_output", data.decode("utf-8", errors="replace"), room=sid)
                            else:
                                break
                    except Exception:
                        break
                if sid in _terminal_sessions:
                    socketio.emit("terminal_output", "\r\n\x1b[31mСессия завершена\x1b[0m\r\n", room=sid)

            threading.Thread(target=read_output, daemon=True).start()

    @socketio.on("terminal_stop")
    def terminal_stop():
        _terminal_drop(request.sid)

    @socketio.on("disconnect")
    def terminal_disconnect():
        _terminal_drop(request.sid)
