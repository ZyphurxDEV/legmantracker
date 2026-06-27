"""
Legman Tracker - standalone Windows tray app.

Polls Roblox games (and every subplace inside them) and:
  - pops a Windows toast notification on a game/place update, AND
  - keeps a live "recent updates" feed in a styled flyout panel that opens when
    you click the tray icon (where you can also add / remove games).

The Roblox-fetching + toast backend is shared with the discord bot's logic; the
UI is built with PySide6 (Qt) - frameless, dark, rounded.

Each tracked game has its own settings (the cogwheel on its row) choosing what to
be alerted on: game updates, subplace updates, subplaces added/removed, status
changes, new badges, deleted badges, and rare-badge award-count changes. Defaults
are game + subplace updates only; everything else is opt-in per game.
"""

import os
import re
import sys
import json
import time
import asyncio
import logging
import functools
import threading
import traceback
import webbrowser
from datetime import datetime

import aiohttp

from windows_toasts import (
    Toast,
    InteractableWindowsToaster,
    ToastDisplayImage,
    ToastImagePosition,
)

from PySide6 import QtCore, QtGui, QtWidgets, QtSvg
from PySide6.QtCore import Qt

# --------------------------------------------------------------------------- #
# config / paths
# --------------------------------------------------------------------------- #

APP_NAME = "Legman Tracker"
APP_ID = "legmantracker"
AUMID = "LegmanTracker"

BADGE_RARE_THRESHOLD = 250

UA = {"User-Agent": "LegmanTracker/1.0 (+https://www.roblox.com)"}

DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LegmanTracker")
TRACKED_FILE = os.path.join(DATA_DIR, "tracked_games.json")
HISTORY_FILE = os.path.join(DATA_DIR, "updates_history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "legmantracker.log")
ICON_DIR = os.path.join(DATA_DIR, "icons")

HISTORY_MAX = 100

POLL_INTERVAL_DEFAULT = 60
POLL_INTERVAL_MIN = 30
POLL_INTERVAL_MAX = 3600
_poll_interval = POLL_INTERVAL_DEFAULT

STATUS_LABELS = {
    "Playable": "playable",
    "GuestProhibited": "playable",
    "UniverseRootPlaceIsPrivate": "private",
    "GameUnapproved": "unapproved",
    "InsufficientPermissionFuzz": "restricted",
    "PurchaseRequired": "paid access",
    "DeviceRestricted": "device restricted",
    "UnderReview": "under review",
    "None": "playable",
}

ACCENT = {
    "update": "#00d488",
    "added": "#3fd0dd",
    "removed": "#e85050",
    "status_up": "#00d488",
    "status_down": "#e85050",
    "status": "#e8b84b",
    "badge": "#c08cff",
    "badge_del": "#e85050",
    "info": "#7d8b97",
}

# --------------------------------------------------------------------------- #
# globals
# --------------------------------------------------------------------------- #

logger = logging.getLogger(APP_ID)
TRACK_LOCK = threading.RLock()
HISTORY_LOCK = threading.RLock()
STOP = threading.Event()

LOOP = None
POLL_LOCK = None
TOASTS_ON = False
SIGNALS = None
POPUP_VISIBLE = False


def _make_toaster():
    return InteractableWindowsToaster(APP_NAME, notifierAUMID=AUMID)


# --------------------------------------------------------------------------- #
# helpers (ported from the bot)
# --------------------------------------------------------------------------- #

def get_unix_ts(ts):
    if not ts:
        return 0
    ts_str = str(ts).replace("Z", "+00:00")
    try:
        if "." in ts_str:
            main_part, frac_tz = ts_str.split(".", 1)
            if "+" in frac_tz:
                frac, tz = frac_tz.split("+", 1)
                ts_str = f"{main_part}.{frac[:6]}+{tz}"
            elif "-" in frac_tz:
                frac, tz = frac_tz.split("-", 1)
                ts_str = f"{main_part}.{frac[:6]}-{tz}"
            else:
                ts_str = f"{main_part}.{frac_tz[:6]}"
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp())
    except Exception:
        return 0


def is_real_update(ts1, ts2):
    """Forward-only update check (roblox cache flaps backwards constantly)."""
    if not ts1 or not ts2:
        return False
    u1 = get_unix_ts(ts1)
    u2 = get_unix_ts(ts2)
    if u1 > 0 and u2 > 0:
        return u2 - u1 > 2
    return False


def human_ts(ts):
    u = get_unix_ts(ts)
    if u <= 0:
        return "unknown"
    try:
        return datetime.fromtimestamp(u).strftime("%b %d, %Y %I:%M %p").lower()
    except Exception:
        return "unknown"


def rel_time(unix_ts):
    try:
        d = int(time.time()) - int(unix_ts)
    except Exception:
        return ""
    if d < 0:
        d = 0
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def parse_place_id(text):
    if not text:
        return None
    text = str(text).strip()
    m = re.search(r"/games/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4,})", text)
    if m:
        return m.group(1)
    return None


_PLACEHOLDER_KEYWORDS = ("unavailable", "content deleted", "not available", "deleted", "moderat")


def is_placeholder_name(name):
    if not name:
        return True
    s = str(name).strip()
    if not (s.startswith("[") and s.endswith("]")):
        return False
    inner = s[1:-1].strip().lower()
    return any(kw in inner for kw in _PLACEHOLDER_KEYWORDS)


def recover_name(info, cached_name, root_place_id):
    """Best real name we have when the live name is a placeholder: prefer the
    cached game name, else the stored root sub-place name, else any sub-place."""
    if not is_placeholder_name(cached_name):
        return cached_name
    subs = info.get("subplaces", {}) or {}
    root = subs.get(str(root_place_id), {}) or {}
    if root.get("name") and not is_placeholder_name(root.get("name")):
        return root["name"]
    for sp in subs.values():
        n = sp.get("name")
        if n and not is_placeholder_name(n):
            return n
    return cached_name or "unknown game"


def good_game_name(new_name, cached_name):
    return new_name if not is_placeholder_name(new_name) else (cached_name or "unknown game")


def load_tracked():
    with TRACK_LOCK:
        if os.path.exists(TRACKED_FILE):
            try:
                with open(TRACKED_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}


def save_tracked(data):
    with TRACK_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = TRACKED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TRACKED_FILE)


def remove_tracked(key):
    with TRACK_LOCK:
        data = load_tracked()
        if key in data:
            name = data[key].get("game_name", "that game")
            del data[key]
            save_tracked(data)
            return name
        return None


NOTIFY_DEFAULTS = {
    "game_update": True,
    "subplace_update": True,
    "subplace_add": False,
    "subplace_delete": False,
    "status_change": False,
    "badge_new": False,
    "badge_delete": False,
    "badge_rare_award": False,
}

NOTIFY_CATEGORIES = [
    ("game_update", "game updates", "the main game is updated"),
    ("subplace_update", "place updates", "a subplace is updated"),
    ("subplace_add", "new subplaces", "a subplace is added"),
    ("subplace_delete", "deleted subplaces", "a subplace is removed"),
    ("status_change", "status changes", "goes private, comes back, paid, etc."),
    ("badge_new", "new badges", "a badge is created"),
    ("badge_delete", "disabled badges", "a badge is disabled"),
    ("badge_rare_award", "rare badge awards", f"award changes (≤ {BADGE_RARE_THRESHOLD} owners)"),
]


def get_notify(info):
    n = dict(NOTIFY_DEFAULTS)
    saved = (info or {}).get("notify") or {}
    for k in NOTIFY_DEFAULTS:
        if k in saved:
            n[k] = bool(saved[k])
    return n


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def subplace_count(info):
    """Number of real subplaces, i.e. excluding the root place (the root IS the
    main game, not a subplace)."""
    subs = info.get("subplaces", {}) or {}
    root = str(info.get("root_place_id"))
    return sum(1 for pid in subs if str(pid) != root)


def set_notify_option(key, opt, value):
    with TRACK_LOCK:
        data = load_tracked()
        if key in data:
            merged = {**NOTIFY_DEFAULTS, **(data[key].get("notify") or {}), opt: bool(value)}
            data[key]["notify"] = merged
            save_tracked(data)
            return merged
    return None


def load_history():
    with HISTORY_LOCK:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []


def add_history(event):
    with HISTORY_LOCK:
        hist = load_history()
        hist.insert(0, event)
        hist = hist[:HISTORY_MAX]
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2)
        os.replace(tmp, HISTORY_FILE)


def save_history(hist):
    with HISTORY_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist[:HISTORY_MAX], f, indent=2)
        os.replace(tmp, HISTORY_FILE)


def clear_history():
    with HISTORY_LOCK:
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
        except Exception:
            logger.exception("failed to clear history")


def _badge_id_from_url(url):
    m = re.search(r"/badges/(\d+)", str(url or ""))
    return m.group(1) if m else None



def load_settings():
    with TRACK_LOCK:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}


def save_settings(data):
    with TRACK_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)


def _clamp_interval(v):
    try:
        return max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, int(v)))
    except Exception:
        return POLL_INTERVAL_DEFAULT


def load_poll_interval():
    """Load the saved interval into the cache (called once at startup)."""
    global _poll_interval
    _poll_interval = _clamp_interval(load_settings().get("poll_interval", POLL_INTERVAL_DEFAULT))
    return _poll_interval


def current_poll_interval():
    return _poll_interval


def set_poll_interval(seconds):
    global _poll_interval
    _poll_interval = _clamp_interval(seconds)
    s = load_settings()
    s["poll_interval"] = _poll_interval
    save_settings(s)
    return _poll_interval


def _migrate_badge_cache():
    """One-time cleanup of badge icons cached by older versions (some were saved
    as blank/pending placeholders and would never refresh). Runs once; from then
    on badge icons are always re-fetched fresh (force=True)."""
    marker = os.path.join(DATA_DIR, ".badge_cache_v2")
    if os.path.exists(marker):
        return
    try:
        if os.path.isdir(ICON_DIR):
            for fn in os.listdir(ICON_DIR):
                if fn.startswith("badge_") and fn.endswith(".png"):
                    try:
                        os.remove(os.path.join(ICON_DIR, fn))
                    except Exception:
                        pass
        with open(marker, "w") as f:
            f.write("ok")
    except Exception:
        logger.exception("badge cache migration failed")


# --------------------------------------------------------------------------- #
# roblox api (async, ported)
# --------------------------------------------------------------------------- #

async def resolve_universe(session, place_id):
    try:
        async with session.get(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        ) as resp:
            if resp.status == 200:
                return (await resp.json()).get("universeId")
    except Exception:
        pass
    return None


async def fetch_game(session, universe_id):
    """list (maybe empty=private) on success, None on transient failure."""
    try:
        async with session.get(
            f"https://games.roblox.com/v1/games?universeIds={universe_id}"
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        return None


async def get_status(session, universe_id, game_data, fallback_status="unknown"):
    price = game_data.get("price")
    if price and isinstance(price, (int, float)) and price > 0:
        return f"paid access ({int(price)} R$)"
    try:
        async with session.get(
            f"https://games.roblox.com/v1/games/multiget-playability-status?universeIds={universe_id}"
        ) as resp:
            if resp.status == 200:
                status_resp = await resp.json()
                status_list = status_resp if isinstance(status_resp, list) else status_resp.get("data", [])
                if status_list and isinstance(status_list[0], dict):
                    raw = str(status_list[0].get("playabilityStatus", "unknown"))
                    return STATUS_LABELS.get(raw, raw.lower())
            elif resp.status == 429 or resp.status >= 500:
                return fallback_status
    except Exception:
        pass
    return fallback_status


async def fetch_all_places(session, universe_id):
    places = []
    cursor = None
    while True:
        url = f"https://develop.roblox.com/v1/universes/{universe_id}/places?sortOrder=Asc&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                places.extend(data.get("data", []))
                cursor = data.get("nextPageCursor")
                if not cursor:
                    break
        except Exception:
            return None
    return places


async def fetch_place_updated(session, place_id):
    try:
        async with session.get(
            f"https://economy.roblox.com/v2/assets/{place_id}/details"
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("Updated")
    except Exception:
        pass
    return None


async def fetch_icon_url(session, universe_id):
    try:
        async with session.get(
            f"https://thumbnails.roblox.com/v1/games/icons"
            f"?universeIds={universe_id}&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false"
        ) as resp:
            if resp.status == 200:
                icon_resp = await resp.json()
                icon_data = icon_resp.get("data", []) if isinstance(icon_resp, dict) else []
                if icon_data:
                    return icon_data[0].get("imageUrl")
    except Exception:
        pass
    return None


async def ensure_icon_file(session, key, icon_url):
    return await ensure_image_file(session, str(key), icon_url)


async def ensure_image_file(session, name, url, force=False):
    """Download an image and cache it locally (toast images must be a local file
    on win32). `force` re-downloads even if cached - used for badge icons, whose
    artwork can change (e.g. a brand-new badge's icon finishing processing)."""
    if not url:
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    path = os.path.join(ICON_DIR, f"{name}.png")
    if os.path.exists(path) and not force:
        return path
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.read()
                with open(path, "wb") as f:
                    f.write(data)
                return path
    except Exception:
        pass
    return path if os.path.exists(path) else None


async def fetch_all_badges(session, universe_id):
    """None on failure (so a partial fetch isn't read as 'all badges deleted'),
    else a list of badge dicts (possibly empty)."""
    badges = []
    cursor = None
    while True:
        url = f"https://badges.roblox.com/v1/universes/{universe_id}/badges?limit=100&sortOrder=Asc"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                badges.extend(data.get("data", []))
                cursor = data.get("nextPageCursor")
                if not cursor:
                    break
        except Exception:
            return None
    return badges


async def fetch_badge_icon(session, badge_id):
    try:
        async with session.get(
            f"https://thumbnails.roblox.com/v1/badges/icons"
            f"?badgeIds={badge_id}&size=150x150&format=Png&isCircular=false"
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data", []) if isinstance(data, dict) else []
                if items and items[0].get("state") == "Completed":
                    return items[0].get("imageUrl")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# notifications + feed events
# --------------------------------------------------------------------------- #

def notify(title, lines, launch_url=None, image_path=None):
    """Pop a Windows toast (skipped while the panel is open on screen)."""
    if not TOASTS_ON:
        return
    if POPUP_VISIBLE:
        logger.info("toast skipped (panel open): %s", title)
        return
    try:
        fields = [title] + [ln for ln in (lines or []) if ln][:3]
        images = []
        if image_path and os.path.exists(image_path):
            try:
                images = [ToastDisplayImage.fromPath(image_path, position=ToastImagePosition.AppLogo)]
            except Exception:
                images = []
        toast = Toast(text_fields=fields, launch_action=(launch_url or None), images=images)
        _make_toaster().show_toast(toast)
        logger.info("toast shown: %s", title)
    except Exception:
        logger.exception("failed to show toast")


def push_event(event, toast=True):
    """Record a feed event: optional toast + history + live signal to the gui."""
    try:
        if toast:
            notify(event.get("game_name", APP_NAME),
                   event.get("lines") or [event.get("text", "")],
                   launch_url=event.get("url"),
                   image_path=event.get("icon_path"))
        add_history(event)
        if SIGNALS is not None:
            SIGNALS.update_event.emit(event)
    except Exception:
        logger.exception("push_event failed")


def emit_status(text):
    if SIGNALS is not None:
        try:
            SIGNALS.status_message.emit(text)
        except Exception:
            pass


def emit_tracked_changed():
    if SIGNALS is not None:
        try:
            SIGNALS.tracked_changed.emit()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# outage / false-positive guards
# --------------------------------------------------------------------------- #

CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)

CONFIRM_POLLS = 2

OUTAGE_FRACTION = 0.5
OUTAGE_MIN_GAMES = 3

PENDING = {}


def _pending(key):
    return PENDING.setdefault(key, {})


def _confirm_count(key, cat, item):
    d = _pending(key).setdefault(cat, {})
    d[item] = d.get(item, 0) + 1
    return d[item]


def _confirm_clear(key, cat, item):
    d = _pending(key).get(cat)
    if d:
        d.pop(item, None)


def _status_seen(key, value):
    """Bump and return how many sweeps in a row `value` has been pending."""
    p = _pending(key)
    cur = p.get("status")
    if cur and cur[0] == value:
        cur[1] += 1
    else:
        cur = [value, 1]
        p["status"] = cur
    return cur[1]


def _status_clear(key):
    _pending(key).pop("status", None)


def _drop_pending(key):
    PENDING.pop(key, None)


# --------------------------------------------------------------------------- #
# polling
# --------------------------------------------------------------------------- #

async def poll_once(session):
    async with POLL_LOCK:
        tracked = load_tracked()
        if not tracked:
            emit_status("no games tracked yet")
            return

        updated_state = {}
        sweep_events = []
        total = 0
        bad = 0

        for key, info in list(tracked.items()):
            if not isinstance(info, dict):
                continue
            total += 1
            try:
                universe_id = info["universe_id"]
                last_updated = info.get("last_updated")
                last_status = info.get("last_status", "")
                cached_name = info.get("game_name", "unknown game")
                cached_root = info.get("root_place_id")
                icon_url = info.get("icon_url")
                icon_path = info.get("icon_path")

                games_list = await fetch_game(session, universe_id)
                if games_list is None:
                    bad += 1
                    continue

                if not games_list:
                    if _confirm_count(key, "empty", "_") < CONFIRM_POLLS:
                        bad += 1
                        continue
                    current_status = "private/hidden"
                    current_updated = last_updated
                    root_place_id = cached_root
                    game_name = recover_name(info, cached_name, cached_root)
                elif is_placeholder_name(games_list[0].get("name")):
                    _confirm_clear(key, "empty", "_")
                    gd = games_list[0]
                    current_updated = gd.get("updated") or last_updated
                    root_place_id = gd.get("rootPlaceId", cached_root)
                    current_status = "private/hidden"
                    game_name = recover_name(info, cached_name, root_place_id)
                else:
                    _confirm_clear(key, "empty", "_")
                    gd = games_list[0]
                    current_updated = gd.get("updated")
                    current_status = await get_status(session, universe_id, gd, last_status or "unknown")
                    game_name = gd.get("name") or (cached_name or "unknown game")
                    root_place_id = gd.get("rootPlaceId", cached_root)

                game_url = f"https://www.roblox.com/games/{root_place_id}" if root_place_id else None
                universe_changed = bool(
                    last_updated and current_updated and is_real_update(last_updated, current_updated)
                )

                if not icon_url and current_status != "private/hidden":
                    icon_url = await fetch_icon_url(session, universe_id)
                if icon_url and not icon_path:
                    icon_path = await ensure_icon_file(session, key, icon_url)

                notify_cfg = get_notify(info)
                new_info = dict(info)

                committed_status = current_status
                if last_status and current_status != last_status:
                    if _status_seen(key, current_status) >= CONFIRM_POLLS:
                        _status_clear(key)
                        if notify_cfg["status_change"]:
                            if current_status == "private/hidden":
                                styp = "status_down"
                            elif last_status == "private/hidden":
                                styp = "status_up"
                            else:
                                styp = "status"
                            sweep_events.append({
                                "type": styp, "universe_id": str(key), "game_name": game_name,
                                "text": f"status: {last_status} → {current_status}",
                                "lines": [f"status: {last_status}", f"now: {current_status}"],
                                "url": game_url, "icon_path": icon_path, "ts": int(time.time()),
                            })
                    else:
                        committed_status = last_status
                else:
                    _status_clear(key)

                last_subplaces = info.get("subplaces", {})
                current_subplaces = dict(last_subplaces)
                updated_place_names, added_place_names, deleted_place_names = [], [], []

                if current_status != "private/hidden":
                    places = await fetch_all_places(session, universe_id)
                    if places is not None:
                        sem = asyncio.Semaphore(5)

                        async def get_sub(p):
                            pid = str(p.get("id"))
                            async with sem:
                                uts = await fetch_place_updated(session, pid)
                            return pid, p.get("name"), uts

                        results = await asyncio.gather(*[get_sub(p) for p in places])
                        current_subplaces = {}

                        for pid, pname, updated_ts in results:
                            old_ts = last_subplaces.get(pid, {}).get("updated") if pid in last_subplaces else None
                            if not updated_ts:
                                updated_ts = old_ts
                            if old_ts and updated_ts and get_unix_ts(updated_ts) < get_unix_ts(old_ts):
                                updated_ts = old_ts
                            current_subplaces[pid] = {"name": pname, "updated": updated_ts}

                            if pid in last_subplaces:
                                if (old_ts and updated_ts and is_real_update(old_ts, updated_ts)
                                        and str(root_place_id) != pid):
                                    updated_place_names.append(pname)
                            elif last_subplaces:
                                added_place_names.append(pname)

                        for old_pid, old_sp in last_subplaces.items():
                            if old_pid in current_subplaces:
                                _confirm_clear(key, "del_sub", old_pid)
                            elif _confirm_count(key, "del_sub", old_pid) >= CONFIRM_POLLS:
                                deleted_place_names.append(old_sp.get("name") or "(unnamed)")
                            else:
                                current_subplaces[old_pid] = old_sp

                upd_lines = []
                if notify_cfg["game_update"] and universe_changed:
                    upd_lines.append("game updated")
                if notify_cfg["subplace_update"]:
                    upd_lines += [f"updated: {nm}" for nm in updated_place_names]
                if upd_lines:
                    toast_lines = upd_lines[:3]
                    if current_updated:
                        toast_lines.append(f"new: {human_ts(current_updated)} | {current_status}")
                    sweep_events.append({
                        "type": "update", "universe_id": str(key), "game_name": game_name,
                        "text": "  •  ".join(upd_lines), "lines": toast_lines,
                        "url": game_url, "icon_path": icon_path, "ts": int(time.time()),
                    })
                    logger.info("update %s: %s", game_name, upd_lines)

                if notify_cfg["subplace_add"] and added_place_names:
                    al = [f"added: {nm}" for nm in added_place_names]
                    sweep_events.append({
                        "type": "added", "universe_id": str(key), "game_name": game_name,
                        "text": "  •  ".join(al), "lines": al[:3],
                        "url": game_url, "icon_path": icon_path, "ts": int(time.time()),
                    })

                if notify_cfg["subplace_delete"] and deleted_place_names:
                    dl = [f"deleted: {nm}" for nm in deleted_place_names]
                    sweep_events.append({
                        "type": "removed", "universe_id": str(key), "game_name": game_name,
                        "text": "  •  ".join(dl), "lines": dl[:3],
                        "url": game_url, "icon_path": icon_path, "ts": int(time.time()),
                    })

                need_badges = (notify_cfg["badge_new"] or notify_cfg["badge_delete"]
                               or notify_cfg["badge_rare_award"])
                if need_badges and current_status != "private/hidden":
                    all_badges = await fetch_all_badges(session, universe_id)
                    if all_badges is not None:
                        last_badges = info.get("badges", {})
                        full_snap = info.get("badges_full_snapshot", False)
                        current_badges = {}
                        for b in all_badges:
                            bid = str(b.get("id"))
                            stats = b.get("statistics") or {}
                            awarded = stats.get("awardedCount", 0) or 0
                            bname = b.get("name") or "unknown badge"
                            badge_url = f"https://www.roblox.com/badges/{bid}"
                            old = last_badges.get(bid)
                            if old is None:
                                current_badges[bid] = {"name": bname, "awarded_count": awarded}
                                if full_snap and notify_cfg["badge_new"]:
                                    bicon = await ensure_image_file(
                                        session, f"badge_{bid}", await fetch_badge_icon(session, bid), force=True)
                                    sweep_events.append({
                                        "type": "badge", "universe_id": str(key), "game_name": game_name,
                                        "text": f"new badge: {bname}", "owners": awarded,
                                        "lines": [f"new badge: {bname}", f"in {game_name}"],
                                        "url": badge_url, "icon_path": icon_path,
                                        "overlay_icon": bicon, "ts": int(time.time()),
                                    })
                            else:
                                old_count = old.get("awarded_count", 0) or 0
                                if awarded < old_count:
                                    awarded = old_count
                                current_badges[bid] = {"name": bname, "awarded_count": awarded}
                                if (full_snap and notify_cfg["badge_rare_award"]
                                        and awarded > old_count and awarded <= BADGE_RARE_THRESHOLD):
                                    delta = awarded - old_count
                                    bicon = await ensure_image_file(
                                        session, f"badge_{bid}", await fetch_badge_icon(session, bid), force=True)
                                    sweep_events.append({
                                        "type": "badge", "universe_id": str(key), "game_name": game_name,
                                        "text": f"{bname}: {old_count} → {awarded}", "owners": awarded,
                                        "lines": [bname, f"awards: {old_count} → {awarded} (+{delta})"],
                                        "url": badge_url, "icon_path": icon_path,
                                        "overlay_icon": bicon, "ts": int(time.time()),
                                    })

                        for old_bid, old_b in last_badges.items():
                            if old_bid in current_badges:
                                _confirm_clear(key, "del_badge", old_bid)
                            elif _confirm_count(key, "del_badge", old_bid) >= CONFIRM_POLLS:
                                if full_snap and notify_cfg["badge_delete"]:
                                    cached = os.path.join(ICON_DIR, f"badge_{old_bid}.png")
                                    sweep_events.append({
                                        "type": "badge_del", "universe_id": str(key), "game_name": game_name,
                                        "text": f"badge disabled: {old_b.get('name') or 'unknown badge'}",
                                        "lines": [f"badge disabled: {old_b.get('name') or 'unknown badge'}"],
                                        "url": f"https://www.roblox.com/badges/{old_bid}",
                                        "icon_path": icon_path,
                                        "overlay_icon": cached if os.path.exists(cached) else None,
                                        "ts": int(time.time()),
                                    })
                            else:
                                current_badges[old_bid] = old_b

                        new_info["badges"] = current_badges
                        new_info["badges_full_snapshot"] = True
                else:
                    new_info["badges_full_snapshot"] = False

                if get_unix_ts(current_updated) > get_unix_ts(last_updated or ""):
                    new_info["last_updated"] = current_updated
                new_info["last_status"] = committed_status
                new_info["game_name"] = game_name
                if root_place_id:
                    new_info["root_place_id"] = root_place_id
                if icon_url:
                    new_info["icon_url"] = icon_url
                if icon_path:
                    new_info["icon_path"] = icon_path
                new_info["subplaces"] = current_subplaces
                updated_state[key] = new_info

            except Exception:
                logger.exception("error checking game %s", key)

        if total >= OUTAGE_MIN_GAMES and bad >= total * OUTAGE_FRACTION:
            logger.warning("possible roblox api outage: %d/%d games bad - dropping %d alert(s)",
                           bad, total, len(sweep_events))
            for k in list(tracked):
                _drop_pending(k)
            emit_status("roblox api looks down — alerts paused")
            return

        for ev in sweep_events:
            push_event(ev)

        fresh = load_tracked()
        changed = False
        for k, v in updated_state.items():
            if k in fresh:
                fresh[k] = v
                changed = True
        save_tracked(fresh)
        if changed:
            emit_tracked_changed()
        emit_status(f"last checked {datetime.now().strftime('%I:%M %p').lower()}")


async def add_game(place_input):
    async with POLL_LOCK:
        place_id = parse_place_id(place_input)
        if not place_id:
            emit_status("couldn't read a place id from that")
            notify(APP_NAME, ["could not read a place id from that input"])
            return

        async with aiohttp.ClientSession(headers=UA, timeout=CLIENT_TIMEOUT) as session:
            universe_id = await resolve_universe(session, place_id)
            if not universe_id:
                emit_status(f"could not find place {place_id}")
                notify(APP_NAME, [f"could not find place {place_id}",
                                  "make sure the id is valid and the game is public"])
                return

            key = str(universe_id)
            if key in load_tracked():
                emit_status(f"already tracking {load_tracked()[key].get('game_name', 'that game')}")
                return

            games_list = await fetch_game(session, universe_id)
            if not games_list:
                emit_status("couldn't fetch that game")
                notify(APP_NAME, ["could not fetch that game (private or unavailable)"])
                return

            gd = games_list[0]
            game_name = good_game_name(gd.get("name"), "unknown game")
            root_place_id = gd.get("rootPlaceId")
            status = await get_status(session, universe_id, gd)
            icon_url = await fetch_icon_url(session, universe_id)
            icon_path = await ensure_icon_file(session, key, icon_url)

            subplaces = {}
            places = await fetch_all_places(session, universe_id)
            if places:
                sem = asyncio.Semaphore(10)

                async def init_sub(p):
                    pid = str(p.get("id"))
                    async with sem:
                        uts = await fetch_place_updated(session, pid)
                    return pid, p.get("name"), uts

                for pid, pname, uts in await asyncio.gather(*[init_sub(p) for p in places]):
                    subplaces[pid] = {"name": pname, "updated": uts}

            data = load_tracked()
            data[key] = {
                "universe_id": universe_id,
                "last_updated": gd.get("updated"),
                "last_status": status,
                "game_name": game_name,
                "root_place_id": root_place_id,
                "icon_url": icon_url,
                "icon_path": icon_path,
                "notify": dict(NOTIFY_DEFAULTS),
                "subplaces": subplaces,
            }
            save_tracked(data)

    game_url = f"https://www.roblox.com/games/{root_place_id}" if root_place_id else None
    n_sub = sum(1 for pid in subplaces if str(pid) != str(root_place_id))
    push_event({
        "type": "added",
        "universe_id": key,
        "game_name": game_name,
        "text": f"now tracking · {_plural(n_sub, 'subplace')} · {status}",
        "lines": [f"now tracking {game_name}", f"{_plural(n_sub, 'subplace')} | {status}"],
        "url": game_url,
        "icon_path": icon_path,
        "ts": int(time.time()),
    })
    emit_tracked_changed()
    emit_status(f"now tracking {game_name}")
    logger.info("added %s (%s)", game_name, key)


async def poll_now():
    emit_status("checking now…")
    async with aiohttp.ClientSession(headers=UA, timeout=CLIENT_TIMEOUT) as session:
        await poll_once(session)


async def backfill_badge_icons(session):
    """Fetch the real icon for badge events already in the feed that don't have
    one yet (captured before the badge's art finished uploading). Runs once at
    startup so old entries stop showing the medal placeholder."""
    hist = load_history()
    if not hist:
        return
    changed = False
    fetch_targets = {}
    for ev in hist:
        typ = ev.get("type")
        if typ not in ("badge", "badge_del"):
            continue
        ov = ev.get("overlay_icon")
        if ov and os.path.exists(ov):
            continue
        bid = _badge_id_from_url(ev.get("url"))
        if not bid:
            continue
        cached = os.path.join(ICON_DIR, f"badge_{bid}.png")
        if os.path.exists(cached):
            ev["overlay_icon"] = cached
            changed = True
        elif typ == "badge":
            fetch_targets.setdefault(bid, []).append(ev)
    for bid, evs in fetch_targets.items():
        try:
            url = await fetch_badge_icon(session, bid)
            path = await ensure_image_file(session, f"badge_{bid}", url, force=True) if url else None
            if path and os.path.exists(path):
                for ev in evs:
                    ev["overlay_icon"] = path
                changed = True
        except Exception:
            pass
    if changed:
        save_history(hist)
        if SIGNALS is not None:
            try:
                SIGNALS.reload_feed.emit()
            except Exception:
                pass
        logger.info("backfilled badge icons")


async def poller_main():
    global POLL_LOCK
    POLL_LOCK = asyncio.Lock()
    logger.info("poller started (every %ds)", current_poll_interval())
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=UA, timeout=CLIENT_TIMEOUT, connector=connector) as session:
        try:
            await backfill_badge_icons(session)
        except Exception:
            logger.exception("badge backfill failed")
        while not STOP.is_set():
            start = time.monotonic()
            try:
                await poll_once(session)
            except Exception:
                logger.exception("poll sweep crashed")
            while not STOP.is_set() and (time.monotonic() - start) < current_poll_interval():
                await asyncio.sleep(1)
    logger.info("poller stopped")


def poller_thread():
    global LOOP
    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)
    try:
        LOOP.run_until_complete(poller_main())
    except Exception:
        logger.exception("poller thread crashed")


def submit(coro):
    if LOOP is not None and LOOP.is_running():
        return asyncio.run_coroutine_threadsafe(coro, LOOP)
    logger.warning("loop not ready, dropping task")
    return None


# --------------------------------------------------------------------------- #
# autostart (HKCU Run key)
# --------------------------------------------------------------------------- #

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LegmanTracker"


def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def is_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except Exception:
        return False


def set_autostart(enable):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
    except Exception:
        logger.exception("failed to toggle autostart")


def register_aumid():
    """Register our AppUserModelID with Windows so toast notifications are shown
    as banners (Windows only shows banners for recognised app IDs) and carry our
    name + icon. The icon must live somewhere persistent (not the onefile temp
    dir), so copy the bundled png into the data folder first."""
    icon_path = os.path.join(DATA_DIR, "app_icon.ico")
    try:
        src = resource_path("icon.ico")
        if os.path.exists(src):
            with open(src, "rb") as f:
                data = f.read()
            with open(icon_path, "wb") as f:
                f.write(data)
    except Exception:
        icon_path = None
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\AppUserModelId\{AUMID}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            if icon_path and os.path.exists(icon_path):
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon_path)
    except Exception:
        logger.exception("failed to register AUMID")


# --------------------------------------------------------------------------- #
# app icon
# --------------------------------------------------------------------------- #

def resource_path(name):
    """Locate a bundled asset both when run from source and when frozen."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _fallback_icon(size=64):
    """Drawn 'L' badge, used only if icon.ico is somehow missing (keeps the app
    icon working without pulling in Pillow at runtime)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    pad = max(1, size // 32)
    p.setBrush(QtGui.QColor(20, 26, 34))
    p.setPen(QtGui.QPen(QtGui.QColor(0, 212, 136), max(1, size // 24)))
    p.drawEllipse(QtCore.QRectF(pad, pad, size - 2 * pad, size - 2 * pad))
    f = QtGui.QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.58))
    p.setFont(f)
    p.setPen(QtGui.QColor(122, 200, 255))
    p.drawText(pm.rect(), Qt.AlignCenter, "L")
    p.end()
    return pm


def app_icon_pixmap(size=64):
    """The legman app icon as a QPixmap (falls back to a drawn 'L' if missing).
    Uses QIcon.pixmap so the best-matching frame is picked from the multi-res
    .ico instead of upscaling a small one."""
    path = resource_path("icon.ico")
    if os.path.exists(path):
        ic = QtGui.QIcon(path)
        if not ic.isNull():
            pm = ic.pixmap(size, size)
            if not pm.isNull():
                return pm
    return _fallback_icon(size)


def app_qicon():
    path = resource_path("icon.ico")
    if os.path.exists(path):
        ic = QtGui.QIcon(path)
        if not ic.isNull():
            return ic
    return QtGui.QIcon(_fallback_icon(64))


ICON_SVGS = {
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>'
               '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "close": '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "back": '<polyline points="15 18 9 12 15 6"/>',
    "clear": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
}

def _medal_svg(color):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
        f'<circle cx="12" cy="8" r="7" stroke="{color}" stroke-width="2"/>'
        '<polygon points="12,4.7 12.82,6.87 15.14,6.98 13.33,8.43 13.94,10.67 12,9.4 '
        f'10.06,10.67 10.67,8.43 8.86,6.98 11.18,6.87" fill="{color}"/>'
        f'<polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" stroke="{color}" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )


def render_svg(svg, size):
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    scale = 2
    pm = QtGui.QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm

_SVG_WRAP = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
             'stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


_SVG_PM_CACHE = {}


def svg_pixmap(name, size=16, color="#8b98a4"):
    ck = (name, size, color)
    cached = _SVG_PM_CACHE.get(ck)
    if cached is not None:
        return cached
    svg = _SVG_WRAP.format(c=color, body=ICON_SVGS[name])
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    scale = 2
    pm = QtGui.QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(scale)
    _SVG_PM_CACHE[ck] = pm
    return pm


def svg_icon(name, size=16, color="#8b98a4"):
    return QtGui.QIcon(svg_pixmap(name, size, color))


@functools.lru_cache(maxsize=256)
def _rounded_cached(path, mtime, size, radius):
    src = QtGui.QPixmap(path)
    if src.isNull():
        return None
    src = src.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QtGui.QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QtGui.QPainter(out)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    clip = QtGui.QPainterPath()
    clip.addRoundedRect(0, 0, size, size, radius, radius)
    p.setClipPath(clip)
    p.drawPixmap((size - src.width()) // 2, (size - src.height()) // 2, src)
    p.end()
    return out


@functools.lru_cache(maxsize=256)
def _circular_cached(path, mtime, size):
    src = QtGui.QPixmap(path)
    if src.isNull():
        return None
    src = src.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QtGui.QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QtGui.QPainter(out)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    clip = QtGui.QPainterPath()
    clip.addEllipse(0, 0, size, size)
    p.setClipPath(clip)
    p.drawPixmap((size - src.width()) // 2, (size - src.height()) // 2, src)
    p.end()
    return out


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def rounded_pixmap_from_path(path, size=40, radius=10):
    if not path:
        return None
    mt = _mtime(path)
    return _rounded_cached(path, mt, size, radius) if mt is not None else None


def _circular_pixmap(path, size):
    mt = _mtime(path)
    return _circular_cached(path, mt, size) if mt is not None else None


def _clear_pixmap_caches():
    """Free cached QPixmaps before QApplication is torn down (QPixmaps destroyed
    after the GUI app crash on exit). Wired to QApplication.aboutToQuit."""
    _SVG_PM_CACHE.clear()
    _rounded_cached.cache_clear()
    _circular_cached.cache_clear()


def _medal_icon(size=38, radius=9):
    """A medal on a purple rounded square - the hero icon for a badge that has no
    artwork of its own."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    grad = QtGui.QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QtGui.QColor("#8a6bff"))
    grad.setColorAt(1, QtGui.QColor("#5b3fd9"))
    path = QtGui.QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    p.fillPath(path, grad)
    g = max(10, int(size * 0.66))
    p.drawPixmap((size - g) // 2, (size - g) // 2, render_svg(_medal_svg("#ffffff"), g))
    p.end()
    return pm


def _compose_icons(base_pm, corner_pm, size=38):
    """Big base icon with a small circular corner icon (bottom-right)."""
    if base_pm is None:
        return corner_pm
    if corner_pm is None:
        return base_pm
    osz = corner_pm.width()
    res = QtGui.QPixmap(size, size)
    res.fill(Qt.transparent)
    p = QtGui.QPainter(res)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.drawPixmap(0, 0, base_pm)
    bx, by = size - osz, size - osz
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor(16, 22, 28))
    p.drawEllipse(bx - 2, by - 2, osz + 3, osz + 3)
    p.drawPixmap(bx, by, corner_pm)
    p.end()
    return res


def feed_card_icon(event, size=38, radius=9):
    """Builds the card thumbnail. For badge events the BADGE is the hero (its art
    or a medal) with the game icon in the corner; everything else just shows the
    game/place icon."""
    typ = event.get("type", "update")
    game_path = event.get("icon_path")
    if typ in ("badge", "badge_del"):
        badge_path = event.get("overlay_icon")
        if badge_path and os.path.exists(badge_path):
            base = rounded_pixmap_from_path(badge_path, size, radius)
        else:
            base = _medal_icon(size, radius)
        corner = None
        if game_path and os.path.exists(game_path):
            corner = _circular_pixmap(game_path, int(size * 0.5))
        return _compose_icons(base, corner, size)
    return rounded_pixmap_from_path(game_path, size, radius)


# --------------------------------------------------------------------------- #
# qt: thread bridge
# --------------------------------------------------------------------------- #

class Signals(QtCore.QObject):
    update_event = QtCore.Signal(dict)
    tracked_changed = QtCore.Signal()
    status_message = QtCore.Signal(str)
    reload_feed = QtCore.Signal()


# --------------------------------------------------------------------------- #
# qt: styling
# --------------------------------------------------------------------------- #

STYLESHEET = """
#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #15212c, stop:1 #0c141b);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 16px;
}
QLabel { color: #e6edf3; }
#title { color: #f0f4f8; font-size: 13px; font-weight: 700; }
#caption { color: #5d6b76; font-size: 10px; font-weight: 700; }
#status { color: #6b7884; font-size: 11px; }
#empty { color: #58656f; font-size: 12px; }

#iconBtn {
    background: transparent; border: none; border-radius: 8px;
    color: #8b98a4; font-size: 15px; padding: 4px;
}
#iconBtn:hover { background: rgba(255,255,255,0.07); color: #e6edf3; }

#tabBtn {
    background: transparent; border: none; border-radius: 9px;
    color: #7d8b97; font-size: 12px; font-weight: 600; padding: 6px 14px;
}
#tabBtn:hover { color: #cdd8e0; }
#tabBtn:checked { background: rgba(0,212,136,0.14); color: #00d488; }

#updCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #18242f, stop:1 #121b23);
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 12px;
}
#updCard:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1d2b38, stop:1 #16212b);
    border: 1px solid rgba(0,212,136,0.28);
}
#cardName { color: #eaf1f6; font-size: 12px; font-weight: 600; }
#cardDetail { color: #8a98a4; font-size: 11px; }
#cardTime { color: #5f6d78; font-size: 10px; }

#trackName { color: #eaf1f6; font-size: 12px; font-weight: 600; }
#trackMeta { color: #7a8893; font-size: 10px; }
#removeBtn {
    background: transparent; border: none; border-radius: 7px;
    color: #6f7c87; font-size: 13px;
}
#removeBtn:hover { background: rgba(232,80,80,0.16); color: #f06a6a; }
#cogBtn {
    background: transparent; border: none; border-radius: 7px;
    color: #6f7c87; font-size: 14px;
}
#cogBtn:hover { background: rgba(255,255,255,0.08); color: #cdd8e0; }

#setLabel { color: #e6edf3; font-size: 12px; font-weight: 600; }
#setDesc { color: #6f7d88; font-size: 10px; }
#setTitle { color: #f0f4f8; font-size: 12px; font-weight: 700; }
#backBtn {
    background: transparent; border: none; border-radius: 7px;
    color: #8b98a4; font-size: 13px; font-weight: 600; padding: 4px 8px;
}
#backBtn:hover { background: rgba(255,255,255,0.07); color: #e6edf3; }
#clearBtn {
    background: transparent; border: none; border-radius: 9px;
    color: #7d8b97; font-size: 12px; font-weight: 600; padding: 6px 12px;
}
#clearBtn:hover { background: rgba(255,255,255,0.07); color: #cdd8e0; }
#chip {
    background: #16212b; border: 1px solid rgba(255,255,255,0.06); border-radius: 9px;
    color: #9aa7b2; font-size: 12px; font-weight: 600; padding: 6px 0px;
}
#chip:hover { color: #cdd8e0; }
#chip:checked {
    background: rgba(0,212,136,0.16); color: #00d488; border: 1px solid rgba(0,212,136,0.35);
}

#addInput {
    background: #0c141c; border: 1px solid #233140; border-radius: 10px;
    color: #e6edf3; font-size: 12px; padding: 9px 12px;
    selection-background-color: #00d488;
}
#addInput:focus { border: 1px solid #00d488; }
#addBtn {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #00d488, stop:1 #00b372);
    border: none; border-radius: 10px; color: #04231a;
    font-size: 12px; font-weight: 700; padding: 9px 16px;
}
#addBtn:hover { background: #12e095; }
#addBtn:pressed { background: #00b372; }

QScrollArea { background: transparent; border: none; }
#scrollInner { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.14); border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.24); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QMenu {
    background: #15212c; color: #e6edf3; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 6px;
}
QMenu::item { padding: 7px 22px 7px 14px; border-radius: 6px; font-size: 12px; }
QMenu::item:selected { background: rgba(0,212,136,0.16); }
QMenu::separator { height: 1px; background: rgba(255,255,255,0.08); margin: 5px 8px; }
QMenu::indicator { width: 14px; height: 14px; }
"""


# --------------------------------------------------------------------------- #
# qt: widgets
# --------------------------------------------------------------------------- #

class ClickableFrame(QtWidgets.QFrame):
    def __init__(self, on_click=None):
        super().__init__()
        self._on_click = on_click
        if on_click:
            self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if self._on_click and e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self._on_click()
        super().mouseReleaseEvent(e)


class ElidedLabel(QtWidgets.QLabel):
    """Single-line label that elides with '…' instead of clipping mid-word.
    Colour is passed explicitly (a custom paintEvent doesn't inherit the QSS
    `color`); font size/weight still come from the stylesheet via objectName."""
    def __init__(self, text, color, parent=None):
        super().__init__(text, parent)
        self._color = QtGui.QColor(color)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)

    def setText(self, t):
        super().setText(t)
        self.update()

    def minimumSizeHint(self):
        return QtCore.QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return QtCore.QSize(0, super().sizeHint().height())

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setClipRect(self.rect())
        p.setFont(self.font())
        p.setPen(self._color)
        fm = self.fontMetrics()
        elided = fm.elidedText(self.text() or "", Qt.ElideRight, self.width())
        p.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter), elided)


class ToggleSwitch(QtWidgets.QAbstractButton):
    """Small iOS-style on/off switch (green when on)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._w, self._h = 40, 23
        self.setFixedSize(self._w, self._h)

    def sizeHint(self):
        return QtCore.QSize(self._w, self._h)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        on = self.isChecked()
        track = QtGui.QColor("#00d488") if on else QtGui.QColor("#2a3742")
        p.setBrush(track)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, self._w, self._h, self._h / 2, self._h / 2)
        d = self._h - 6
        x = (self._w - d - 3) if on else 3
        p.setBrush(QtGui.QColor("#06231a") if on else QtGui.QColor("#aab6c0"))
        p.drawEllipse(int(x), 3, d, d)


def make_toggle_row(label, desc, checked, on_change):
    row = QtWidgets.QFrame()
    row.setObjectName("updCard")
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(12, 9, 12, 9)
    lay.setSpacing(10)
    col = QtWidgets.QVBoxLayout()
    col.setSpacing(2)
    name = QtWidgets.QLabel(label)
    name.setObjectName("setLabel")
    col.addWidget(name)
    if desc:
        d = QtWidgets.QLabel(desc)
        d.setObjectName("setDesc")
        col.addWidget(d)
    lay.addLayout(col, 1)
    sw = ToggleSwitch()
    sw.setChecked(bool(checked))
    sw.toggled.connect(on_change)
    lay.addWidget(sw, 0, Qt.AlignVCenter)
    return row


def make_update_card(event):
    typ = event.get("type", "update")
    accent = ACCENT.get(typ, ACCENT["update"])
    url = event.get("url")

    card = ClickableFrame(on_click=(lambda: webbrowser.open(url)) if url else None)
    card.setObjectName("updCard")
    lay = QtWidgets.QHBoxLayout(card)
    lay.setContentsMargins(10, 9, 14, 9)
    lay.setSpacing(10)

    bar = QtWidgets.QFrame()
    bar.setFixedWidth(3)
    bar.setStyleSheet(f"background:{accent}; border-radius:2px;")
    lay.addWidget(bar)

    pm = feed_card_icon(event, size=38, radius=9)
    if pm is not None:
        icon = QtWidgets.QLabel()
        icon.setFixedSize(38, 38)
        icon.setPixmap(pm)
        lay.addWidget(icon)

    mid = QtWidgets.QVBoxLayout()
    mid.setSpacing(2)
    top = QtWidgets.QHBoxLayout()
    top.setSpacing(8)
    name = ElidedLabel(event.get("game_name", "unknown game"), "#eaf1f6")
    name.setObjectName("cardName")
    t = QtWidgets.QLabel(rel_time(event.get("ts", 0)))
    t.setObjectName("cardTime")
    t.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    card._time_label = t
    card._ts = event.get("ts", 0)
    top.addWidget(name, 1)
    top.addWidget(t, 0)
    mid.addLayout(top)
    detail_row = QtWidgets.QHBoxLayout()
    detail_row.setSpacing(6)
    detail = ElidedLabel(event.get("text", ""), "#8a98a4")
    detail.setObjectName("cardDetail")
    detail_row.addWidget(detail, 1)
    if event.get("owners") is not None:
        detail_row.addWidget(_owners_chip(event["owners"]), 0)
    mid.addLayout(detail_row)
    lay.addLayout(mid, 1)
    return card


def _owners_chip(count):
    """A small 'N owners' indicator: person icon + count (replaces the word)."""
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(3)
    icon = QtWidgets.QLabel()
    icon.setPixmap(svg_pixmap("user", 12, "#8a98a4"))
    icon.setFixedSize(12, 12)
    num = QtWidgets.QLabel(str(count))
    num.setObjectName("cardDetail")
    h.addWidget(icon)
    h.addWidget(num)
    w.setToolTip(f"{count} owners")
    return w


def make_tracked_row(key, info, on_remove, on_open, on_settings):
    name = info.get("game_name", "unknown game")
    status = info.get("last_status", "?")
    nsub = subplace_count(info)
    on_count = sum(1 for v in get_notify(info).values() if v)
    url = None
    root = info.get("root_place_id")
    if root:
        url = f"https://www.roblox.com/games/{root}"

    row = ClickableFrame(on_click=(lambda: on_open(url)) if url else None)
    row.setObjectName("updCard")
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(10, 8, 6, 8)
    lay.setSpacing(8)

    pm = rounded_pixmap_from_path(info.get("icon_path"), size=36, radius=9)
    if pm is not None:
        icon = QtWidgets.QLabel()
        icon.setFixedSize(36, 36)
        icon.setPixmap(pm)
        lay.addWidget(icon)

    col = QtWidgets.QVBoxLayout()
    col.setSpacing(2)
    nlbl = ElidedLabel(name, "#eaf1f6")
    nlbl.setObjectName("trackName")
    meta = ElidedLabel(f"{status} · {_plural(nsub, 'subplace')} · {_plural(on_count, 'alert')}", "#7a8893")
    meta.setObjectName("trackMeta")
    col.addWidget(nlbl)
    col.addWidget(meta)
    lay.addLayout(col, 1)

    cog = QtWidgets.QPushButton()
    cog.setObjectName("cogBtn")
    cog.setCursor(Qt.PointingHandCursor)
    cog.setFixedSize(28, 28)
    cog.setIcon(svg_icon("settings", 15, "#8b98a4"))
    cog.setIconSize(QtCore.QSize(15, 15))
    cog.setToolTip("choose what to track")
    cog.clicked.connect(lambda: on_settings(key))
    lay.addWidget(cog, 0, Qt.AlignVCenter)

    rm = QtWidgets.QPushButton()
    rm.setObjectName("removeBtn")
    rm.setCursor(Qt.PointingHandCursor)
    rm.setFixedSize(28, 28)
    rm.setIcon(svg_icon("trash", 15, "#7a8893"))
    rm.setIconSize(QtCore.QSize(15, 15))
    rm.setToolTip("stop tracking")
    rm.clicked.connect(lambda: on_remove(key, name))
    lay.addWidget(rm, 0, Qt.AlignVCenter)
    return row


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


class HintFilter(QtCore.QObject):
    """Redirects widget tooltips to the popup's status line instead of using
    native QToolTip windows (which stutter over a translucent always-on-top
    window). Reads each widget's existing setToolTip() text."""
    def __init__(self, popup):
        super().__init__(popup)
        self.popup = popup
        self._active = None

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QtCore.QEvent.Enter:
            tip = obj.toolTip() if hasattr(obj, "toolTip") else ""
            if tip:
                self.popup.show_hint(tip)
                self._active = obj
        elif et == QtCore.QEvent.Leave and obj is self._active:
            self.popup.clear_hint()
            self._active = None
        elif et == QtCore.QEvent.ToolTip:
            return True
        return False


class PopupWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(384)
        self._last_hide = 0.0
        self._base_status = ""
        self._hovering = False
        self._loaded = False
        self._tracked_dirty = False
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)

        card = QtWidgets.QFrame()
        card.setObjectName("card")
        self.card = card
        outer.addWidget(card)

        v = QtWidgets.QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(9)
        logo = QtWidgets.QLabel()
        logo.setPixmap(app_icon_pixmap(26))
        logo.setFixedSize(26, 26)
        header.addWidget(logo)
        title = QtWidgets.QLabel("LEGMAN TRACKER")
        title.setObjectName("title")
        f = title.font()
        f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.6)
        title.setFont(f)
        header.addWidget(title)
        header.addStretch(1)
        settings_btn = QtWidgets.QPushButton()
        settings_btn.setObjectName("iconBtn")
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.setFixedSize(28, 28)
        settings_btn.setIcon(svg_icon("settings", 15, "#8b98a4"))
        settings_btn.setIconSize(QtCore.QSize(15, 15))
        settings_btn.setToolTip("settings")
        settings_btn.clicked.connect(self.open_app_settings)
        header.addWidget(settings_btn)
        refresh = QtWidgets.QPushButton()
        refresh.setObjectName("iconBtn")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setFixedSize(28, 28)
        refresh.setIcon(svg_icon("refresh", 15, "#8b98a4"))
        refresh.setIconSize(QtCore.QSize(15, 15))
        refresh.setToolTip("check all games now")
        refresh.clicked.connect(lambda: submit(poll_now()))
        header.addWidget(refresh)
        closeb = QtWidgets.QPushButton()
        closeb.setObjectName("iconBtn")
        closeb.setCursor(Qt.PointingHandCursor)
        closeb.setFixedSize(28, 28)
        closeb.setIcon(svg_icon("close", 15, "#8b98a4"))
        closeb.setIconSize(QtCore.QSize(15, 15))
        closeb.setToolTip("close")
        closeb.clicked.connect(self.hide)
        header.addWidget(closeb)
        v.addLayout(header)

        tabs = QtWidgets.QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_recent = QtWidgets.QPushButton("recent updates")
        self.tab_tracked = QtWidgets.QPushButton("tracked games")
        for b in (self.tab_recent, self.tab_tracked):
            b.setObjectName("tabBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
        self.tab_recent.setChecked(True)
        grp = QtWidgets.QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self.tab_recent)
        grp.addButton(self.tab_tracked)
        self.tab_recent.clicked.connect(self._show_recent)
        self.tab_tracked.clicked.connect(self._show_tracked)
        tabs.addWidget(self.tab_recent)
        tabs.addWidget(self.tab_tracked)
        tabs.addStretch(1)
        self.clear_btn = QtWidgets.QPushButton(" clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setIcon(svg_icon("clear", 13, "#7d8b97"))
        self.clear_btn.setIconSize(QtCore.QSize(13, 13))
        self.clear_btn.setToolTip("clear recent updates")
        self.clear_btn.clicked.connect(self._clear_history)
        tabs.addWidget(self.clear_btn)
        v.addLayout(tabs)

        self.stack = QtWidgets.QStackedWidget()
        self.recent_inner, self.recent_lay, recent_scroll = self._make_scroll_page()
        self.tracked_inner, self.tracked_lay, tracked_scroll = self._make_scroll_page()
        self.settings_inner, self.settings_lay, settings_scroll = self._make_scroll_page()
        self.app_inner, self.app_lay, app_scroll = self._make_scroll_page()
        self.stack.addWidget(recent_scroll)
        self.stack.addWidget(tracked_scroll)
        self.stack.addWidget(settings_scroll)
        self.stack.addWidget(app_scroll)
        self.stack.setFixedHeight(312)
        v.addWidget(self.stack)

        addrow = QtWidgets.QHBoxLayout()
        addrow.setSpacing(8)
        self.add_input = QtWidgets.QLineEdit()
        self.add_input.setObjectName("addInput")
        self.add_input.setPlaceholderText("place id or roblox game url")
        self.add_input.returnPressed.connect(self._do_add)
        addb = QtWidgets.QPushButton("add")
        addb.setObjectName("addBtn")
        addb.setCursor(Qt.PointingHandCursor)
        addb.clicked.connect(self._do_add)
        addrow.addWidget(self.add_input, 1)
        addrow.addWidget(addb)
        v.addLayout(addrow)

        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("status")
        v.addWidget(self.status)


        self._time_timer = QtCore.QTimer(self)
        self._time_timer.setInterval(20000)
        self._time_timer.timeout.connect(self._tick_times)

    def _tick_times(self):
        for i in range(self.recent_lay.count()):
            w = self.recent_lay.itemAt(i).widget()
            if w is not None and hasattr(w, "_time_label"):
                w._time_label.setText(rel_time(w._ts))

    def _make_scroll_page(self):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        inner.setObjectName("scrollInner")
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(7)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return inner, lay, scroll

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        for event in reversed(load_history()):
            self._add_card(event)
        self._refresh_empty(self.recent_lay, "no updates yet — they'll show up here")
        self.refresh_tracked()

    @staticmethod
    def _drop_item(item):
        if item is not None and item.widget() is not None:
            w = item.widget()
            w.setParent(None)
            w.deleteLater()

    def _add_card(self, event):
        self._strip_empty(self.recent_lay)
        card = make_update_card(event)
        self.recent_lay.insertWidget(0, card)
        while self.recent_lay.count() - 1 > HISTORY_MAX:
            self._drop_item(self.recent_lay.takeAt(self.recent_lay.count() - 2))

    def on_update_event(self, event):
        if self._loaded:
            self._add_card(event)

    def reload_recent(self):
        if not self._loaded:
            return
        while self.recent_lay.count() > 1:
            self._drop_item(self.recent_lay.takeAt(0))
        for event in reversed(load_history()):
            self._add_card(event)
        self._refresh_empty(self.recent_lay, "no updates yet — they'll show up here")

    def refresh_tracked(self):
        if not self._loaded:
            return
        if not POPUP_VISIBLE:
            self._tracked_dirty = True
            return
        self._tracked_dirty = False
        while self.tracked_lay.count() > 1:
            self._drop_item(self.tracked_lay.takeAt(0))
        data = load_tracked()
        if not data:
            self._refresh_empty(self.tracked_lay, "no games yet — add one below")
            return
        for key, info in data.items():
            row = make_tracked_row(key, info, self._do_remove, self._open_url, self.open_settings)
            self.tracked_lay.insertWidget(self.tracked_lay.count() - 1, row)

    def open_settings(self, key):
        data = load_tracked()
        info = data.get(key)
        if not info:
            return
        self._settings_key = key
        while self.settings_lay.count() > 1:
            self._drop_item(self.settings_lay.takeAt(0))

        head = QtWidgets.QFrame()
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 2)
        hl.setSpacing(6)
        back = QtWidgets.QPushButton("  back")
        back.setObjectName("backBtn")
        back.setCursor(Qt.PointingHandCursor)
        back.setIcon(svg_icon("back", 13, "#8b98a4"))
        back.setIconSize(QtCore.QSize(13, 13))
        back.clicked.connect(self._back_to_tracked)
        hl.addWidget(back, 0)
        title = ElidedLabel(info.get("game_name", "unknown game"), "#f0f4f8")
        title.setObjectName("setTitle")
        hl.addWidget(title, 1)
        self.settings_lay.insertWidget(self.settings_lay.count() - 1, head)

        notify_cfg = get_notify(info)
        for opt, label, desc in NOTIFY_CATEGORIES:
            row = make_toggle_row(label, desc, notify_cfg[opt], self._make_toggle_handler(key, opt))
            self.settings_lay.insertWidget(self.settings_lay.count() - 1, row)

        self.clear_btn.setVisible(False)
        self.stack.setCurrentIndex(2)

    def _make_toggle_handler(self, key, opt):
        def handler(value):
            set_notify_option(key, opt, value)
            self.set_status(f"{'tracking' if value else 'ignoring'} {opt.replace('_', ' ')}")
        return handler

    def _back_to_tracked(self):
        self.refresh_tracked()
        self.tab_tracked.setChecked(True)
        self.clear_btn.setVisible(False)
        self.stack.setCurrentIndex(1)

    def open_app_settings(self):
        while self.app_lay.count() > 1:
            self._drop_item(self.app_lay.takeAt(0))

        head = QtWidgets.QFrame()
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 2)
        hl.setSpacing(6)
        back = QtWidgets.QPushButton("  back")
        back.setObjectName("backBtn")
        back.setCursor(Qt.PointingHandCursor)
        back.setIcon(svg_icon("back", 13, "#8b98a4"))
        back.setIconSize(QtCore.QSize(13, 13))
        back.clicked.connect(self._back_to_recent)
        hl.addWidget(back, 0)
        title = QtWidgets.QLabel("settings")
        title.setObjectName("setTitle")
        hl.addWidget(title, 1)
        self.app_lay.insertWidget(self.app_lay.count() - 1, head)

        cap = QtWidgets.QLabel("CHECK INTERVAL")
        cap.setObjectName("caption")
        self.app_lay.insertWidget(self.app_lay.count() - 1, cap)

        chips = QtWidgets.QFrame()
        ch = QtWidgets.QHBoxLayout(chips)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(6)
        grp = QtWidgets.QButtonGroup(chips)
        grp.setExclusive(True)
        cur = current_poll_interval()
        for label, secs in (("30s", 30), ("1m", 60), ("2m", 120), ("5m", 300), ("10m", 600)):
            b = QtWidgets.QPushButton(label)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            if secs == cur:
                b.setChecked(True)
            b.clicked.connect(lambda _=False, s=secs, l=label: self._set_interval(s, l))
            grp.addButton(b)
            ch.addWidget(b, 1)
        self.app_lay.insertWidget(self.app_lay.count() - 1, chips)

        hint = QtWidgets.QLabel("how often it checks your games for updates")
        hint.setObjectName("setDesc")
        self.app_lay.insertWidget(self.app_lay.count() - 1, hint)

        auto = make_toggle_row("start with windows", "launch automatically when you log in",
                               is_autostart(), lambda v: set_autostart(v))
        self.app_lay.insertWidget(self.app_lay.count() - 1, auto)

        self.clear_btn.setVisible(False)
        self.stack.setCurrentIndex(3)

    def _set_interval(self, secs, label):
        set_poll_interval(secs)
        self.set_status(f"now checking every {label}")

    def _back_to_recent(self):
        self.tab_recent.setChecked(True)
        self.clear_btn.setVisible(True)
        self.stack.setCurrentIndex(0)

    def _strip_empty(self, lay):
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None and w.objectName() == "empty":
                self._drop_item(lay.takeAt(i))
                return

    def _refresh_empty(self, lay, text):
        has_row = any(lay.itemAt(i).widget() is not None for i in range(lay.count()))
        if has_row:
            return
        lbl = QtWidgets.QLabel(text)
        lbl.setObjectName("empty")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lay.insertWidget(0, lbl)

    def _do_add(self):
        txt = self.add_input.text().strip()
        if not txt:
            return
        self.add_input.clear()
        self.set_status("adding…")
        submit(add_game(txt))

    def _do_remove(self, key, name):
        removed = remove_tracked(key)
        self.refresh_tracked()
        self.set_status(f"stopped tracking {removed or name}")
        push_event({
            "type": "removed", "universe_id": key, "game_name": removed or name,
            "text": "stopped tracking", "lines": [f"stopped tracking {removed or name}"],
            "url": None, "icon_path": None, "ts": int(time.time()),
        }, toast=False)

    def _open_url(self, url):
        if url:
            webbrowser.open(url)

    def _show_recent(self):
        self.stack.setCurrentIndex(0)
        self.clear_btn.setVisible(True)

    def _show_tracked(self):
        self.refresh_tracked()
        self.clear_btn.setVisible(False)
        self.stack.setCurrentIndex(1)

    def _clear_history(self):
        clear_history()
        while self.recent_lay.count() > 1:
            self._drop_item(self.recent_lay.takeAt(0))
        self._refresh_empty(self.recent_lay, "no updates yet — they'll show up here")
        self.set_status("cleared recent updates")

    def set_status(self, text):
        self._base_status = text
        if not self._hovering:
            self.status.setText(text)

    def show_hint(self, text):
        self._hovering = True
        self.status.setText(text)

    def clear_hint(self):
        self._hovering = False
        self.status.setText(self._base_status)

    def show_at_tray(self):
        self.adjustSize()
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        margin = 12
        x = scr.right() - self.width() - margin
        y = scr.bottom() - self.height() - margin
        self.move(max(scr.left() + margin, x), max(scr.top() + margin, y))
        self.show()
        self.raise_()
        self.activateWindow()
        self.add_input.setFocus()

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            if time.monotonic() - self._last_hide < 0.3:
                return
            self.show_at_tray()

    def event(self, e):
        if e.type() == QtCore.QEvent.WindowDeactivate:
            self._last_hide = time.monotonic()
            self.hide()
        return super().event(e)

    def showEvent(self, e):
        global POPUP_VISIBLE
        POPUP_VISIBLE = True
        self._ensure_loaded()
        if self._tracked_dirty:
            self.refresh_tracked()
        self._tick_times()
        self._time_timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        global POPUP_VISIBLE
        POPUP_VISIBLE = False
        self._time_timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _):
        card = getattr(self, "card", None)
        if card is None:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        base = QtCore.QRectF(card.geometry()).translated(0, 5)
        layers = 13
        for i in range(layers, 0, -1):
            p.setBrush(QtGui.QColor(0, 0, 0, 7))
            p.drawRoundedRect(base.adjusted(-i, -i, i, i), 16 + i, 16 + i)


# --------------------------------------------------------------------------- #
# startup
# --------------------------------------------------------------------------- #

def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)


def run_selftest():
    setup_logging()
    global TOASTS_ON
    result_path = os.path.join(DATA_DIR, "selftest_result.txt")
    try:
        qt_ok = False
        svg_ok = False
        try:
            _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            _app.setStyleSheet(STYLESHEET)
            pm = svg_pixmap("settings", 16)
            svg_ok = not pm.isNull()
            _ = PopupWindow()
            qt_ok = True
        except Exception:
            logger.exception("qt self-test failed")
        register_aumid()
        TOASTS_ON = True
        notify(APP_NAME, ["self-test OK", "notifications are working"])
        time.sleep(6)
        icon_ok = os.path.exists(resource_path("icon.ico"))
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(f"OK\ntoast=yes\nqt={'yes' if qt_ok else 'NO'}\n"
                    f"svg={'yes' if svg_ok else 'NO'}\nicon={'yes' if icon_ok else 'NO'}\n")
        logger.info("selftest OK (qt=%s svg=%s icon=%s)", qt_ok, svg_ok, icon_ok)
        return 0
    except Exception:
        logger.exception("selftest failed")
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                f.write("FAIL\n" + traceback.format_exc())
        except Exception:
            pass
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())

    setup_logging()
    os.makedirs(ICON_DIR, exist_ok=True)
    _migrate_badge_cache()
    load_poll_interval()
    logger.info("=== %s starting ===", APP_NAME)
    logger.info("data dir: %s", DATA_DIR)

    global TOASTS_ON, SIGNALS

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_qicon())
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLESHEET)
    app.aboutToQuit.connect(_clear_pixmap_caches)

    register_aumid()
    TOASTS_ON = True
    SIGNALS = Signals()

    popup = PopupWindow()
    popup._hint_filter = HintFilter(popup)
    app.installEventFilter(popup._hint_filter)
    SIGNALS.update_event.connect(popup.on_update_event)
    SIGNALS.tracked_changed.connect(popup.refresh_tracked)
    SIGNALS.status_message.connect(popup.set_status)
    SIGNALS.reload_feed.connect(popup.reload_recent)

    tray = QtWidgets.QSystemTrayIcon(app_qicon())
    tray.setToolTip("legman tracker")

    menu = QtWidgets.QMenu()
    act_open = menu.addAction("open")
    act_check = menu.addAction("check now")
    menu.addSeparator()
    act_quit = menu.addAction("quit")

    act_open.triggered.connect(popup.show_at_tray)
    act_check.triggered.connect(lambda: submit(poll_now()))

    def do_quit():
        logger.info("quit requested")
        STOP.set()
        tray.hide()
        app.quit()
    act_quit.triggered.connect(do_quit)

    tray.setContextMenu(menu)

    def on_activated(reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            popup.toggle()
    tray.activated.connect(on_activated)
    tray.show()

    threading.Thread(target=poller_thread, name="poller", daemon=True).start()

    logger.info("tray running")
    app.exec()
    STOP.set()
    logger.info("=== %s exiting ===", APP_NAME)


if __name__ == "__main__":
    main()
