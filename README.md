# token-watcher

A floating, always-on-top desktop widget for Ubuntu that shows your AI API
spend at a glance. Supports multiple providers.

Built with Python and GTK4. Designed to sit in a corner of your desktop and
stay out of the way until you need it.

![screenshot](https://raw.githubusercontent.com/jeff-hillman/token-watcher/main/docs/screenshot.png)

---

## Supported providers

| Provider | Config | What it shows |
|---|---|---|
| **OpenRouter** | `api-key` | USD spend this billing period |
| **Anthropic** | `api-key` + optional `admin-key` | Rate-limit headroom (regular key); spend this calendar month (admin key) |

More providers can be added - see [Adding a provider](#adding-a-provider).

---

## Installation

### From the Snap Store

```bash
sudo snap install token-watcher
```

### Configure

**OpenRouter:**
```bash
snap set token-watcher provider=openrouter
snap set token-watcher api-key="sk-or-..."
```

**Anthropic (rate-limit view, regular key):**
```bash
snap set token-watcher provider=anthropic
snap set token-watcher api-key="sk-ant-api03-..."
```

**Anthropic (spend view, admin key):**
```bash
snap set token-watcher provider=anthropic
snap set token-watcher admin-key="sk-ant-admin01-..."
# api-key is optional alongside admin-key but adds rate-limit info
snap set token-watcher api-key="sk-ant-api03-..."
```

**Optional settings:**
```bash
snap set token-watcher refresh-interval=30   # default: 60 seconds
```

### Launch

```bash
token-watcher
```

---

## Usage

- **Drag** anywhere on screen to reposition
- **Double-click** to force an immediate refresh
- **Right-click** for a quick menu (refresh / quit)
- The dot in the top-right corner is green when data is fresh, amber when stale

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
# OpenRouter
TOKENWATCH_PROVIDER=openrouter TOKENWATCH_API_KEY="sk-or-..." python3 src/token_watch.py

# Anthropic (rate limits only)
TOKENWATCH_PROVIDER=anthropic TOKENWATCH_API_KEY="sk-ant-api03-..." python3 src/token_watch.py

# Anthropic (spend + rate limits)
TOKENWATCH_PROVIDER=anthropic \
  TOKENWATCH_API_KEY="sk-ant-api03-..." \
  TOKENWATCH_ADMIN_KEY="sk-ant-admin01-..." \
  python3 src/token_watch.py
```

All `snap set` keys map to `TOKENWATCH_<KEY>` env vars (uppercased, hyphens → underscores).

---

## Building the snap locally

```bash
sudo snap install snapcraft --classic
snapcraft
sudo snap install token-watcher_*.snap --dangerous
```

---

## Adding a provider

Each provider is a small class in `src/token_watch.py`. To add one:

1. Create a class with `NAME`, `DISPLAY`, and a `fetch(**kwargs) -> dict` static method
2. The `fetch` method receives `api_key`, `admin_key`, and any future config keys
3. Return a dict via the `_result()` helper: `spend`, `spend_label`, `limit`, `extra`, `error`
4. Register it in the `PROVIDERS` dict

```python
class MyProvider:
    NAME    = "myprovider"
    DISPLAY = "MY PROVIDER"

    @staticmethod
    def fetch(api_key: str, **kwargs) -> dict:
        # ... call your API ...
        return _result(spend=12.34, spend_label="this month")

PROVIDERS["myprovider"] = MyProvider
```

---

## How it works

The widget polls the configured provider's API on a timer. All API calls run
in a background thread so the UI never blocks.

Always-on-top behaviour:
- **Wayland**: `gtk-layer-shell` (sets the `TOP` layer)
- **X11**: `GdkToplevel.begin_move()` for dragging; `_NET_WM_STATE_ABOVE` via
  `wmctrl` or `xdotool` (if installed), with a `libX11` ctypes fallback

---

## Author

Jeff Hillman — [github.com/jeff-hillman](https://github.com/jeff-hillman)
