#!/usr/bin/env python3
"""
token-watch - OpenRouter credit usage desktop widget
A floating, always-on-top GTK4 widget for monitoring OpenRouter API spend.

Config (as a snap):
  snap set token-watch api-key="sk-or-..."
  snap set token-watch refresh-interval=60

Config (standalone, via env vars):
  TOKENWATCH_API_KEY="sk-or-..." token-watch
  TOKENWATCH_REFRESH_INTERVAL=60 token-watch
"""

import cairo  # must be imported before gi loads cairo to register pycairo foreign types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
gi.require_foreign('cairo')
from gi.repository import Gtk, Gdk, GLib, Pango
from gi.repository import PangoCairo

import threading
import subprocess
import os
import sys
import json
import math
import urllib.request
import urllib.error
import time

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_REFRESH = 60   # seconds
KEY_URL = "https://openrouter.ai/api/v1/auth/key"

# Dark card palette
C_BG        = (0.10, 0.11, 0.13, 0.93)
C_BORDER    = (0.25, 0.28, 0.33, 1.00)
C_LABEL     = (0.45, 0.50, 0.58, 1.00)
C_VALUE     = (0.93, 0.95, 0.97, 1.00)
C_TRACK     = (0.18, 0.21, 0.25, 1.00)
C_OK        = (0.20, 0.83, 0.60, 1.00)   # green  < 70%
C_WARN      = (0.98, 0.75, 0.15, 1.00)   # amber  70-90%
C_CRIT      = (0.95, 0.30, 0.30, 1.00)   # red    > 90%

RADIUS  = 10
W       = 230
H       = 96
PAD     = 14


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def get_config(key: str, default: str = "") -> str:
    """Read snap config key, fall back to TOKENWATCH_* env var."""
    if os.environ.get("SNAP"):
        try:
            result = subprocess.run(
                ["snapctl", "get", key],
                capture_output=True, text=True, timeout=3
            )
            val = result.stdout.strip()
            if val:
                return val
        except Exception:
            pass
    env_key = "TOKENWATCH_" + key.upper().replace("-", "_")
    return os.environ.get(env_key, default)


# --------------------------------------------------------------------------
# OpenRouter API
# --------------------------------------------------------------------------

def fetch_usage(api_key: str) -> dict:
    """
    Fetch /api/v1/auth/key from OpenRouter.
    Returns: {usage, error}
    usage is in USD for the current billing period.
    The per-key limit field is a key-level cap, not the plan limit,
    and is usually null - so we don't display a denominator.
    """
    if not api_key:
        return {"usage": None, "error": "No API key set"}

    try:
        req = urllib.request.Request(
            KEY_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/token-watch",
                "X-Title": "token-watch",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())

        info  = body.get("data", body)
        usage = float(info.get("usage", 0))          # USD, current period
        return {"usage": usage, "error": None}

    except urllib.error.HTTPError as e:
        msg = "Invalid API key" if e.code == 401 else f"HTTP {e.code}"
        return {"usage": None, "error": msg}
    except Exception as e:
        return {"usage": None, "error": str(e)[:35]}


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + r,     y + r,     r, math.pi,           3 * math.pi / 2)
    cr.arc(x + w - r, y + r,     r, 3 * math.pi / 2,   2 * math.pi)
    cr.arc(x + w - r, y + h - r, r, 0,                 math.pi / 2)
    cr.arc(x + r,     y + h - r, r, math.pi / 2,       math.pi)
    cr.close_path()


def bar_color(fraction: float):
    if fraction < 0.70:
        return C_OK
    elif fraction < 0.90:
        return C_WARN
    return C_CRIT


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class TokenWatch(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app)

        self._data = {"usage": None, "error": "Loading..."}
        self._last_fetch = 0.0
        self._refresh_interval = int(get_config("refresh-interval",
                                                str(DEFAULT_REFRESH)))

        self._setup_window()
        self._setup_css()
        self._setup_drawing()
        self._setup_gestures()
        self._setup_window_hints()

        # Kick off first fetch
        self._async_fetch()

        # Periodic timer
        GLib.timeout_add_seconds(self._refresh_interval, self._on_timer)

    # ------------------------------------------------------------------
    # Window config
    # ------------------------------------------------------------------

    def _setup_window(self):
        self.set_title("token-watch")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(W, H)

    def _setup_css(self):
        css = b"""
        window {
            background-color: transparent;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _setup_drawing(self):
        area = Gtk.DrawingArea()
        area.set_content_width(W)
        area.set_content_height(H)
        area.set_draw_func(self._draw)
        self.set_child(area)
        self._area = area

    def _setup_gestures(self):
        # Drag to move - use a plain click gesture so we get the button/sequence
        # for GdkToplevel.begin_move() which hands the drag to the WM (smooth)
        drag = Gtk.GestureClick()
        drag.connect("pressed", self._drag_pressed)
        self._area.add_controller(drag)

        # Click (double = force refresh)
        click = Gtk.GestureClick()
        click.connect("released", self._click_released)
        self._area.add_controller(click)

        # Right-click menu
        rc = Gtk.GestureClick()
        rc.set_button(3)
        rc.connect("pressed", self._right_click)
        self._area.add_controller(rc)

    def _setup_window_hints(self):
        """
        Hook into the realize signal so we can manipulate the underlying
        GdkSurface after it's been created.
        """
        self.connect("realize", self._on_realize)

    def _on_realize(self, widget):
        surface = self.get_surface()
        if surface is None:
            return

        # Try gtk-layer-shell first (Wayland / wlroots compositors)
        try:
            gi.require_version('GtkLayerShell', '0.1')
            from gi.repository import GtkLayerShell
            if GtkLayerShell.is_supported():
                GtkLayerShell.init_for_window(self)
                GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
                GtkLayerShell.set_exclusive_zone(self, -1)
                return
        except Exception:
            pass

        # Try X11: send _NET_WM_STATE_ABOVE via ctypes + libX11
        try:
            gi.require_version('GdkX11', '4.0')
            from gi.repository import GdkX11
            if isinstance(surface, GdkX11.X11Surface):
                self._x11_set_above(surface)
                return
        except Exception:
            pass

        # Fallback: nothing we can do on this compositor
        print("token-watch: could not set always-on-top (compositor not supported)",
              file=sys.stderr)

    def _x11_set_above(self, surface):
        """Set _NET_WM_STATE_ABOVE. Tries wmctrl, xdotool, then ctypes."""
        import shutil
        xid = surface.get_xid()

        if shutil.which("wmctrl"):
            try:
                subprocess.Popen(["wmctrl", "-i", "-r", hex(xid), "-b", "add,above"],
                                 stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass

        if shutil.which("xdotool"):
            try:
                subprocess.Popen(["xdotool", "windowstate", "--add", "ABOVE", str(xid)],
                                 stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass

        # ctypes fallback using _x11_ctx
        ctx = self._x11_ctx()
        if ctx is None:
            return
        import ctypes
        xlib, xdisplay, xwindow, xroot = ctx

        xlib.XInternAtom.restype = ctypes.c_ulong
        NET_WM_STATE       = xlib.XInternAtom(xdisplay, b"_NET_WM_STATE",       False)
        NET_WM_STATE_ABOVE = xlib.XInternAtom(xdisplay, b"_NET_WM_STATE_ABOVE", False)

        class _Ev(ctypes.Structure):
            _fields_ = [
                ("type",         ctypes.c_int),
                ("serial",       ctypes.c_ulong),
                ("send_event",   ctypes.c_int),
                ("display",      ctypes.c_void_p),
                ("window",       ctypes.c_ulong),
                ("message_type", ctypes.c_ulong),
                ("format",       ctypes.c_int),
                ("data",         ctypes.c_ulong * 5),
            ]
        ev = _Ev()
        ev.type         = 33
        ev.window       = xwindow
        ev.message_type = NET_WM_STATE
        ev.format       = 32
        ev.data[0]      = 1       # _NET_WM_STATE_ADD
        ev.data[1]      = NET_WM_STATE_ABOVE
        ev.data[3]      = 1       # source=app
        xlib.XSendEvent(xdisplay, xroot, False,
                        0x00080000 | 0x00100000, ctypes.byref(ev))
        xlib.XFlush(xdisplay)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, area, cr, width, height):
        # Background card
        cr.set_source_rgba(*C_BG)
        rounded_rect(cr, 0, 0, width, height, RADIUS)
        cr.fill()

        # Border
        cr.set_source_rgba(*C_BORDER)
        cr.set_line_width(1.0)
        rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, RADIUS)
        cr.stroke()

        inner_w = width - PAD * 2

        # Title label - "LCD" style small caps
        self._text(cr, area, "OPENROUTER  CREDITS",
                   Pango.FontDescription.from_string("Monospace Bold 7"),
                   C_LABEL, PAD, 10, inner_w)

        # Freshness dot  (green if recent, amber if stale)
        age = time.time() - self._last_fetch
        dot_c = C_OK if (self._last_fetch > 0 and age < self._refresh_interval * 1.5) else C_WARN
        cr.set_source_rgba(*dot_c)
        cr.arc(width - PAD, 14, 3, 0, 2 * math.pi)
        cr.fill()

        # Main value line
        err   = self._data.get("error")
        usage = self._data.get("usage")

        if err and usage is None:
            value_str = err
            sub_str   = ""
        elif usage is not None:
            value_str = f"${usage:.4f}"
            sub_str   = "this billing period"
        else:
            value_str = "---"
            sub_str   = ""

        # Big LCD-style number
        self._text(cr, area, value_str,
                   Pango.FontDescription.from_string("Monospace Bold 16"),
                   C_VALUE, PAD, 25, inner_w)

        self._text(cr, area, sub_str,
                   Pango.FontDescription.from_string("Monospace 7"),
                   C_LABEL, PAD, 50, inner_w)

        # Progress bar: animate as a pulsing "alive" indicator when no limit,
        # just draw the track so there's always something there
        bar_y = height - 15
        bar_h = 5
        bar_w = inner_w
        cr.set_source_rgba(*C_TRACK)
        rounded_rect(cr, PAD, bar_y, bar_w, bar_h, bar_h / 2)
        cr.fill()

    @staticmethod
    def _text(cr, area, text, font, color, x, y, max_w):
        if not text:
            return
        layout = area.create_pango_layout(text)
        layout.set_font_description(font)
        layout.set_width(int(max_w * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        cr.set_source_rgba(*color)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    # ------------------------------------------------------------------
    # Drag-to-move
    # ------------------------------------------------------------------

    def _drag_pressed(self, gesture, n_press, x, y):
        """Hand the drag off to the window manager via GdkToplevel.begin_move().
        This is natively smooth - the WM moves the window, not us."""
        surface = self.get_surface()
        if surface is None:
            return
        # begin_move needs the triggering event's device and timestamp
        sequence = gesture.get_current_sequence()
        event    = gesture.get_last_event(sequence)
        if event is None:
            return
        device    = event.get_device()
        timestamp = event.get_time()
        # Cast surface to Toplevel interface and invoke begin_move
        toplevel = surface  # GdkX11Surface implements GdkToplevel
        toplevel.begin_move(device, 1, x, y, timestamp)
        # Tell GTK the gesture is "done" so it doesn't fight the WM
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # ------------------------------------------------------------------
    # Click interactions
    # ------------------------------------------------------------------

    def _click_released(self, gesture, n_press, x, y):
        if n_press == 2:
            self._async_fetch()

    def _right_click(self, gesture, n_press, x, y):
        # Simple popover menu
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        btn_refresh = Gtk.Button(label="Refresh now")
        btn_refresh.connect("clicked", lambda _: (self._async_fetch(), popover.popdown()))
        box.append(btn_refresh)

        btn_quit = Gtk.Button(label="Quit")
        btn_quit.connect("clicked", lambda _: self.get_application().quit())
        box.append(btn_quit)

        popover = Gtk.Popover()
        popover.set_child(box)
        popover.set_parent(self._area)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    def _async_fetch(self):
        api_key = get_config("api-key")
        threading.Thread(target=self._fetch_thread, args=(api_key,),
                         daemon=True).start()

    def _fetch_thread(self, api_key):
        result = fetch_usage(api_key)
        self._last_fetch = time.time()
        GLib.idle_add(self._on_data, result)

    def _on_data(self, result):
        self._data = result
        self._area.queue_draw()
        return False

    def _on_timer(self):
        self._async_fetch()
        return True


# --------------------------------------------------------------------------
# Application entry
# --------------------------------------------------------------------------

class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="ai.openrouter.tokenwatch")

    def do_activate(self):
        win = TokenWatch(self)
        win.present()


def main():
    GTK_A11Y = os.environ.get("GTK_A11Y", "")
    if not GTK_A11Y:
        os.environ["GTK_A11Y"] = "none"  # suppress dbus a11y warning in non-desktop envs
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
