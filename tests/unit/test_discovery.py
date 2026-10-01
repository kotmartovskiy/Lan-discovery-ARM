# -*- coding: utf-8 -*-
"""Unit: discovery engine (PHASE 6): parse_scan, run_scan, reconcile."""
import sqlite3
import threading
from types import SimpleNamespace

from core import discovery as d

NMAP_OUT = """Nmap scan report for router.lan (192.168.3.1)
Host is up (0.0031s latency).
MAC Address: AA:BB:CC:DD:EE:FF (Cisco Systems)
Nmap scan report for 192.168.3.50
Host is up.
MAC Address: 11:22:33:44:55:66 (Unknown vendor)
Nmap scan report for 192.168.3.77
Host is up.
"""


def test_parse_scan_basic():
    devs = d.parse_scan(NMAP_OUT)
    assert set(devs) == {"192.168.3.1", "192.168.3.50", "192.168.3.77"}
    assert devs["192.168.3.1"]["hostname"] == "router.lan"
    assert devs["192.168.3.1"]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert devs["192.168.3.1"]["vendor"] == "Cisco Systems"
    assert devs["192.168.3.50"]["hostname"] is None
    assert devs["192.168.3.50"]["vendor"] == "Unknown vendor"
    assert devs["192.168.3.77"]["mac"] is None


def test_parse_scan_empty():
    assert d.parse_scan("") == {}
    assert d.parse_scan("Note: Host seems down.") == {}


def test_parse_scan_without_mac_section():
    devs = d.parse_scan("Nmap scan report for 10.0.0.1\nHost is up.\n")
    assert devs == {"10.0.0.1": {"hostname": None, "mac": None,
                                 "vendor": None}}


def _runner(stdout="", rc=0, exc=None):
    def run(cmd, **kwargs):
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=rc, stdout=stdout)
    return run


def test_run_scan_adds_self_ips(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _runner(
        stdout="Nmap scan report for 192.168.3.99\nHost is up.\n"))
    monkeypatch.setattr(d, "_scan_ifaces", lambda: ["eth0"])
    monkeypatch.setattr(d, "_subnet", lambda: "192.168.3.0/24")
    monkeypatch.setattr(d, "_self_ips", lambda: ["10.0.0.50"])
    out = d.run_scan()
    assert out is not None
    devs = d.parse_scan(out)
    assert "192.168.3.99" in devs
    assert "10.0.0.50" in devs


def test_run_scan_self_ip_not_duplicated(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _runner(
        stdout="Nmap scan report for 10.0.0.50\nHost is up.\n"))
    monkeypatch.setattr(d, "_scan_ifaces", lambda: ["eth0"])
    monkeypatch.setattr(d, "_subnet", lambda: "10.0.0.0/24")
    monkeypatch.setattr(d, "_self_ips", lambda: ["10.0.0.50"])
    out = d.run_scan()
    devs = d.parse_scan(out)
    assert out.count("Nmap scan report for 10.0.0.50") == 1


def test_run_scan_nmap_missing(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _runner(exc=FileNotFoundError()))
    monkeypatch.setattr(d, "_self_ips", lambda: [])
    assert d.run_scan(subnet="192.168.3.0/24", ifaces=["eth0"]) is None


def test_run_scan_all_ifaces_fail(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _runner(stdout="", rc=1))
    monkeypatch.setattr(d, "_self_ips", lambda: [])
    assert d.run_scan(subnet="192.168.3.0/24",
                      ifaces=["eth0", "wlan0"]) is None


def test_run_scan_empty_output(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _runner(stdout="   ", rc=0))
    monkeypatch.setattr(d, "_self_ips", lambda: ["10.0.0.1"])
    # только self_ips → хост есть → результат есть
    out = d.run_scan(subnet="192.168.3.0/24", ifaces=["eth0"])
    assert out is not None
    assert d.parse_scan(out) == {"10.0.0.1": {"hostname": None, "mac": None,
                                              "vendor": None}}


def _seed(db, ip, mac=None, online=1, misses=0, name=None):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO devices (ip, online, mac, first_seen, last_seen, "
        "appearances, misses, name) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (ip, online, mac, "01.01.2026 00:00:00", "01.01.2026 00:00:00",
         misses, name),
    )
    con.commit()
    con.close()


def _get(db, ip):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT online, mac, misses, appearances, is_new, name "
        "FROM devices WHERE ip=?", (ip,)).fetchone()
    con.close()
    return row


def test_reconcile_new_device(monkeypatch, devices_db, no_dns):
    monkeypatch.setattr(d, "_max_misses", lambda: 6)
    con = sqlite3.connect(devices_db)
    stats = d.reconcile(
        con,
        {"192.168.3.10": {"hostname": "pc", "mac": "AA:BB:CC:DD:EE:01",
                          "vendor": "V"}},
        now="01.02.2026 10:00:00")
    con.commit()
    assert stats == {"new": 1, "online": 0, "offline": 0, "mac_changed": 0}
    row = _get(devices_db, "192.168.3.10")
    assert row[0] == 1 and row[4] == 1 and row[3] == 1
    ev = con.execute("SELECT event, severity FROM events").fetchall()
    assert ev == [("NEW", "info")]
    con.close()


def test_reconcile_online_then_absent(monkeypatch, devices_db, no_dns):
    monkeypatch.setattr(d, "_max_misses", lambda: 6)
    con = sqlite3.connect(devices_db)
    info = {"hostname": "pc", "mac": "AA:BB:CC:DD:EE:01", "vendor": None}
    d.reconcile(con, {"192.168.3.10": info}, now="01.02.2026 10:00:00")
    con.commit()

    # повторное обнаружение: online-события нет, appearances растёт
    stats = d.reconcile(con, {"192.168.3.10": info},
                        now="01.02.2026 10:00:30")
    con.commit()
    assert stats["online"] == 0 and stats["new"] == 0
    row = _get(devices_db, "192.168.3.10")
    assert row[3] == 2 and row[0] == 1

    # пропуск: misses+1, ещё онлайн
    stats = d.reconcile(con, {}, now="01.02.2026 10:01:00")
    con.commit()
    assert stats["offline"] == 0
    assert _get(devices_db, "192.168.3.10")[2] == 1

    # 5 пропусков подряд → misses=6 → OFFLINE
    for i in range(5):
        d.reconcile(con, {}, now=f"01.02.2026 10:0{i + 2}:00")
    con.commit()
    row = _get(devices_db, "192.168.3.10")
    assert row[0] == 0 and row[2] == 6
    events = [r[0] for r in con.execute("SELECT event FROM events")]
    assert "OFFLINE" in events
    off = con.execute(
        "SELECT severity FROM events WHERE event='OFFLINE'").fetchone()
    assert off[0] == "warning"
    con.close()


def test_reconcile_online_event_after_offline(monkeypatch, devices_db,
                                              no_dns):
    monkeypatch.setattr(d, "_max_misses", lambda: 2)
    con = sqlite3.connect(devices_db)
    info = {"hostname": "pc", "mac": "AA:BB:CC:DD:EE:01", "vendor": None}
    d.reconcile(con, {"192.168.3.10": info}, now="01.02.2026 10:00:00")
    d.reconcile(con, {}, now="01.02.2026 10:01:00")
    d.reconcile(con, {}, now="01.02.2026 10:02:00")  # offline
    con.commit()
    stats = d.reconcile(con, {"192.168.3.10": info},
                        now="01.02.2026 10:03:00")
    con.commit()
    assert stats["online"] == 1
    events = [r[0] for r in con.execute(
        "SELECT event FROM events ORDER BY id")]
    assert events == ["NEW", "OFFLINE", "ONLINE"]
    con.close()


def test_reconcile_mac_changed(monkeypatch, devices_db, no_dns):
    monkeypatch.setattr(d, "_max_misses", lambda: 6)
    _seed(devices_db, "192.168.3.20", mac="AA:BB:CC:DD:EE:01", name="old")
    con = sqlite3.connect(devices_db)
    stats = d.reconcile(
        con,
        {"192.168.3.20": {"hostname": None, "mac": "AA:BB:CC:DD:EE:02",
                          "vendor": "V2"}},
        now="01.02.2026 10:00:00")
    con.commit()
    assert stats["mac_changed"] == 1
    row = _get(devices_db, "192.168.3.20")
    assert row[1] == "AA:BB:CC:DD:EE:02"
    assert row[5] is None  # name обнулён
    ev = con.execute("SELECT event, severity FROM events").fetchall()
    assert ev == [("MAC_CHANGED", "warning")]
    con.close()


def test_reconcile_no_change_keeps_mac(monkeypatch, devices_db, no_dns):
    monkeypatch.setattr(d, "_max_misses", lambda: 6)
    _seed(devices_db, "192.168.3.21", mac="AA:BB:CC:DD:EE:03")
    con = sqlite3.connect(devices_db)
    stats = d.reconcile(
        con,
        {"192.168.3.21": {"hostname": None, "mac": "aa:bb:cc:dd:ee:03",
                          "vendor": None}},
        now="01.02.2026 10:00:00")
    con.commit()
    assert stats["mac_changed"] == 0
    # регистр не считается сменой, но значение обновляется как есть
    assert _get(devices_db, "192.168.3.21")[1].lower() == \
        "aa:bb:cc:dd:ee:03"
    con.close()


def test_get_scan_status_shape(monkeypatch):
    monkeypatch.setattr(d, "_scan_interval", lambda: 30)
    monkeypatch.setattr(d, "_scan_enabled", lambda: True)
    st = d.get_scan_status()
    assert st["interval_sec"] == 30
    assert st["scan_enabled"] is True
    assert isinstance(st["thread_alive"], bool)
    assert set(st) == {"last_scan", "last_ok", "last_error", "errors",
                       "interval_sec", "scan_enabled", "thread_alive"}


def test_scan_enabled_flag(monkeypatch):
    """PHASE 16 №59: network.scan_enabled (default true, bool/строки)."""
    monkeypatch.setattr(d, "_cfg_net", lambda k, dv=None: dv)
    assert d._scan_enabled() is True
    for val, want in ((False, False), (True, True),
                      ("false", False), ("0", False), ("no", False),
                      ("true", True), ("1", True), ("on", True)):
        monkeypatch.setattr(d, "_cfg_net", lambda k, dv=None, v=val: v)
        assert d._scan_enabled() is want, val


def test_start_scan_thread_guard(monkeypatch):
    hold = threading.Event()
    monkeypatch.setattr(d, "scan_loop", hold.wait)
    monkeypatch.setattr(d, "_scan_thread", None)
    try:
        t1 = d.start_scan_thread()
        t2 = d.start_scan_thread()
        assert t1 is t2
        assert t1.is_alive()
    finally:
        hold.set()
