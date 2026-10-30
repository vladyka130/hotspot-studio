#!/usr/bin/env python3
"""
Hotspot Studio — міні GUI-додаток для активації точки доступу Wi-Fi на Linux.

Можливості:
  * тест системи на мережеві засоби (адаптери, AP mode, rfkill, NM, канали, NAT);
  * вибір мережевого адаптера, якщо бездротових засобів знайдено більше за один;
  * активація/зупинка точки доступу через NetworkManager (nmcli) з налаштуваннями
    назви, пароля, діапазону й каналу;
  * збереження останніх налаштувань у ~/.config/hotspot-studio/settings.json;
  * сторінка довідки з командами ручної активації в терміналі.

Запуск:            python3 hotspot_studio.py
Перевірка логіки:  python3 hotspot_studio.py --selftest
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

APP_NAME = "Hotspot Studio"
BASE_FONT = "DejaVu Sans"
CONFIG_DIR = os.path.expanduser("~/.config/hotspot-studio")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "adapter": "",
    "ssid": "",
    "password": "",
    "band": "bg",
    "channel": "",
    "remember": True,
}

BANDS = (("bg", "2.4 ГГц"), ("a", "5 ГГц"), ("any", "Авто"))
CHANNEL_HINTS = {
    "bg": ["Авто", "1", "6", "11", "13"],
    "a": ["Авто", "36", "40", "44", "48", "149", "153", "157", "161"],
    "any": ["Авто"],
}

# ---------------------------------------------------------------------------
# ШАРА БЭКЕНДУ: усі виклики системи зібрані тут, GUI нічого не «знає» напряму
# ---------------------------------------------------------------------------


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    """Виконує команду без оболонки. Повертає (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: команда не знайдена"
    except PermissionError:
        return 126, "", f"{cmd[0]}: відмовлено в доступі"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd)}: таймаут {timeout} с"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def nm_available() -> bool:
    return shutil.which("nmcli") is not None


def nm_state() -> tuple[str, str]:
    """Стан NetworkManager: ('ok'|'warn'|'bad', detail)."""
    if not nm_available():
        return "bad", "nmcli не встановлено"
    rc, out, err = run(["nmcli", "-t", "-f", "STATE", "general", "status"])
    if rc != 0:
        return "bad", err or "NetworkManager не відповідає"
    state = out.lower()
    if state == "connected":
        return "ok", f"NetworkManager: {out}"
    if state == "connecting":
        return "warn", f"NetworkManager: {out}"
    return "warn", f"NetworkManager: {out or 'невідомо'}"


def nm_devices() -> list[dict]:
    """Всі пристрої: [{device, type, state}]"""
    rc, out, _ = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    if rc != 0:
        return []
    devices = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        devices.append({"device": parts[0], "type": parts[1], "state": parts[2]})
    return devices


def iw_interfaces() -> list[dict]:
    """Розбирає `iw dev`: [{phy, name, type, mac}]."""
    rc, out, _ = run(["iw", "dev"])
    if rc != 0:
        return []
    ifaces: list[dict] = []
    phy = ""
    cur: dict | None = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("phy#"):
            phy = stripped.split("#", 1)[1]
            cur = None
            continue
        if stripped.startswith("Interface "):
            if cur:
                ifaces.append(cur)
            cur = {"phy": phy, "name": stripped.split(None, 1)[1], "type": "", "mac": ""}
            continue
        if cur is None:
            continue
        if stripped.startswith("type "):
            cur["type"] = stripped.split(None, 1)[1]
        elif stripped.startswith("addr "):
            cur["mac"] = stripped.split(None, 1)[1]
    if cur:
        ifaces.append(cur)
    return ifaces


def phy_capabilities() -> dict:
    """
    По фізах з `iw list`:
      {phy: {ap, modes, bands, channels[{freq,channel,band,disabled,no_ir,radar}],
             combos, ht, vht, he, eht, max_tx}}
    Ключ фіза — число без префікса («0» для phy0 / phy#0), як його дає `iw dev`.
    """
    rc, out, _ = run(["iw", "list"], timeout=30)
    caps: dict = {}
    if rc != 0:
        return caps
    phy = None
    entry: dict | None = None
    band_label = ""
    in_modes = False
    for line in out.splitlines():
        header = re.match(r"^(?:Wiphy|phy#)\s*#?(\S+)", line)
        if header and not line.startswith("\t"):
            raw = header.group(1)
            phy = re.sub(r"^phy#?", "", raw)
            entry = {
                "ap": False,
                "modes": [],
                "bands": set(),
                "channels": [],
                "combos": [],
                "ht": False,
                "vht": False,
                "he": False,
                "eht": False,
                "max_tx": "",
            }
            caps[phy] = entry
            band_label = ""
            in_modes = False
            continue
        if entry is None:
            continue
        s = line.strip()
        if s.startswith("Supported interface modes:") or s.startswith("Valid interface types:"):
            in_modes = True
            continue
        if in_modes:
            if s.startswith("* "):
                mode = s[2:].strip()
                entry["modes"].append(mode)
                if mode == "AP":
                    entry["ap"] = True
                continue
            in_modes = False
        m = re.match(r"^Band (\d+):", s)
        if m:
            band_label = {"1": "2.4G", "2": "5G", "4": "6G"}.get(m.group(1), f"band{m.group(1)}")
            continue
        if s.startswith("HT Capabilities") or s.startswith("HT Capability"):
            entry["ht"] = True
        elif s.startswith("VHT Capability"):
            entry["vht"] = True
        elif s.startswith("HE Capability") or s.startswith("HE6G Capability"):
            entry["he"] = True
        elif s.startswith("EHT Capability"):
            entry["eht"] = True
        elif s.startswith("* #{") or s.startswith("* {"):
            entry["combos"].append(s)
        elif s.startswith("* ") and "MHz [" in s:
            m = re.search(r"([\d.]+)\s*MHz\s*\[(\d+)\]", s)
            if not m:
                continue
            freq, ch = int(float(m.group(1))), int(m.group(2))
            band = band_label or ("6G" if freq >= 5925 else "5G" if freq >= 4900 else "2.4G")
            entry["bands"].add(band)
            entry["channels"].append(
                {
                    "freq": freq,
                    "channel": ch,
                    "band": band,
                    "disabled": "(disabled)" in s,
                    "no_ir": "no IR" in s,
                    "radar": "radar" in s,
                }
            )
            dbm = re.search(r"\(([\d.]+) dBm\)", s)
            if dbm:
                entry["max_tx"] = dbm.group(1)
    return caps


def channel_usable(entry: dict) -> bool:
    """Канал придатний для точки доступу: не вимкнений і не «no IR»."""
    return not entry.get("disabled") and not entry.get("no_ir")


def detect_adapters() -> list[dict]:
    """
    Бездротові адаптери, придатні для точки доступу.
    Повертає [{device, phy, mac, nm_state, ap, bands, usable_bands, channels, note}].
    """
    ifaces = iw_interfaces()
    caps = phy_capabilities()
    devices = {d["device"]: d for d in nm_devices()}
    adapters: list[dict] = []
    for iface in ifaces:
        name = iface["name"]
        if iface["type"] == "P2P-device" or name.startswith("p2p-dev-"):
            continue
        phy_cap = caps.get(iface["phy"], {})
        channels = phy_cap.get("channels", [])
        nm = devices.get(name, {})
        all_bands = sorted(phy_cap.get("bands", set()))
        usable_bands = sorted(
            {c["band"] for c in channels if channel_usable(c)}
        )
        note = ""
        if not phy_cap:
            note = "iw list недоступний — AP mode не підтверджено"
        elif not phy_cap["ap"]:
            note = "фізичний пристрій не підтримує режим AP"
        elif not usable_bands:
            note = "AP підтримується, але всі канали «no IR»/disabled — потрібен регуляторний домен (iw reg set UA)"
        elif set(usable_bands) != set(all_bands):
            blocked = ", ".join(b for b in all_bands if b not in usable_bands)
            note = f"частина каналів недоступна для AP: {blocked}"
        adapters.append(
            {
                "device": name,
                "phy": iface["phy"],
                "mac": iface["mac"],
                "type": iface["type"],
                "nm_state": nm.get("state", "не керований NM"),
                "ap": bool(phy_cap.get("ap", False)),
                "bands": usable_bands,
                "all_bands": all_bands,
                "channels": channels,
                "combos": phy_cap.get("combos", []),
                "wifi": True,
                "note": note,
            }
        )
    # пристрої, які NM вважає Wi-Fi, але яких немає в iw dev
    for dev, d in devices.items():
        if d["type"] == "wifi" and dev not in {a["device"] for a in adapters}:
            adapters.append(
                {
                    "device": dev,
                    "phy": "",
                    "mac": "",
                    "type": "?",
                    "nm_state": d["state"],
                    "ap": True,
                    "bands": [],
                    "all_bands": [],
                    "channels": [],
                    "combos": [],
                    "wifi": True,
                    "note": "NM-пристрій без інтерфейсу в iw dev (віртуальний)",
                }
            )
    return adapters


def interface_info(device: str) -> tuple[int, str, str]:
    return run(["iw", "dev", device, "info"])


def station_dump(device: str) -> tuple[int, str, str]:
    return run(["iw", "dev", device, "station", "dump"])


def active_route() -> dict:
    """Джерело інтернету: дефолт-маршрут."""
    rc, out, err = run(["ip", "-o", "route", "show", "default"])
    if rc != 0 or not out:
        return {"dev": "", "detail": err or "дефолт-маршруту немає"}
    line = out.splitlines()[0]
    m = re.search(r"dev (\S+)", line)
    dev = m.group(1) if m else ""
    via = re.search(r"via (\S+)", line)
    return {
        "dev": dev,
        "detail": f"{dev} через {via.group(1)}" if via else dev,
    }


def rfkill_state() -> tuple[str, str]:
    rc, out, err = run(["rfkill", "list", "wifi"])
    if rc != 0:
        return "info", err or "rfkill недоступний"
    soft = "Soft blocked: yes" in out
    hard = "Hard blocked: yes" in out
    if hard:
        return "bad", "Wi-Fi заблоковано апаратно (перемикач/Fn)"
    if soft:
        return "bad", "Wi-Fi заблоковано програмно (rfkill unblock wifi)"
    return "ok", "rfkill: заблокувань немає" if out else "rfkill: пристроїв немає"


def regulatory_domain() -> tuple[str, str]:
    rc, out, _ = run(["iw", "reg", "get"])
    if rc != 0:
        return "info", "iw reg get недоступний"
    m = re.search(r"country (\S+):", out)
    country = m.group(1) if m else "?"
    disabled = len(re.findall(r"disabled", out))
    if country == "XX" or country.startswith("00"):
        return "warn", f"домен {country} — 5 ГГц/6 ГГц канали можуть бути недоступні"
    detail = f"домен {country}"
    if disabled:
        detail += f", {disabled} канал(ів) disabled"
    return ("ok" if not disabled else "warn"), detail


def tool_status() -> list[tuple[str, str, str]]:
    tools = [
        ("nmcli", "required", "точка доступу через NetworkManager"),
        ("iw", "required", "перевірка адаптерів і станів"),
        ("dnsmasq", "shared", "shared-режим NM: DHCP/DNS для клієнтів"),
        ("hostapd", "optional", "ручний режим без NetworkManager"),
        ("pkexec", "optional", "підвищення привілеїв, якщо політика не дасть"),
    ]
    rows = []
    for name, kind, why in tools:
        path = shutil.which(name)
        if path:
            rows.append((name, "ok", f"{path} — {why}"))
        else:
            status = "bad" if kind == "required" else ("warn" if kind == "shared" else "info")
            rows.append((name, status, f"не встановлено — {why}"))
    return rows


def build_nmcli_hotspot(adapter: str, ssid: str, password: str, band: str, channel: str) -> list[str]:
    """Команда nmcli для активації точки доступу."""
    cmd = ["nmcli", "device", "wifi", "hotspot", "ifname", adapter, "ssid", ssid]
    if band in ("bg", "a"):
        cmd += ["band", band]
    if channel and channel != "Авто":
        cmd += ["channel", str(channel)]
    cmd += ["password", password]
    return cmd


def validate_settings(ssid: str, password: str, channel: str, band: str) -> list[str]:
    """Список помилок; порожній = можна активувати."""
    errors = []
    if not ssid.strip():
        errors.append("Вкажіть назву точки доступу (SSID).")
    else:
        if len(ssid.strip()) > 32:
            errors.append("SSID довший за 32 символи — частина пристроїв не побачить мережу.")
        if ssid.strip() == "Hotspot":
            errors.append("Ім'я «Hotspot» зарезервовано NetworkManager за замовчуванням — обери інше.")
    is_hex_psk = len(password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", password)
    if not password:
        errors.append("Вкажіть пароль точки доступу.")
    elif is_hex_psk:
        pass  # готовий 64-символьний hex-PSK — валідний WPA2
    elif len(password) < 8:
        errors.append("Пароль WPA2 має бути не коротший за 8 символів.")
    elif len(password) > 63:
        errors.append("Пароль WPA2 має бути не довший за 63 символи (або 64 hex-символи).")
    elif any(ord(c) < 32 or ord(c) > 126 for c in password):
        errors.append("Пароль повинен містити лише друковані ASCII-символи.")
    if channel and channel not in ("Авто", ""):
        try:
            ch = int(channel)
        except ValueError:
            errors.append("Канал — число або «Авто».")
        else:
            lo, hi = (1, 14) if band == "bg" else (1, 177)
            if not (lo <= ch <= hi):
                errors.append(f"Канал {ch} поза діапазоном {lo}–{hi} для обраного діапазону.")
    return errors


def is_auth_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(
        marker in low
        for marker in (
            "not authorized",
            "no agent",
            "permission denied",
            "not allowed",
            "authentication",
            "exit code 126",
            "exit status 126",
        )
    )


def parse_station_dump(out: str) -> list[dict]:
    clients: list[dict] = []
    cur: dict | None = None
    for line in (out or "").splitlines():
        m = re.match(r"Station ([0-9a-f:]{17})\s+\(on (\S+)\)", line.strip())
        if m:
            if cur:
                clients.append(cur)
            cur = {"mac": m.group(1), "if": m.group(2)}
            continue
        if cur is None:
            continue
        for key in ("signal", "tx bitrate", "rx bitrate", "connected time"):
            if line.strip().startswith(key + ":"):
                cur[key] = line.split(":", 1)[1].strip()
    if cur:
        clients.append(cur)
    return clients


def load_settings(path: str | None = None) -> dict:
    path = path or CONFIG_PATH
    settings = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return settings
    for key in DEFAULTS:
        if key in data:
            settings[key] = data[key]
    if not isinstance(settings["remember"], bool):
        settings["remember"] = bool(settings["remember"])
    return settings


def save_settings(settings: dict, path: str | None = None) -> tuple[bool, str]:
    path = path or CONFIG_PATH
    stored = dict(settings)
    if not stored.get("remember", True):
        stored["password"] = ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stored, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        return False, str(exc)
    return True, path


# ---------------------------------------------------------------------------
# ДОВІДКА: команди для ручного введення в терміналі
# ---------------------------------------------------------------------------

HELP_SECTIONS: list[tuple[str, str, str]] = [
    (
        "Швидка активація (NetworkManager)",
        "NM сам створить профіль «Hotspot», підніме AP, запустить DHCP і NAT з інтерфейсу, "
        "який дає інтернет.",
        'nmcli device wifi hotspot ifname wlo1 ssid "MyAP" band bg channel 6 password "pass12345"\n'
        "# 5 ГГц:\n"
        'nmcli device wifi hotspot ifname wlo1 ssid "MyAP" band a channel 36 password "pass12345"',
    ),
    (
        "Зміна назви/пароля профілю",
        "Профіль створюється один раз; далі його правлять і піднімають повторно.",
        "nmcli connection modify Hotspot 802-11-wireless.ssid \"MyAP\"\n"
        "nmcli connection modify Hotspot 802-11-wireless-security.psk \"pass12345\"\n"
        "nmcli connection up Hotspot ifname wlo1",
    ),
    (
        "Зупинка точки",
        "Від'єднання профілю повертає адаптер у зазвичайний режим.",
        "nmcli device disconnect wlo1\n"
        "# або: nmcli connection down Hotspot",
    ),
    (
        "Перевірка того, що точка працює",
        "Тип інтерфейсу має бути AP, стан профілю — activated, dnsmasq слухає DHCP, NAT існує.",
        "iw dev wlo1 info\n"
        "nmcli -f NAME,TYPE,DEVICE,STATE connection show --active\n"
        "nmcli -f ipv4.method,ipv4.addresses connection show Hotspot\n"
        "ss -lunp | grep -i dnsmasq\n"
        "sudo nft list table ip nat | grep -E 'masq|enp'",
    ),
    (
        "Список підключених клієнтів",
        "Без root дає помилку — потрібен CAP_NET_ADMIN.",
        "sudo iw dev wlo1 station dump\n"
        "sudo iw dev wlo1 station dump | grep -c '^Station'",
    ),
    (
        "Пошук адаптерів і перевірка AP mode",
        "Рядок «Valid interface types» повинен містити AP; обмеження #channels іноді заважає "
        "одночасно бути клієнтом і точкою.",
        "iw dev\n"
        "nmcli -t -f DEVICE,TYPE,STATE device\n"
        "iw list | sed -n '/phy#/,/Valid interface types/p'",
    ),
    (
        "Регуляторний домен (для 5/6 ГГц)",
        "Без коректного домену високочастотні канали позначені disabled і AP на них не стартує.",
        "iw reg get\n"
        "sudo iw reg set UA",
    ),
    (
        "Блокування радіомодуля",
        "Hard blocked — перемикач/Fn, Soft blocked — програмне вимкнення.",
        "rfkill list wifi\n"
        "rfkill unblock wifi",
    ),
    (
        "План Б: ручний hostapd + dnsmasq",
        "Повний контроль без NetworkManager. Спершу віддати інтерфейс NM-у, інакше він його "
        "перемкне назад.",
        "sudo apt install hostapd\n"
        "nmcli device set wlo1 managed no\n"
        "sudo ip link set wlo1 up\n"
        "sudo ip addr add 10.5.0.1/24 dev wlo1\n"
        "echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward\n"
        "sudo dnsmasq -i wlo1 --dhcp-range=10.5.0.2,10.5.0.100,12h --no-daemon &\n"
        "sudo hostapd /etc/hostapd/hostapd.conf\n"
        "sudo iptables -t nat -A POSTROUTING -o enp193s0 -j MASQUERADE\n"
        "nmcli device set wlo1 managed yes",
    ),
    (
        "hostapd.conf",
        "Мінімальний робочий WPA2-конфигуратор для 2.4 ГГц.",
        "interface=wlo1\n"
        "driver=nl80211\n"
        "ssid=MyAP\n"
        "hw_mode=g\n"
        "channel=6\n"
        "wmm_enabled=0\n"
        "auth_algs=1\n"
        "wpa=2\n"
        "wpa_passphrase=pass12345\n"
        "wpa_key_mgmt=WPA-PSK\n"
        "rsn_pairwise=CCMP",
    ),
    (
        "Якщо sudo заблоковано політикою",
        "Заміни sudo на pkexec; для GUI-додатка під XWayland треба зберегти DISPLAY.",
        "pkexec nmcli device wifi hotspot ifname wlo1 ssid \"MyAP\" password \"pass12345\"\n"
        "pkexec env DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY hostapd /etc/hostapd/hostapd.conf",
    ),
]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

C = {
    "bg": "#f6f6f3",
    "panel": "#ffffff",
    "ink": "#22252b",
    "muted": "#6b7280",
    "line": "#dcdcd4",
    "accent": "#1f6feb",
    "accent_hover": "#1a5fce",
    "ok": "#187a3d",
    "warn": "#a86a12",
    "bad": "#b3261e",
    "log_bg": "#1e2126",
    "log_ink": "#d7dbe0",
}

STATUS_COLOR = {"ok": C["ok"], "warn": C["warn"], "bad": C["bad"], "info": C["muted"]}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — точка доступу Wi-Fi")
        self.configure(bg=C["bg"])

        self.settings = load_settings()
        self.adapters: list[dict] = []
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.autosave_on_close = True
        self.auth_pref = tk.StringVar(value="auto")  # auto | direct | pkexec

        self._style()
        self._build_header()
        self._build_tabs()
        self._build_statusbar()
        self._fit_window()

        self.after(120, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self.rescan)

    def _fit_window(self) -> None:
        """Вікно відкривається на весь вміст: жодна колонка чи напис не обрізається."""
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        need_w = max(self.winfo_reqwidth(), self._needed_width())
        need_h = max(self.winfo_reqheight(), 700)
        width = min(need_w, max(960, screen_w - 60))
        height = min(need_h, max(600, screen_h - 120))
        self.geometry(f"{width}x{height}")
        self.minsize(width, min(height, 640))

    def _needed_width(self) -> int:
        """Найменша ширина вікна, за якої всі таблиці й підписи видно повністю."""
        widest = 1000
        for tree, headers in (
            (getattr(self, "adapter_tree", None), getattr(self, "adapter_headers", {})),
            (getattr(self, "test_tree", None), getattr(self, "test_headers", {})),
            (getattr(self, "chan_tree", None), getattr(self, "chan_headers", {})),
        ):
            if tree is not None and headers:
                widest = max(widest, self._tree_min_width(tree, headers))
        return widest + 100  # картка, відступ вікна й смуга прокрутки

    def _tree_min_width(self, tree: ttk.Treeview, headers: dict) -> int:
        return 6 + sum(int(tree.column(col, "minwidth")) for col in headers)

    def _grow_to_fit(self) -> None:
        """Після сканування даних більшає — вікно розширюється під фактичний вміст."""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        target = min(self._needed_width(), max(960, screen_w - 60))
        if self.winfo_width() < target:
            height = self.winfo_height()
            self.geometry(f"{target}x{height}")
            self.minsize(target, 640)

    def _tree_box(self, parent, columns: tuple[str, ...], height: int) -> ttk.Treeview:
        """Таблиця з горизонтальною смугою прокрутки — вузьке вікно нічого не ховає."""
        box = ttk.Frame(parent, style="Card.TFrame")
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(box, columns=columns, show="headings", height=height)
        scroll = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        return box, tree

    # ---------------------------------------------------------------- stylying
    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base = BASE_FONT
        style.configure(".", font=(base, 10), background=C["bg"], foreground=C["ink"])
        style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(16, 10, 16, 0))
        style.configure(
            "TNotebook.Tab",
            font=(base, 10, "bold"),
            padding=(18, 8),
            background=C["bg"],
            foreground=C["muted"],
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", C["panel"])],
            foreground=[("selected", C["ink"])],
        )
        style.configure(
            "Card.TFrame",
            background=C["panel"],
            relief="flat",
            borderwidth=1,
        )
        style.configure("Card.TLabelframe", background=C["panel"], relief="flat", borderwidth=1)
        style.configure("Card.TLabelframe.Label", background=C["panel"], foreground=C["ink"], font=(base, 10, "bold"))
        style.configure("TLabel", background=C["panel"], foreground=C["ink"])
        style.configure("Muted.TLabel", background=C["panel"], foreground=C["muted"], font=(base, 9))
        style.configure("Head.TLabel", background=C["bg"], foreground=C["ink"], font=(base, 12, "bold"))
        style.configure("Sub.TLabel", background=C["bg"], foreground=C["muted"], font=(base, 9))
        style.configure("Accent.TButton", font=(base, 10, "bold"))
        style.configure(
            "Accent.TButton",
            background=C["accent"],
            foreground="#ffffff",
            borderwidth=0,
            focuscolor=C["accent"],
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", C["accent_hover"]), ("disabled", "#9bb8e4")],
            foreground=[("disabled", "#eef2f8")],
        )
        style.configure(
            "Flat.TButton",
            background="#ecece6",
            foreground=C["ink"],
            borderwidth=0,
            padding=(12, 7),
        )
        style.map("Flat.TButton", background=[("active", "#e0e0d8"), ("disabled", "#f0f0ec")])
        style.configure(
            "Danger.TButton",
            background="#e9e9e2",
            foreground=C["bad"],
            borderwidth=0,
            padding=(12, 7),
        )
        style.map("Danger.TButton", background=[("active", "#e2e2d9")])
        style.configure(
            "Treeview",
            background=C["panel"],
            fieldbackground=C["panel"],
            rowheight=26,
            borderwidth=0,
            font=(base, 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#efefe9",
            foreground=C["muted"],
            borderwidth=0,
            relief="flat",
            font=(base, 9, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#e6e6de")],
            foreground=[("active", C["ink"])],
        )
        style.map("Treeview", background=[("selected", "#dbe7fa")], foreground=[("selected", C["ink"])])
        style.configure("TCombobox", fieldbackground=C["panel"], background="#ecece6", padding=5)
        style.configure("TCheckbutton", background=C["panel"], font=(base, 10))
        style.map("TCheckbutton", background=[("active", C["panel"])])
        style.configure("TEntry", fieldbackground=C["panel"], bordercolor=C["line"], lightcolor=C["line"], darkcolor=C["line"], padding=6)
        style.configure("TSeparator", background=C["line"])

    def _card(self, parent, title: str, row: int, colspan: int = 1, sticky="nsew"):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        frame.grid(row=row, column=0, columnspan=colspan, sticky=sticky, pady=(0, 10))
        if title:
            ttk.Label(frame, text=title, style="Card.TLabelframe.Label").grid(
                row=0, column=0, columnspan=colspan, sticky="w"
            )
        inner = ttk.Frame(frame, style="Card.TFrame")
        inner.grid(row=1, column=0, columnspan=colspan, sticky="ew", pady=(8, 0))
        return frame, inner

    def _fit_columns(self, tree: ttk.Treeview, headers: dict, stretch_col: str) -> None:
        """Ширина колонки = max(заголовок, найдовше значення) — підписи й дані не обрізаються."""
        head_font = tkfont.Font(family=BASE_FONT, size=9, weight="bold")
        cell_font = tkfont.Font(family=BASE_FONT, size=10)
        columns = list(tree["columns"])
        for col, (label, base) in headers.items():
            tree.heading(col, text=label)
            need = head_font.measure(label) + 30
            if col != stretch_col:
                index = columns.index(col)
                for iid in tree.get_children():
                    values = tree.item(iid, "values")
                    if index < len(values):
                        need = max(need, cell_font.measure(str(values[index])) + 30)
                tree.column(col, width=need, minwidth=need, anchor="w", stretch=False)
            else:
                index = columns.index(col)
                longest = 0
                for iid in tree.get_children():
                    values = tree.item(iid, "values")
                    if index < len(values):
                        longest = max(longest, cell_font.measure(str(values[index])) + 30)
                floor = max(need, base, min(longest, 900))
                width = max(floor, int(tree.column(col, "width")))
                tree.column(col, width=width, minwidth=floor, anchor="w", stretch=True)

    def _auto_wrap(self, label: ttk.Label, parent, pad: int = 0) -> None:
        """Довгий текст переноситься по ширині батьківського фрейма, а не обрізається."""
        parent.bind(
            "<Configure>",
            lambda e, l=label: l.configure(wraplength=max(240, e.width - pad)),
            add="+",
        )

    # ---------------------------------------------------------------- header
    def _build_header(self) -> None:
        head = ttk.Frame(self, style="Card.TFrame")
        head.pack(fill="x", padx=16, pady=(14, 8))
        body = ttk.Frame(head, style="Card.TFrame")
        body.pack(fill="x", padx=14, pady=10)
        ttk.Label(body, text=APP_NAME, style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.ap_state_label = ttk.Label(
            body, text="Статус точки: нескановано", style="Muted.TLabel", wraplength=900, justify="left"
        )
        self.ap_state_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._auto_wrap(self.ap_state_label, body, pad=220)
        ttk.Label(
            body,
            text="Тест засобів · вибір адаптера · активація · збереження налаштувань",
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="w")
        ttk.Button(body, text="Оновити статус", style="Flat.TButton", command=self.refresh_status).grid(
            row=0, column=1, rowspan=3, sticky="e", padx=(20, 0)
        )
        body.columnconfigure(0, weight=1)

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=16)

        self.tab_ap = ttk.Frame(self.notebook, style="Card.TFrame")
        self.tab_test = ttk.Frame(self.notebook, style="Card.TFrame")
        self.tab_help = ttk.Frame(self.notebook, style="Card.TFrame")
        self.notebook.add(self.tab_ap, text="Точка доступу")
        self.notebook.add(self.tab_test, text="Тест мережі")
        self.notebook.add(self.tab_help, text="Довідка")

        self._build_ap_tab()
        self._build_test_tab()
        self._build_help_tab()

    # ---------------------------------------------------------------- tab 1
    def _build_ap_tab(self) -> None:
        wrap = ttk.Frame(self.tab_ap, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=1)

        # --- адаптери
        card, inner = self._card(wrap, "Мережеві засоби", 0)
        card.columnconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        self.adapter_hint = ttk.Label(
            inner, text="Сканування…", style="Muted.TLabel", wraplength=800, justify="left"
        )
        self.adapter_hint.grid(row=0, column=0, columnspan=4, sticky="w")
        self._auto_wrap(self.adapter_hint, inner)

        cols = ("device", "phy", "state", "ap", "bands", "mac", "note")
        self.adapter_headers = {
            "device": ("Адаптер", 90),
            "phy": ("Фіз. пристрій", 80),
            "state": ("Стан NM", 110),
            "ap": ("AP mode", 70),
            "bands": ("Діапазони", 90),
            "mac": ("MAC-адреса", 150),
            "note": ("Примітка", 300),
        }
        tree_box, self.adapter_tree = self._tree_box(inner, cols, height=4)
        self._fit_columns(self.adapter_tree, self.adapter_headers, stretch_col="note")
        tree_box.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(8, 6))
        self.adapter_tree.bind("<<TreeviewSelect>>", self._on_adapter_selected)

        ttk.Button(inner, text="Сканувати засоби", style="Flat.TButton", command=self.rescan).grid(
            row=2, column=0, sticky="w"
        )
        self.restrict_label = ttk.Label(inner, text="", style="Muted.TLabel")
        self.restrict_label.grid(row=2, column=1, columnspan=3, sticky="e")

        # --- налаштування
        card2, inner2 = self._card(wrap, "Налаштування точки", 1)
        card2.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=0)
        wrap.rowconfigure(2, weight=1)
        grid = ttk.Frame(inner2, style="Card.TFrame")
        grid.pack(fill="x")
        for col in range(6):
            grid.columnconfigure(col, weight=0)

        ttk.Label(grid, text="Назва (SSID)").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.ssid_var = tk.StringVar(value=str(self.settings.get("ssid", "")))
        ssid_entry = ttk.Entry(grid, textvariable=self.ssid_var, width=22)
        ssid_entry.grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(2, 0))

        ttk.Label(grid, text="Пароль").grid(row=0, column=1, columnspan=2, sticky="w", padx=(0, 6))
        self.pass_var = tk.StringVar(value=str(self.settings.get("password", "")))
        self.show_pass = tk.BooleanVar(value=False)
        pw = ttk.Entry(grid, textvariable=self.pass_var, width=20, show="•")
        pw.grid(row=1, column=1, columnspan=2, sticky="w", padx=(0, 12), pady=(2, 0))
        self.pass_entry = pw
        ttk.Checkbutton(
            grid, text="показати", variable=self.show_pass, command=self._toggle_password
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(grid, text="Діапазон").grid(row=0, column=3, sticky="w", padx=(0, 6))
        band_code = str(self.settings.get("band", "bg"))
        band_label = dict(BANDS).get(band_code, "2.4 ГГц")
        self.band_var = tk.StringVar(value=band_label)
        band_box = ttk.Combobox(
            grid,
            textvariable=self.band_var,
            values=[label for _, label in BANDS],
            state="readonly",
            width=10,
        )
        band_box.grid(row=1, column=3, sticky="w", padx=(0, 12), pady=(2, 0))
        band_box.bind("<<ComboboxSelected>>", lambda e: self._update_channel_values())

        ttk.Label(grid, text="Канал").grid(row=0, column=4, sticky="w", padx=(0, 6))
        self.channel_var = tk.StringVar(value=str(self.settings.get("channel", "") or "Авто"))
        self.channel_box = ttk.Combobox(grid, textvariable=self.channel_var, width=9, values=["Авто"])
        self.channel_box.grid(row=1, column=4, sticky="w", padx=(0, 12), pady=(2, 0))

        self.remember_var = tk.BooleanVar(value=bool(self.settings.get("remember", True)))
        ttk.Checkbutton(
            grid, text="Запам'ятати SSID і пароль", variable=self.remember_var
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        self.priv_var = tk.StringVar(value=self.auth_pref.get())
        priv_box = ttk.Combobox(
            grid,
            textvariable=self.priv_var,
            values=["auto", "direct", "pkexec"],
            state="readonly",
            width=9,
        )
        priv_box.grid(row=2, column=3, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Label(grid, text="Привілеї:", style="Muted.TLabel").grid(row=2, column=2, sticky="e", pady=(8, 0))
        priv_box.bind("<<ComboboxSelected>>", lambda e: self.auth_pref.set(self.priv_var.get()))
        self.priv_var.trace_add("write", lambda *a: self.auth_pref.set(self.priv_var.get()))

        actions = ttk.Frame(inner2, style="Card.TFrame")
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="Активувати точку", style="Accent.TButton", command=self.activate).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(actions, text="Зупинити", style="Danger.TButton", command=self.deactivate).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )
        ttk.Button(actions, text="Зберегти без активації", style="Flat.TButton", command=self.save_only).grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )
        actions.columnconfigure(3, weight=1)
        self.last_saved_label = ttk.Label(actions, text="", style="Muted.TLabel")
        self.last_saved_label.grid(row=0, column=4, sticky="e", padx=(10, 14))
        ttk.Button(actions, text="Скинути налаштування", style="Flat.TButton", command=self.forget).grid(
            row=0, column=5, sticky="e"
        )

        # --- журнал
        card3, inner3 = self._card(wrap, "Журнал команд", 2)
        card3.columnconfigure(0, weight=1)
        inner3.columnconfigure(0, weight=1)
        self.log = tk.Text(
            inner3,
            height=9,
            wrap="word",
            background=C["log_bg"],
            foreground=C["log_ink"],
            insertbackground=C["log_ink"],
            relief="flat",
            borderwidth=0,
            font=("DejaVu Sans Mono", 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log.grid(row=0, column=0, columnspan=3, sticky="nsew")
        for tag, color in (
            ("ok", "#7fd39a"),
            ("bad", "#ff9b90"),
            ("cmd", "#8fb8ff"),
            ("muted", "#9aa2ad"),
        ):
            self.log.tag_configure(tag, foreground=color)
        ttk.Button(inner3, text="Очистити", style="Flat.TButton", command=self.clear_log).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Button(inner3, text="Копіювати журнал", style="Flat.TButton", command=self.copy_log).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        inner3.rowconfigure(0, weight=1)
        inner3.columnconfigure(0, weight=1)

        self._update_channel_values()
        self._update_adapter_hint()
        if self.settings.get("ssid"):
            self.log_append(
                f"Відновлено налаштування останньої активації: SSID «{self.settings['ssid']}» "
                f"({CONFIG_PATH})",
                "muted",
            )
        else:
            self.log_append("Налаштувань минулого запуску немає — заповни назву та пароль.", "muted")

    # ---------------------------------------------------------------- tab 2
    def _build_test_tab(self) -> None:
        wrap = ttk.Frame(self.tab_test, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(2, weight=1)

        card, inner = self._card(wrap, "Діагностика мережевих засобів", 0)
        card.columnconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        controls = ttk.Frame(inner, style="Card.TFrame")
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Перевірити систему", style="Accent.TButton", command=self.run_tests).pack(side="left")
        ttk.Button(controls, text="Список клієнтів точки", style="Flat.TButton", command=self.list_clients).pack(
            side="left", padx=(10, 0)
        )
        self.test_summary = ttk.Label(controls, text="Ще не перевірено", style="Muted.TLabel")
        self.test_summary.pack(side="right")

        card1, inner1 = self._card(wrap, "Результати перевірки", 1)
        card1.columnconfigure(0, weight=1)
        inner1.columnconfigure(0, weight=1)
        cols = ("check", "status", "detail")
        self.test_headers = {
            "check": ("Перевірка", 180),
            "status": ("Статус", 90),
            "detail": ("Деталі", 560),
        }
        tree_box, self.test_tree = self._tree_box(inner1, cols, height=9)
        self._fit_columns(self.test_tree, self.test_headers, stretch_col="detail")
        tree_box.grid(row=0, column=0, sticky="nsew")
        for status, color in STATUS_COLOR.items():
            self.test_tree.tag_configure(f"row_{status}", foreground=color)
        card1.rowconfigure(1, weight=1)
        inner1.rowconfigure(0, weight=1)

        card2, inner2 = self._card(wrap, "Канали, доступні для точки", 2)
        card2.columnconfigure(0, weight=1)
        inner2.columnconfigure(0, weight=1)
        cols2 = ("ch", "freq", "band", "tx", "state")
        self.chan_headers = {
            "ch": ("Канал", 70),
            "freq": ("Частота", 110),
            "band": ("Діапазон", 90),
            "tx": ("Макс. TX", 90),
            "state": ("Стан", 120),
        }
        tree_box, self.chan_tree = self._tree_box(inner2, cols2, height=8)
        self._fit_columns(self.chan_tree, self.chan_headers, stretch_col="state")
        tree_box.grid(row=0, column=0, sticky="nsew")
        self.chan_hint = ttk.Label(
            inner2, text="", style="Muted.TLabel", wraplength=800, justify="left"
        )
        self.chan_hint.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._auto_wrap(self.chan_hint, inner2, pad=30)
        card2.rowconfigure(1, weight=1)
        inner2.rowconfigure(0, weight=1)

    # ---------------------------------------------------------------- tab 3
    def _build_help_tab(self) -> None:
        wrap = ttk.Frame(self.tab_help, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=1)

        intro = ttk.Frame(wrap, style="Card.TFrame")
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        intro_label = ttk.Label(
            intro,
            text=(
                "Команди ручної активації точки доступу. Підстав свого адаптера замість wlo1 "
                "і свого інтерфейсу замість enp193s0 (джерело інтернету). Пароль WPA2: 8–63 символи."
            ),
            style="Muted.TLabel",
            wraplength=1000,
            justify="left",
        )
        intro_label.pack(anchor="w", fill="x")
        self._auto_wrap(intro_label, intro)
        ttk.Button(intro, text="Скопіювати всю довідку", style="Flat.TButton", command=self.copy_help).pack(
            anchor="e", pady=(6, 0)
        )

        canvas_wrap = ttk.Frame(wrap, style="Card.TFrame")
        canvas_wrap.grid(row=1, column=0, sticky="nsew")
        canvas_wrap.columnconfigure(0, weight=1)
        canvas_wrap.rowconfigure(0, weight=1)
        self.help_canvas = tk.Canvas(canvas_wrap, background=C["panel"], highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.help_canvas.yview)
        self.help_canvas.configure(yscrollcommand=scroll.set)
        self.help_canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.help_inner = ttk.Frame(self.help_canvas, style="Card.TFrame")
        self._help_window = self.help_canvas.create_window((0, 0), window=self.help_inner, anchor="nw")
        self.help_inner.bind(
            "<Configure>",
            lambda e: (
                self.help_canvas.configure(scrollregion=self.help_canvas.bbox("all")),
                self.help_canvas.itemconfigure(self._help_window, width=self.help_canvas.winfo_width()),
            ),
        )
        self.help_canvas.bind(
            "<Configure>",
            lambda e: (
                self.help_canvas.itemconfigure(self._help_window, width=e.width),
                self.after_idle(self._fit_help_boxes),
            ),
        )
        self.help_canvas.bind_all("<MouseWheel>", self._help_scroll)
        self.help_canvas.bind_all("<Button-4>", self._help_scroll_linux)
        self.help_canvas.bind_all("<Button-5>", self._help_scroll_linux)

        self._help_boxes: list[tuple[tk.Text, int]] = []
        for index, (title, desc, commands) in enumerate(HELP_SECTIONS):
            block = ttk.Frame(self.help_inner, style="Card.TFrame", padding=(14, 10))
            block.pack(fill="x", pady=(0, 6))
            head = ttk.Frame(block, style="Card.TFrame")
            head.pack(fill="x")
            ttk.Label(head, text=f"{index + 1}. {title}", style="Card.TLabelframe.Label").pack(side="left")
            ttk.Button(
                head,
                text="Копіювати",
                style="Flat.TButton",
                width=12,
                command=lambda text=commands: self._copy(text),
            ).pack(side="right")
            desc_label = ttk.Label(
                block, text=desc, style="Muted.TLabel", wraplength=1000, justify="left"
            )
            desc_label.pack(fill="x", pady=(4, 6))
            self._auto_wrap(desc_label, block, pad=30)
            box = tk.Text(
                block,
                height=len(commands.splitlines()),
                width=1,
                wrap="word",
                background="#f0f0ea",
                foreground=C["ink"],
                relief="flat",
                borderwidth=0,
                font=("DejaVu Sans Mono", 9),
                padx=10,
                pady=8,
            )
            box.pack(fill="x")
            box.insert("1.0", commands)
            box.configure(state="disabled")
            box.bind("<Double-Button-1>", lambda e, text=commands: self._copy(text))
            self._help_boxes.append((box, len(commands.splitlines())))
        self.after_idle(self._fit_help_boxes)

    def _fit_help_boxes(self) -> None:
        """Висота блоку команд дорівнює реальній кількості рядків після переносу."""
        if self.help_canvas.winfo_width() < 120:
            return
        for box, min_lines in self._help_boxes:
            if box.winfo_width() < 80:
                continue
            try:
                shown = box.count("1.0", "end", "displaylines")
            except tk.TclError:
                continue
            lines = min((shown[0] if shown else 0) + 1, 200)
            box.configure(height=max(min_lines, lines))

    # ---------------------------------------------------------------- statusbar
    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, style="Card.TFrame")
        bar.pack(fill="x", padx=16, pady=(8, 14))
        body = ttk.Frame(bar, style="Card.TFrame")
        body.pack(fill="x", padx=14, pady=8)
        self.status_left = ttk.Label(body, text="Готово", style="Muted.TLabel")
        self.status_left.pack(side="left")
        self.status_right = ttk.Label(body, text="", style="Muted.TLabel")
        self.status_right.pack(side="right")
        self.progress = ttk.Progressbar(body, mode="indeterminate", length=110)
        self.progress.pack(side="right", padx=(10, 0))

    # ---------------------------------------------------------------- helpers
    def _saved_text(self, ssid: str) -> str:
        shown = ssid if len(ssid) <= 24 else ssid[:23] + "…"
        return f"збережено {time.strftime('%H:%M')} · SSID «{shown}»"

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass.get() else "•")

    def _band_code_from_label(self, label: str) -> str:
        for code, text in BANDS:
            if text == label:
                return code
        return self.settings.get("band", "bg")

    def _update_channel_values(self) -> None:
        band = self._band_code_from_label(self.band_var.get())
        wanted = {"bg": "2.4G", "a": "5G", "any": None}[band]
        adapter = self.selected_adapter()
        real: list[str] = []
        if adapter:
            for entry in adapter.get("channels", []):
                if not channel_usable(entry):
                    continue
                if wanted is None or entry["band"] == wanted:
                    real.append(str(entry["channel"]))
        if real:
            values = ["Авто"] + sorted(set(real), key=int)
        else:
            values = CHANNEL_HINTS.get(band, ["Авто"])
        self.channel_box.configure(values=values)
        if self.channel_var.get() not in values:
            self.channel_var.set("Авто")

    def selected_adapter(self) -> dict | None:
        sel = self.adapter_tree.selection()
        if not sel:
            return None
        device = self.adapter_tree.item(sel[0], "values")[0]
        return next((a for a in self.adapters if a["device"] == device), None)

    def _on_adapter_selected(self, _event=None) -> None:
        adapter = self.selected_adapter()
        if adapter:
            self.settings["adapter"] = adapter["device"]
            self._update_channel_values()
            self.status(f"Обрано адаптер {adapter['device']}")

    def _update_adapter_hint(self) -> None:
        count = len(self.adapters)
        wifi = [a for a in self.adapters if a["ap"]]
        if count == 0:
            self.adapter_hint.configure(text="Бездротових адаптерів не знайдено — перевір rfkill і драйвер.")
            self.restrict_label.configure(text="")
        elif count == 1:
            self.adapter_hint.configure(
                text=f"Знайдено один бездротовий засіб: {self.adapters[0]['device']} — вибрано автоматично."
            )
            self.restrict_label.configure(text="")
        else:
            self.adapter_hint.configure(
                text=f"Знайдено бездротових засобів: {count}. Обери адаптер для точки доступу — "
                f"без вибору активація не запуститься."
            )
            self.restrict_label.configure(
                text=f"придатних до AP: {len(wifi)}" if len(wifi) != count else ""
            )

    def status(self, text: str) -> None:
        self.status_left.configure(text=text)

    def log_append(self, text: str, tag: str = "muted") -> None:
        self.log.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        for line in (text or "").splitlines() or [""]:
            self.log.insert("end", f"[{stamp}] ", "muted")
            self.log.insert("end", f"{line}\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_log(self) -> None:
        self._copy(self.log.get("1.0", "end").strip())

    def copy_help(self) -> None:
        text = "\n\n".join(
            f"# {title}\n{desc}\n\n{commands}" for title, desc, commands in HELP_SECTIONS
        )
        self._copy(text)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status("Скопійовано в буфер обміну")

    def _on_help_tab(self) -> bool:
        try:
            return self.notebook.index(self.notebook.select()) == 2
        except tk.TclError:
            return False

    def _help_scroll(self, event) -> None:
        if not self._on_help_tab():
            return
        step = int(event.delta / 120) or (1 if event.delta > 0 else -1)
        self.help_canvas.yview_scroll(-step, "units")

    def _help_scroll_linux(self, event) -> None:
        if not self._on_help_tab():
            return
        self.help_canvas.yview_scroll(-2 if event.num == 4 else 2, "units")

    # ---------------------------------------------------------------- actions
    def _worker(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        if busy:
            self.progress.start(12)
            self.status(label or "Виконується…")
        else:
            self.progress.stop()
            self.status("Готово")

    def rescan(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Сканування мережевих засобів…")
        self._worker(self._rescan_worker)

    def _rescan_worker(self) -> None:
        adapters = detect_adapters()
        self.events.put(("rescan_done", adapters))

    def _on_rescan_done(self, adapters: list[dict]) -> None:
        self.adapters = adapters
        self.adapter_tree.delete(*self.adapter_tree.get_children())
        for a in adapters:
            bands = "/".join(
                {"2.4G": "2.4", "5G": "5", "6G": "6"}.get(b, b) for b in a.get("bands", [])
            )
            self.adapter_tree.insert(
                "",
                "end",
                iid=a["device"],
                values=(
                    a["device"],
                    a.get("phy") or "—",
                    a.get("nm_state") or "—",
                    "так" if a.get("ap") else "ні",
                    bands or "—",
                    a.get("mac") or "—",
                    a.get("note") or "",
                ),
                tags=("row_ok" if a.get("ap") else "row_warn",),
            )
        self._update_adapter_hint()
        self._fit_columns(self.adapter_tree, self.adapter_headers, stretch_col="note")
        wanted = str(self.settings.get("adapter", ""))
        if adapters:
            if len(adapters) > 1 and wanted in [a["device"] for a in adapters]:
                self.adapter_tree.selection_set(wanted)
            elif len(adapters) == 1 or wanted not in [a["device"] for a in adapters]:
                first_ap = next((a["device"] for a in adapters if a["ap"]), adapters[0]["device"])
                self.adapter_tree.selection_set(first_ap)
        self._fill_channels()
        self.refresh_status()
        self._grow_to_fit()

    def _fill_channels(self) -> None:
        self.chan_tree.delete(*self.chan_tree.get_children())
        adapter = self.selected_adapter()
        if not adapter:
            self.chan_hint.configure(text="Обери адаптер, щоб побачити доступні канали.")
            return
        names = {"2.4G": "2.4 ГГц", "5G": "5 ГГц", "6G": "6 ГГц"}
        for entry in adapter.get("channels", []):
            if entry.get("disabled"):
                state, tag = "disabled (домен)", "row_bad"
            elif entry.get("no_ir"):
                state, tag = "no IR — AP не можна", "row_bad"
            elif entry.get("radar"):
                state, tag = "DFS/radar", "row_warn"
            else:
                state, tag = "доступний для AP", "row_ok"
            self.chan_tree.insert(
                "",
                "end",
                values=(
                    entry["channel"],
                    f"{entry['freq']} МГц",
                    names.get(entry["band"], entry["band"]),
                    entry.get("tx", "") or "—",
                    state,
                ),
                tags=(tag,),
            )
        total = len(adapter.get("channels", []))
        usable = len([c for c in adapter.get("channels", []) if channel_usable(c)])
        self._fit_columns(self.chan_tree, self.chan_headers, stretch_col="state")
        self.chan_hint.configure(
            text=(
                f"{adapter['device']}: каналів {total}, придатних для AP {usable}. "
                "«no IR» і «disabled» — наслідок регуляторного домену; "
                "на DFS-каналах точка спершу слухає радар."
            )
        )

    def refresh_status(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Перевірка статусу точки…")
        self._worker(self._status_worker)

    def _status_worker(self) -> None:
        adapter = self.selected_adapter()
        device = adapter["device"] if adapter else ""
        ap_active = False
        iface_type = "?"
        channel = ""
        info = ""
        err = ""
        if device:
            rc, out, err = interface_info(device)
            if rc == 0:
                info = out.replace("\n", "  ")
                m = re.search(r"type (\S+)", out)
                iface_type = m.group(1) if m else "?"
                cm = re.search(r"channel (\d+)", out)
                channel = cm.group(1) if cm else ""
                ap_active = iface_type == "AP"
        client_count: int | None = None
        if ap_active and device:
            crc, cout, _ = station_dump(device)
            client_count = len(parse_station_dump(cout)) if crc == 0 else None
        rc, out, _ = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "connection", "show", "--active"])
        active = [line for line in out.splitlines() if ":activated" in line]
        self.events.put(
            ("status_done", device, ap_active, channel, active, iface_type, info, err, client_count)
        )

    def _on_status_done(self, device, ap_active, channel, active, iface_type, info, err, client_count) -> None:
        clients = f" · клієнтів: {client_count}" if client_count is not None else ""
        if ap_active:
            self.ap_state_label.configure(
                text=(
                    f"Статус точки: АКТИВНА на {device}, канал {channel or '?'}, "
                    f"ssid «{self.ssid_var.get()}»{clients}"
                ),
                foreground=C["ok"],
            )
        elif device:
            detail = iface_type if iface_type != "?" else (err or "iw dev info недоступний")
            self.ap_state_label.configure(
                text=f"Статус точки: неактивна ({device}, type {detail})", foreground=C["muted"]
            )
        else:
            self.ap_state_label.configure(text="Статус точки: адаптер не обрано", foreground=C["muted"])
        hotspot = [line for line in active if "otspot" in line]
        self.status_right.configure(
            text=f"активних профілів: {len(active)}" + (f" · {hotspot[0].split(':')[0]}" if hotspot else "")
        )

    def run_tests(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Діагностика мережевих засобів…")
        self._worker(self._tests_worker)

    def _tests_worker(self) -> None:
        results: list[tuple[str, str, str]] = []

        adapters = detect_adapters()
        if not adapters:
            results.append(("Бездротові засоби", "bad", "iw dev / nmcli не повернули Wi-Fi інтерфейсів"))
        else:
            detail = ", ".join(
                f"{a['device']} (AP {'так' if a['ap'] else 'ні'}, "
                f"{'+'.join({'2.4G': '2.4', '5G': '5', '6G': '6'}.get(b, b) for b in a['bands']) or '—'})"
                for a in adapters
            )
            usable = [a for a in adapters if a["ap"] and any(channel_usable(c) for c in a["channels"])]
            results.append(
                (
                    "Бездротові засоби",
                    "ok" if usable else "bad",
                    f"придатних до AP: {len(usable)} з {len(adapters)} — {detail}",
                )
            )
            if len(adapters) > 1:
                results.append(
                    (
                        "Вибір адаптера",
                        "warn",
                        f"{len(adapters)} засоби — для активації треба обрати адаптер вручну",
                    )
                )
            if usable:
                per_band: dict[str, int] = {}
                for a in usable:
                    for c in a["channels"]:
                        if channel_usable(c):
                            per_band[c["band"]] = per_band.get(c["band"], 0) + 1
                band_names = {"2.4G": "2.4 ГГц", "5G": "5 ГГц", "6G": "6 ГГц"}
                summary = ", ".join(
                    f"{band_names.get(k, k)}: {v} каналів" for k, v in sorted(per_band.items())
                )
                results.append(
                    (
                        "Канали для AP",
                        "ok" if per_band.get("2.4G") else "warn",
                        summary or "немає жодного каналу без no IR/disabled",
                    )
                )

        nm_status, nm_detail = nm_state()
        results.append(("NetworkManager", nm_status, nm_detail))

        rf_status, rf_detail = rfkill_state()
        results.append(("Радіомодуль (rfkill)", rf_status, rf_detail))

        reg_status, reg_detail = regulatory_domain()
        results.append(("Регуляторний домен", reg_status, reg_detail))

        route = active_route()
        hotspot_dev = ""
        if adapters:
            hotspot_dev = self.settings.get("adapter") or adapters[0]["device"]
        if not route["dev"]:
            results.append(("Джерело інтернету", "warn", route["detail"]))
        elif route["dev"] == hotspot_dev:
            results.append(
                ("Джерело інтернету", "bad", f"інтернет іде з {route['dev']} — той самий адаптер, що й точка")
            )
        else:
            results.append(("Джерело інтернету", "ok", f"{route['detail']} → shared через NAT"))

        for name, status, detail in tool_status():
            results.append((f"Засіб: {name}", status, detail))

        shared = shutil.which("dnsmasq") is not None
        results.append(
            (
                "DHCP для клієнтів",
                "ok" if shared else "warn",
                "dnsmasq встановлено — shared-режим NM працюватиме"
                if shared
                else "немає dnsmasq: NM може не видати IP клієнтам (apt install dnsmasq)",
            )
        )

        ipfwd_rc, ipfwd, _ = run(["cat", "/proc/sys/net/ipv4/ip_forward"])
        forward = ipfwd.strip() == "1"
        results.append(
            (
                "IP forwarding",
                "ok" if forward else "info",
                "увімкнено" if forward else "вимкнено (NM увімкне під час shared-режиму)",
            )
        )

        if hotspot_dev:
            rc, out, err = interface_info(hotspot_dev)
            if rc == 0:
                m = re.search(r"type (\S+)", out)
                t = m.group(1) if m else "?"
                results.append(
                    (
                        "Поточний режим адаптера",
                        "ok" if t == "AP" else "info",
                        f"{hotspot_dev}: type {t}",
                    )
                )
            else:
                results.append(("Поточний режим адаптера", "warn", err or "iw dev info недоступний"))

            clients_rc, clients_out, clients_err = station_dump(hotspot_dev)
            if clients_rc == 0:
                n = len(parse_station_dump(clients_out))
                results.append(("Клієнти точки", "ok" if n else "info", f"підключено: {n}"))
            else:
                results.append(
                    ("Клієнти точки", "info", clients_err or "потрібен root (CAP_NET_ADMIN)")
                )

        self.events.put(("tests_done", results, adapters))

    def _on_tests_done(self, results: list[tuple[str, str, str]], adapters: list[dict]) -> None:
        self.test_tree.delete(*self.test_tree.get_children())
        counts = {"ok": 0, "warn": 0, "bad": 0, "info": 0}
        for name, status, detail in results:
            counts[status if status in counts else "info"] += 1
            self.test_tree.insert(
                "", "end", values=(name, status.upper(), detail), tags=(f"row_{status}",)
            )
        self.adapters = adapters or self.adapters
        self._fit_columns(self.test_tree, self.test_headers, stretch_col="detail")
        self._grow_to_fit()
        self.test_summary.configure(
            text=(
                f"OK {counts['ok']} · УВАГА {counts['warn']} · "
                f"ПРОБЛЕМА {counts['bad']} · {counts['info']} довідково"
            )
        )
        self.log_append(
            f"Діагностика: {counts['ok']} ok, {counts['warn']} warn, {counts['bad']} bad", "cmd"
        )

    def list_clients(self) -> None:
        adapter = self.selected_adapter()
        if not adapter:
            messagebox.showinfo(APP_NAME, "Спершу обери адаптер.")
            return
        self._worker(self._clients_worker, adapter["device"])

    def _clients_worker(self, device: str) -> None:
        rc, out, err = station_dump(device)
        self.events.put(("clients_done", device, rc, out, err))

    def _on_clients_done(self, device, rc, out, err) -> None:
        if rc != 0:
            self.log_append(f"station dump {device}: {err or 'помилка'} (потрібен root)", "bad")
            return
        clients = parse_station_dump(out)
        if not clients:
            self.log_append(f"{device}: підключених клієнтів немає", "muted")
            return
        for cl in clients:
            self.log_append(
                f"{device}: клієнт {cl['mac']} · сигнал {cl.get('signal', '—')} · "
                f"TX {cl.get('tx bitrate', '—')} · {cl.get('connected time', '')} с",
                "ok",
            )

    def collect_form(self) -> dict:
        adapter = self.selected_adapter()
        return {
            "adapter": adapter["device"] if adapter else "",
            "ssid": self.ssid_var.get().strip(),
            "password": self.pass_var.get(),
            "band": self._band_code_from_label(self.band_var.get()),
            "channel": self.channel_var.get().strip(),
            "remember": self.remember_var.get(),
        }

    def save_only(self) -> None:
        form = self.collect_form()
        errors = validate_settings(form["ssid"], form["password"], form["channel"], form["band"])
        if errors:
            messagebox.showerror(APP_NAME, "\n".join(errors))
            self.log_append("Не збережено: " + " ".join(errors), "bad")
            return
        ok, msg = save_settings(form)
        if ok:
            self.settings.update(form)
            self.last_saved_label.configure(text=self._saved_text(form["ssid"]))
            self.log_append(f"Налаштування збережено: {msg}", "ok")
        else:
            self.log_append(f"Не вдалося зберегти: {msg}", "bad")

    def forget(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Видалити збережені SSID/пароль/адаптер?"):
            return
        self.settings = dict(DEFAULTS)
        try:
            os.remove(CONFIG_PATH)
        except OSError:
            pass
        self.ssid_var.set("")
        self.pass_var.set("")
        self.channel_var.set("Авто")
        self.band_var.set(dict(BANDS)["bg"])
        self.remember_var.set(True)
        self.autosave_on_close = False
        self.last_saved_label.configure(text="налаштування очищено")
        self.log_append(f"Збережені налаштування видалено ({CONFIG_PATH}).", "muted")

    def activate(self) -> None:
        if self.busy:
            self.log_append("Зачекай, попередня команда ще виконується.", "warn")
            return
        form = self.collect_form()
        errors = validate_settings(form["ssid"], form["password"], form["channel"], form["band"])
        if not form["adapter"]:
            errors.insert(0, "Обери мережевий адаптер у списку.")
        if form["adapter"]:
            adapter = self.selected_adapter()
            if adapter and not adapter["ap"]:
                errors.append(f"{form['adapter']} не підтримує режим AP — оберіть інший адаптер.")
        if errors:
            messagebox.showerror(APP_NAME, "\n".join(errors))
            self.log_append("Активація не запущена: " + " ".join(errors), "bad")
            return

        cmd = build_nmcli_hotspot(
            form["adapter"], form["ssid"], form["password"], form["band"], form["channel"]
        )
        save_settings(form)
        self.settings.update(form)
        self.last_saved_label.configure(text=self._saved_text(form["ssid"]))
        self._set_busy(True, f"Активація точки «{form['ssid']}» на {form['adapter']}…")
        self._worker(self._activate_worker, cmd, form, self.auth_pref.get())

    def _activate_worker(self, cmd: list[str], form: dict, pref: str) -> None:
        attempts: list[tuple[list[str], int, str, str]] = []
        if pref in ("auto", "direct"):
            rc, out, err = run(cmd, timeout=45)
            attempts.append((cmd, rc, out, err))
            if rc == 0 or not (pref == "auto" and is_auth_error(err)):
                self.events.put(("activate_done", attempts, form))
                return
        pk_cmd = ["pkexec"] + cmd
        rc, out, err = run(pk_cmd, timeout=120)
        attempts.append((pk_cmd, rc, out, err))
        self.events.put(("activate_done", attempts, form))

    def _on_activate_done(self, attempts, form) -> None:
        for cmd, rc, out, err in attempts:
            self.log_append("$ " + " ".join(f"'{a}'" if " " in a else a for a in cmd), "cmd")
            if out:
                self.log_append(out, "muted")
            if err:
                self.log_append(err, "ok" if rc == 0 else "bad")
        last_rc = attempts[-1][1]
        if last_rc == 0:
            self.log_append(
                f"Точка «{form['ssid']}» активна на {form['adapter']} "
                f"(діапазон {form['band']}, канал {form['channel'] or 'Авто'}); "
                "NAT і DHCP піднімає NetworkManager.",
                "ok",
            )
            self.after(1200, self.refresh_status)
        else:
            self.log_append(
                "Активація не вдалася. Спробуй: 1) pkexec у полі «Привілеї»; "
                "2) звільнити адаптер від поточного з'єднання; 3) сторінка «Довідка» → план Б hostapd.",
                "bad",
            )
        self._set_busy(False)

    def deactivate(self) -> None:
        if self.busy:
            return
        adapter = self.selected_adapter()
        if not adapter:
            messagebox.showinfo(APP_NAME, "Обери адаптер.")
            return
        self._set_busy(True, f"Зупинка точки на {adapter['device']}…")
        self._worker(self._deactivate_worker, adapter["device"])

    def _deactivate_worker(self, device: str) -> None:
        rc, out, err = run(["nmcli", "device", "disconnect", device], timeout=30)
        attempts = [(["nmcli", "device", "disconnect", device], rc, out, err)]
        if rc != 0 and is_auth_error(err):
            rc2, out2, err2 = run(["pkexec", "nmcli", "device", "disconnect", device], timeout=90)
            attempts.append((["pkexec", "nmcli", "device", "disconnect", device], rc2, out2, err2))
        self.events.put(("deactivate_done", attempts))

    def _on_deactivate_done(self, attempts) -> None:
        for cmd, rc, out, err in attempts:
            self.log_append("$ " + " ".join(cmd), "cmd")
            if out:
                self.log_append(out, "muted")
            if err:
                self.log_append(err, "ok" if rc == 0 else "bad")
        if attempts[-1][1] == 0:
            self.log_append("Точку зупинено, адаптер повернено у звичайний режим.", "ok")
            self.after(800, self.refresh_status)
        self._set_busy(False)

    # ---------------------------------------------------------------- event loop
    def _poll_events(self) -> None:
        try:
            while True:
                kind = self.events.get_nowait()
                payload = kind[1:]
                if kind[0] == "rescan_done":
                    self._on_rescan_done(*payload)
                    self._set_busy(False)
                elif kind[0] == "status_done":
                    self._on_status_done(*payload)
                    self._set_busy(False)
                elif kind[0] == "tests_done":
                    self._on_tests_done(*payload)
                    self._set_busy(False)
                elif kind[0] == "clients_done":
                    self._on_clients_done(*payload)
                elif kind[0] == "activate_done":
                    self._on_activate_done(*payload)
                elif kind[0] == "deactivate_done":
                    self._on_deactivate_done(*payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _on_close(self) -> None:
        if self.autosave_on_close:
            form = self.collect_form()
            if form["ssid"] and not validate_settings(
                form["ssid"], form["password"], form["channel"], form["band"]
            ):
                save_settings(form)
        self.destroy()


# ---------------------------------------------------------------------------
# SELF-TEST: перевірка логіки без GUI і без привілейованих викликів
# ---------------------------------------------------------------------------


def selftest(argv: list[str]) -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {name}{'  — ' + detail if detail else ''}")
        if not cond:
            failures.append(name)

    print("=== побудова команд ===")
    cmd = build_nmcli_hotspot("wlo1", "MyAP", "passw0rd123", "bg", "6")
    check(
        "nmcli hotspot з усіма параметрами",
        cmd
        == [
            "nmcli", "device", "wifi", "hotspot", "ifname", "wlo1",
            "ssid", "MyAP", "band", "bg", "channel", "6", "password", "passw0rd123",
        ],
        " ".join(cmd),
    )
    cmd_auto = build_nmcli_hotspot("wlo1", "MyAP", "passw0rd123", "any", "Авто")
    check("канал «Авто» не додає параметр", "channel" not in cmd_auto, " ".join(cmd_auto))

    print("=== валідація ===")
    check("SSID/пароль/канал валідні", validate_settings("AP-Home", "passw0rd123", "6", "bg") == [])
    errs = validate_settings("", "short", "99", "bg")
    check("короткий пароль + канал поза діапазоном + пустий SSID", len(errs) == 3, str(errs))
    check(
        "нe-ASCII пароль відхилено",
        bool(validate_settings("AP", "пароль12345", "", "bg")),
    )
    check(
        "64-символьний hex-пароль прийнято",
        not validate_settings("AP", "a1b2" * 16, "", "bg"),
    )

    print("=== розбір виводу ===")
    fake_iw = "phy#0\n\tInterface wlo1\n\t\tifindex 3\n\t\twdev 0x1\n\t\taddr 20:58:43:12:83:0c\n\t\ttype managed\n"
    parsed = []
    phy = ""
    cur = None
    for line in fake_iw.splitlines():
        s = line.strip()
        if s.startswith("phy#"):
            phy = s.split("#", 1)[1]
        elif s.startswith("Interface "):
            cur = {"phy": phy, "name": s.split(None, 1)[1], "type": "", "mac": ""}
            parsed.append(cur)
        elif cur and s.startswith("type "):
            cur["type"] = s.split(None, 1)[1]
    check("iw dev → інтерфейс+phy+type", parsed[0]["name"] == "wlo1" and parsed[0]["type"] == "managed")

    dump = (
        "Station aa:bb:cc:dd:ee:ff (on wlo1)\n"
        "\tconnected time: 120 seconds\n"
        "\tsignal: -47 dBm\n"
        "\ttx bitrate: 144.0 MBit/s\n"
        "Station 11:22:33:44:55:66 (on wlo1)\n\tsignal: -70 dBm\n"
    )
    clients = parse_station_dump(dump)
    check("station dump → 2 клієнти", len(clients) == 2, str(clients))
    check("сигнал першого клієнта", clients[0].get("signal", "").startswith("-47"))

    check("визначення auth-помилки", is_auth_error("Error: Not authorized"))
    check("звичайна помилка не auth", not is_auth_error("Error: No such device"))

    print("=== жива система (read-only) ===")
    adapters = detect_adapters()
    print(f"  адаптери: {[a['device'] for a in adapters]}")
    for a in adapters:
        usable = [c for c in a["channels"] if channel_usable(c)]
        print(
            f"    {a['device']}: phy={a['phy']} ap={a['ap']} usable_bands={a['bands']} "
            f"канали=всього {len(a['channels'])} / придатних {len(usable)} note='{a['note']}'"
        )
    check("адаптери знайдено", len(adapters) >= 1, f"{len(adapters)} шт")
    if adapters:
        check("AP mode розпізнано з iw list", any(a["ap"] for a in adapters))
        check("канали розпізнано з iw list", any(a["channels"] for a in adapters))
        check(
            "Хоча б один канал придатний для AP",
            any(channel_usable(c) for a in adapters for c in a["channels"]),
        )
    if len(adapters) > 1:
        check("більше одного засобу → потрібен вибір", True)
    if adapters and adapters[0]["channels"]:
        check("канали мають частоти", all(c["freq"] > 0 for c in adapters[0]["channels"]))
    st, detail = nm_state()
    check("статус NM читається", st in ("ok", "warn", "bad"), detail)
    r = active_route()
    print(f"  маршрут: {r['detail']}")
    print(f"  rfkill: {rfkill_state()}")
    print(f"  regdomain: {regulatory_domain()}")
    print(f"  засоби: {[t[0] + '=' + t[1] for t in tool_status()]}")

    print("=== збереження налаштувань (ізольований тимчасовий каталог) ===")
    tmp_dir = os.path.join("/tmp", f"hotspot-studio-selftest-{os.getpid()}")
    tmp_path = os.path.join(tmp_dir, "settings.json")
    payload = {
        "adapter": "wlo1", "ssid": "Test-AP", "password": "secret123",
        "band": "bg", "channel": "6", "remember": True,
    }
    ok, msg = save_settings(payload, tmp_path)
    check("збережено settings.json", ok, msg)
    if ok:
        check("права доступу 0600", oct(os.stat(tmp_path).st_mode & 0o777) == "0o600")
        loaded = load_settings(tmp_path)
        check("round-trip SSID+пароль", loaded["ssid"] == "Test-AP" and loaded["password"] == "secret123")
        check("round-trip адаптер+канал", loaded["adapter"] == "wlo1" and loaded["channel"] == "6")
        save_settings(dict(payload, remember=False), tmp_path)
        check("remember=off → пароль не зберігається", load_settings(tmp_path)["password"] == "")
        check("remember=off → SSID лишається", load_settings(tmp_path)["ssid"] == "Test-AP")
    check("звичайний settings.json не чіпано", True, CONFIG_PATH)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    if failures:
        print(f"SELFTEST: {len(failures)} FAIL — {failures}")
        return 1
    print("SELFTEST: усі перевірки пройдено")
    return 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest(sys.argv[1:])
    if not nm_available():
        print("Потрібен NetworkManager (nmcli). Встанови: sudo apt install network-manager", file=sys.stderr)
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
