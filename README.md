# Legman Tracker (Windows tray app)

A standalone Windows app that watches Roblox games and their subplaces and pops a
**Windows notification** whenever a game or one of its places gets updated. It
lives in the system tray; click the icon for a styled flyout panel showing a live
**feed of recent updates**, your **tracked games**, and a box to add more.

The Roblox-fetching logic comes from the Discord bot; here it routes updates to
Windows toasts + an in-app feed instead of Discord embeds. The UI is built with
Qt (PySide6) — no browser, no console.

## What it can notify on (per game)

Every tracked game has its own **⚙ settings** (the cogwheel on its row in the
*Tracked games* tab) where you toggle exactly what to be alerted on:

- **Game updates** — the universe's `updated` timestamp moves forward.
- **Place updates** — a sub-place inside the game gets a new update time.
- **New / deleted sub-places** — a place is added to or removed from the game.
- **Status changes** — goes private/hidden, comes back, becomes paid, under review, etc.
- **New badges** — a badge is created.
- **Deleted badges** — a badge is removed.
- **Rare badge awards** — award-count changes for badges with ≤ 250 owners
  (avoids spam from popular badges).

Defaults for a newly added game are **game updates + place updates**; everything
else is off until you turn it on. Badge tracking is only fetched when you enable a
badge option (and the first check after enabling silently snapshots the current
badges so you don't get a flood of "new badge" alerts for existing ones).

## Using it

Run **`LegmanTracker.exe`** (in `dist` after building). The icon appears in the
system tray near the clock — click the `^` ("show hidden icons") if you don't see
it.

- **Left-click the tray icon** → opens the flyout panel:
  - **Recent updates** tab — a live feed of every game/place update, newest on
    top, with the game icon and how long ago it happened. Click a card to open
    that game's Roblox page.
  - **Tracked games** tab — everything you're watching, with status + subplace
    count + how many alert types are on. Each row has a **⚙** (choose what to
    track for that game) and an **✕** (stop tracking). Click a row to open the game.
  - **Add box** (bottom, always visible) — paste a Roblox **place ID** or a full
    game URL and hit **Add** (or Enter).
  - **⟳** in the header forces an immediate check; **✕** closes the panel.
  - The panel auto-hides when it loses focus, like a normal flyout.
- **Right-click the tray icon** → menu: Open, Check now, Quit. (*Start with
  Windows* lives on the panel's settings page.)
- Click a **notification** to open that game's Roblox page.

It checks every tracked game every **60 seconds**. The first check after adding a
game just sets the baseline — you only get notified on changes *after* that.

### Where it stores data

Everything is in `%APPDATA%\LegmanTracker\`:

- `tracked_games.json` — the games you're watching + last-seen state
- `updates_history.json` — the recent-updates feed (last 100)
- `icons\` — cached game icons (shown on cards + toasts)
- `legmantracker.log` — activity log

## Building the .exe yourself

You need Python 3 installed (3.14 was used here). Then just run:

```
build.bat
```

It creates a virtual environment, installs the dependencies, and runs PyInstaller
(the app icon comes from `icon.ico`). The result is **`dist\LegmanTracker.exe`** —
a single ~38 MB file you can copy anywhere (no Python needed to run it).
`build.bat` closes any running copy first so the rebuild doesn't fail on a locked
exe.

Run from source without building:

```
.venv\Scripts\pythonw.exe legmantracker.py
```

Quick check that notifications + Qt work on your machine:

```
.venv\Scripts\python.exe legmantracker.py --selftest
```

## Tuning

- **Check interval** — set how often it polls (30s / 1m / 2m / 5m / 10m) in the
  in-app **settings page** (the ⚙ in the panel header). Don't go too low or Roblox
  may rate-limit you.
- Colors/look live in the `STYLESHEET` string in `legmantracker.py`.

To also notify on **status changes** (game goes private / comes back) or
**subplaces added/deleted**, look in `poll_once()` — those changes are already
computed there; you'd just build an event and call `push_event(...)` like the
game/place updates do.

## Reliability (no false-alert spam when Roblox's API hiccups)

Roblox's web API regularly returns stale/empty/partial data from different edge
servers. To avoid spamming you with "updates" that didn't really happen:

- **Request timeouts** — a hung endpoint can't stall a whole check.
- **Forward-only** — game/place timestamps and badge award-counts only ever move
  *up*; a value that jumps backwards is treated as a stale cache and ignored.
- **Confirmation (debounce)** — a status change, sub-place deletion or badge
  deletion must show up on **two checks in a row** before it alerts, so one bad
  poll can't fire it. A place/badge that briefly vanishes is carried over, not
  reported as deleted.
- **Outage circuit-breaker** — if at least half of your tracked games fail or
  come back empty in one check (3+ games), the app assumes the API is down,
  **drops every alert from that check**, shows "roblox api looks down — alerts
  paused", and re-evaluates from the last good state.

(This behaviour is covered by a simulation test that flaps the API and asserts no
false alerts get through while real, sustained changes still do.)

## Notes / limits

- Roblox's API only exposes **public** games. Private/unlisted games show up as
  `private/hidden` and their places can't be read until they're public again.
- Toasts respect Windows **Focus Assist / Do Not Disturb** — if those are on,
  notifications are held in the Action Center.
- This polls the public Roblox web API; it is not affiliated with Roblox.

## License

[MIT](LICENSE) — do whatever you want, no warranty.
