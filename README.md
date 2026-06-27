# token-watcher

A floating, always-on-top desktop widget for Ubuntu that shows your current
[OpenRouter](https://openrouter.ai) API spend at a glance.

Built with Python and GTK4. Designed to sit in a corner of your desktop and
stay out of the way until you need it.

![screenshot placeholder](https://raw.githubusercontent.com/jeff-hillman/token-watcher/main/docs/screenshot.png)

---

## Features

- Dark card widget, always on top, no titlebar
- Shows current billing period spend from the OpenRouter API
- Drag anywhere on screen to reposition
- Double-click to force an immediate refresh
- Right-click for a quick menu (refresh / quit)
- Polls every 60 seconds (configurable)
- Freshness indicator dot (green = fresh, amber = stale)
- Works on X11 and Wayland (via gtk-layer-shell)

---

## Installation

### From the Snap Store

```bash
sudo snap install token-watcher
```

### Configure your API key

Get your API key from [openrouter.ai/keys](https://openrouter.ai/keys), then:

```bash
snap set token-watcher api-key="sk-or-..."
```

Optionally change the refresh interval (default 60 seconds):

```bash
snap set token-watcher refresh-interval=30
```

### Launch

```bash
token-watcher
```

Or find it in your application launcher.

---

## Running without snap (development)

**Dependencies:**

```bash
sudo apt install python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-4.0 gir1.2-gtklayershell-0.1 \
    libgtk-layer-shell0
```

**Run:**

```bash
TOKENWATCH_API_KEY="sk-or-..." python3 src/token_watch.py
```

All snap config keys have `TOKENWATCH_` environment variable equivalents:

| Snap config                          | Env var                          |
|--------------------------------------|----------------------------------|
| `snap set token-watcher api-key=...` | `TOKENWATCH_API_KEY=...`         |
| `snap set token-watcher refresh-interval=30` | `TOKENWATCH_REFRESH_INTERVAL=30` |

---

## Building the snap locally

```bash
sudo snap install snapcraft --classic
snapcraft
sudo snap install token-watcher_*.snap --dangerous
```

---

## How it works

The widget polls `https://openrouter.ai/api/v1/auth/key` on a timer. The
`usage` field returns your spend in USD for the current billing period. Data
is fetched in a background thread so the UI never blocks.

Always-on-top behaviour is implemented via:
- **Wayland**: `gtk-layer-shell` (sets the `TOP` layer)
- **X11**: `GdkToplevel.begin_move()` for dragging; `_NET_WM_STATE_ABOVE`
  via `wmctrl` or `xdotool` (if installed) for the always-on-top state

---

## Author

Jeff Hillman — [github.com/jeff-hillman](https://github.com/jeff-hillman)

## License

MIT
