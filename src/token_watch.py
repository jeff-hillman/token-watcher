#!/usr/bin/env python3
"""
token-watcher - AI API spend desktop widget
A floating, always-on-top GTK4 widget for monitoring API spend.

Currently supported providers: openrouter

Config (as a snap):
  snap set token-watcher provider=openrouter   # default
  snap set token-watcher api-key="sk-or-..."
  snap set token-watcher refresh-interval=60   # optional, default 60s

Config (standalone, via env vars):
  TOKENWATCH_PROVIDER=openrouter TOKENWATCH_API_KEY="sk-or-..." token-watcher
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

DEFAULT_REFRESH  = 60   # seconds
DEFAULT_PROVIDER = "openrouter"

# Dark card palette
C_BG     = (0.10, 0.11, 0.13, 0.93)
C_BORDER = (0.25, 0.28, 0.33, 1.00)
C_LABEL  = (0.45, 0.50, 0.58, 1.00)
C_VALUE  = (0.93, 0.95, 0.97, 1.00)
C_TRACK  = (0.18, 0.21, 0.25, 1.00)
C_OK     = (0.20, 0.83, 0.60, 1.00)   # green  < 70 %
C_WARN   = (0.98, 0.75, 0.15, 1.00)   # amber  70-90 %
C_CRIT   = (0.95, 0.30, 0.30, 1.00)   # red    > 90 %

RADIUS = 10
W      = 230
H      = 96
PAD    = 14
DRAG_THRESHOLD = 4   # pixels of movement before a press becomes a drag


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
# Provider result schema
# --------------------------------------------------------------------------
#
# Every provider's fetch() must return a dict with these keys:
#
#   spend        float | None   - USD spent this billing period
#   spend_label  str            - short description of what spend covers
#   limit        float | None   - USD limit if known, else None
#   extra        str | None     - optional second info line (e.g. token headroom)
#   error        str | None     - error message; if set and spend is None, shown instead
#
# The widget renders whatever is populated; missing/None fields are omitted.

def _result(spend=None, spend_label="", limit=None, extra=None, error=None):
    return {
        "spend":       spend,
        "spend_label": spend_label,
        "limit":       limit,
        "extra":       extra,
        "error":       error,
    }


def _http(url, headers, timeout=10):
    """GET url, return (response_body_dict, http_response_object).
    Raises urllib.error.HTTPError on non-2xx."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode()), resp


# --------------------------------------------------------------------------
# Provider: OpenRouter
# --------------------------------------------------------------------------

class OpenRouterProvider:
    NAME    = "openrouter"
    DISPLAY = "OPENROUTER"
    URL     = "https://openrouter.ai/api/v1/auth/key"

    @staticmethod
    def fetch(api_key: str, **_kwargs) -> dict:
        if not api_key:
            return _result(error="No API key set")
        try:
            body, _ = _http(OpenRouterProvider.URL, {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer":  "https://github.com/jeff-hillman/token-watcher",
                "X-Title":       "token-watcher",
            })
            info  = body.get("data", body)
            spend = float(info.get("usage", 0))
            return _result(spend=spend, spend_label="this billing period")

        except urllib.error.HTTPError as e:
            return _result(error="Invalid API key" if e.code == 401 else f"HTTP {e.code}")
        except Exception as e:
            return _result(error=str(e)[:40])


# --------------------------------------------------------------------------
# Provider registry
# --------------------------------------------------------------------------
# To add a new provider:
#   1. Create a class with NAME, DISPLAY, and a fetch(**kwargs) -> dict method
#   2. fetch() receives api_key, admin_key, and any future config keys
#   3. Return via _result(): spend, spend_label, limit, extra, error
#   4. Add to PROVIDERS below

PROVIDERS = {
    OpenRouterProvider.NAME: OpenRouterProvider,
}

def get_provider_class(name: str):
    return PROVIDERS.get(name.lower().strip(), OpenRouterProvider)


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + r,     y + r,     r, math.pi,         3 * math.pi / 2)
    cr.arc(x + w - r, y + r,     r, 3 * math.pi / 2, 2 * math.pi)
    cr.arc(x + w - r, y + h - r, r, 0,               math.pi / 2)
    cr.arc(x + r,     y + h - r, r, math.pi / 2,     math.pi)
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

        self._data             = _result(error="Loading...")
        self._last_fetch       = 0.0
        self._refresh_interval = int(get_config("refresh-interval", str(DEFAULT_REFRESH)))
        self._provider_name    = get_config("provider", DEFAULT_PROVIDER)
        self._provider_cls     = get_provider_class(self._provider_name)
        self._layer_shell      = False   # True when gtk-layer-shell is active
        self._drag_start       = None    # (ptr_x, ptr_y, margin_left, margin_top)
        self._drag_active      = False   # True once pointer exceeds DRAG_THRESHOLD
        self._always_on_top    = True    # togglable via right-click menu

        self._setup_window()
        self._setup_css()
        self._setup_drawing()
        self._setup_gestures()
        self._setup_window_hints()

        self._async_fetch()
        GLib.timeout_add_seconds(self._refresh_interval, self._on_timer)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self):
        self.set_title("token-watcher")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(W, H)
        # Don't steal focus from other windows when clicked
        self.set_focusable(False)
        self.set_can_focus(False)

    def _setup_css(self):
        # Force dark theme for the popover/menu regardless of system theme
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

        provider = Gtk.CssProvider()
        provider.load_from_data(b"window { background-color: transparent; }")
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
        area.set_focusable(False)
        self.set_child(area)
        self._area = area

    def _setup_gestures(self):
        # Single controller for left-button press/release (handles drag + click + dbl-click)
        lclick = Gtk.GestureClick()
        lclick.connect("pressed",  self._on_press)
        lclick.connect("released", self._on_release)
        self._area.add_controller(lclick)

        # Motion controller on the window (not the area) so it keeps receiving
        # events even when the pointer moves outside the widget during a drag.
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)
        self._motion_ctrl = motion   # keep a reference

        rc = Gtk.GestureClick()
        rc.set_button(3)
        rc.connect("pressed", self._right_click)
        self._area.add_controller(rc)

    def _setup_window_hints(self):
        # gtk-layer-shell: init_for_window MUST be called before the window
        # is mapped (i.e. before present()/realize completes), so we do it here.
        try:
            gi.require_version('GtkLayerShell', '0.1')
            from gi.repository import GtkLayerShell
            if GtkLayerShell.is_supported():
                GtkLayerShell.init_for_window(self)
                GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
                GtkLayerShell.set_exclusive_zone(self, -1)
                # Never give this surface keyboard focus — keeps it out of
                # the compositor's focus cycle (alt-tab, etc.)
                try:
                    GtkLayerShell.set_keyboard_mode(
                        self, GtkLayerShell.KeyboardMode.NONE)
                except Exception:
                    pass  # older gtk-layer-shell versions may not have this
                # Anchor to top-left so margin offsets are screen-relative
                GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
                GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP,  True)
                # Start 20 px from top-right — user can drag to reposition
                try:
                    mon = Gdk.Display.get_default().get_monitors().get_item(0)
                    geo = mon.get_geometry()
                    GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT,
                                             geo.width - W - 20)
                except Exception:
                    GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, 20)
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 20)
                self._layer_shell = True
                return
        except Exception:
            pass

        # X11 / non-layer-shell Wayland: use realize + map signals
        self.connect("realize", self._on_realize)
        self.connect("map",     self._on_map)

    # ------------------------------------------------------------------
    # Always-on-top / compositor hints
    # ------------------------------------------------------------------

    def _on_realize(self, widget):
        # Stamp window hints before map so the WM picks them up on first show.
        # _on_map will send the client message to re-evaluate _ABOVE afterward.
        ctx = self._x11_ctx()
        if ctx is None:
            return
        import ctypes
        xlib, xdisplay, xwindow, _ = ctx
        xlib.XInternAtom.restype  = ctypes.c_ulong
        xlib.XChangeProperty.restype = ctypes.c_int

        XA_ATOM = ctypes.c_ulong(4)
        PropModeReplace = 0

        def _intern(name):
            return xlib.XInternAtom(xdisplay, name, False)

        def _set_atoms(prop, *atoms):
            arr = (ctypes.c_ulong * len(atoms))(*atoms)
            xlib.XChangeProperty(xdisplay, xwindow, prop,
                                 XA_ATOM, 32, PropModeReplace,
                                 ctypes.cast(arr, ctypes.c_char_p),
                                 len(atoms))

        # Window type: UTILITY — excluded from alt-tab by virtually all WMs
        NET_WM_WINDOW_TYPE         = _intern(b"_NET_WM_WINDOW_TYPE")
        NET_WM_WINDOW_TYPE_UTILITY = _intern(b"_NET_WM_WINDOW_TYPE_UTILITY")
        _set_atoms(NET_WM_WINDOW_TYPE, NET_WM_WINDOW_TYPE_UTILITY)

        # State: above + skip taskbar + skip pager (keep out of focus cycle)
        NET_WM_STATE              = _intern(b"_NET_WM_STATE")
        NET_WM_STATE_ABOVE        = _intern(b"_NET_WM_STATE_ABOVE")
        NET_WM_STATE_SKIP_TASKBAR = _intern(b"_NET_WM_STATE_SKIP_TASKBAR")
        NET_WM_STATE_SKIP_PAGER   = _intern(b"_NET_WM_STATE_SKIP_PAGER")
        _set_atoms(NET_WM_STATE,
                   NET_WM_STATE_ABOVE,
                   NET_WM_STATE_SKIP_TASKBAR,
                   NET_WM_STATE_SKIP_PAGER)

        xlib.XFlush(xdisplay)

    def _on_map(self, widget):
        # After the window is mapped, send a client message so the WM
        # re-evaluates the state (needed for GNOME Shell / Mutter on X11).
        self._x11_set_above_state(self._always_on_top)

    def _x11_ctx(self):
        import ctypes, ctypes.util
        try:
            gi.require_version('GdkX11', '4.0')
            from gi.repository import GdkX11
            surface = self.get_surface()
            if not isinstance(surface, GdkX11.X11Surface):
                return None
            lib_x11  = ctypes.util.find_library("X11")
            lib_gtk4 = ctypes.util.find_library("gtk-4") or "libgtk-4.so.1"
            if not lib_x11 or not lib_gtk4:
                return None
            xlib   = ctypes.CDLL(lib_x11)
            libgtk = ctypes.CDLL(lib_gtk4)
            class _PyGObj(ctypes.Structure):
                _fields_ = [('ob_refcnt', ctypes.c_ssize_t),
                             ('ob_type',  ctypes.c_void_p),
                             ('gobject',  ctypes.c_void_p)]
            display  = GdkX11.X11Display.get_default()
            raw      = _PyGObj.from_address(id(display))
            libgtk.gdk_x11_display_get_xdisplay.argtypes = [ctypes.c_void_p]
            libgtk.gdk_x11_display_get_xdisplay.restype  = ctypes.c_void_p
            xdisplay = libgtk.gdk_x11_display_get_xdisplay(ctypes.c_void_p(raw.gobject))
            return xlib, xdisplay, surface.get_xid(), display.get_xrootwindow()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, area, cr, width, height):
        # Card background
        cr.set_source_rgba(*C_BG)
        rounded_rect(cr, 0, 0, width, height, RADIUS)
        cr.fill()

        # Border
        cr.set_source_rgba(*C_BORDER)
        cr.set_line_width(1.0)
        rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, RADIUS)
        cr.stroke()

        inner_w = width - PAD * 2

        # Provider name as title
        title = getattr(self._provider_cls, 'DISPLAY', self._provider_name.upper())
        self._text(cr, area, title,
                   Pango.FontDescription.from_string("Monospace Bold 7"),
                   C_LABEL, PAD, 10, inner_w)

        # Freshness dot
        age   = time.time() - self._last_fetch
        dot_c = C_OK if (self._last_fetch > 0 and age < self._refresh_interval * 1.5) \
                     else C_WARN
        cr.set_source_rgba(*dot_c)
        cr.arc(width - PAD, 14, 3, 0, 2 * math.pi)
        cr.fill()

        err   = self._data.get("error")
        spend = self._data.get("spend")
        limit = self._data.get("limit")
        extra = self._data.get("extra")
        label = self._data.get("spend_label", "")

        # --- Error state ---
        if err and spend is None and not extra:
            self._text(cr, area, err,
                       Pango.FontDescription.from_string("Monospace Bold 13"),
                       C_CRIT, PAD, 28, inner_w)
            self._draw_bar_track(cr, width, height, inner_w)
            return

        # --- Main value ---
        if spend is not None:
            if limit is not None:
                value_str = f"${spend:.2f} / ${limit:.2f}"
            else:
                value_str = f"${spend:.4f}"
        elif extra:
            # No spend data but have extra (e.g. Anthropic rate-limit-only mode)
            value_str = ""
        else:
            value_str = "---"

        if value_str:
            self._text(cr, area, value_str,
                       Pango.FontDescription.from_string("Monospace Bold 16"),
                       C_VALUE, PAD, 25, inner_w)

        # --- Sub-label (spend description or error alongside extra) ---
        sub = label
        if err and extra:
            sub = f"{err}"
        if sub:
            self._text(cr, area, sub,
                       Pango.FontDescription.from_string("Monospace 7"),
                       C_LABEL, PAD, 48, inner_w)

        # --- Extra line (rate-limit headroom etc.) ---
        extra_y = 48 if not sub else 57
        if extra:
            self._text(cr, area, extra,
                       Pango.FontDescription.from_string("Monospace 7"),
                       C_LABEL, PAD, extra_y, inner_w)

        # --- Progress bar (only when we have spend + limit) ---
        if spend is not None and limit is not None and limit > 0:
            fraction = min(spend / limit, 1.0)
            self._draw_bar(cr, width, height, inner_w, fraction)
        else:
            self._draw_bar_track(cr, width, height, inner_w)

    def _draw_bar_track(self, cr, width, height, inner_w):
        cr.set_source_rgba(*C_TRACK)
        rounded_rect(cr, PAD, height - 15, inner_w, 5, 2.5)
        cr.fill()

    def _draw_bar(self, cr, width, height, inner_w, fraction):
        self._draw_bar_track(cr, width, height, inner_w)
        fill_w = max(5, inner_w * fraction)
        cr.set_source_rgba(*bar_color(fraction))
        rounded_rect(cr, PAD, height - 15, fill_w, 5, 2.5)
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
    # Press / drag / release — unified handler
    # ------------------------------------------------------------------

    def _on_press(self, gesture, n_press, x, y):
        # Record press position; motion handler decides if this becomes a drag
        self._drag_start  = (x, y, self._ls_margin_left(), self._ls_margin_top())
        self._drag_active = False

    def _on_motion(self, controller, x, y):
        if self._drag_start is None:
            return
        start_x, start_y, orig_left, orig_top = self._drag_start
        dx = x - start_x
        dy = y - start_y

        if not self._drag_active:
            if (dx * dx + dy * dy) < DRAG_THRESHOLD * DRAG_THRESHOLD:
                return   # not enough movement yet — don't start a drag
            self._drag_active = True

        if self._layer_shell:
            try:
                gi.require_version('GtkLayerShell', '0.1')
                from gi.repository import GtkLayerShell
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT,
                                         max(0, orig_left + int(dx)))
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP,
                                         max(0, orig_top  + int(dy)))
            except Exception:
                pass
        else:
            # WM-native move — delegate to compositor on first motion past threshold
            surface = self.get_surface()
            if surface is None:
                return
            # Reconstruct an event for begin_move; use the motion event's device
            # by asking the gesture for the last event instead
            event = controller.get_current_event()
            if event is None:
                return
            surface.begin_move(event.get_device(), 1,
                                start_x + dx, start_y + dy,
                                event.get_time())
            # After handing off to WM, clear so we don't keep calling begin_move
            self._drag_start = None

    def _on_release(self, gesture, n_press, x, y):
        was_drag = self._drag_active
        self._drag_start  = None
        self._drag_active = False
        if not was_drag and n_press == 2:
            self._async_fetch()

    def _ls_margin_left(self):
        try:
            gi.require_version('GtkLayerShell', '0.1')
            from gi.repository import GtkLayerShell
            return GtkLayerShell.get_margin(self, GtkLayerShell.Edge.LEFT)
        except Exception:
            return 0

    def _ls_margin_top(self):
        try:
            gi.require_version('GtkLayerShell', '0.1')
            from gi.repository import GtkLayerShell
            return GtkLayerShell.get_margin(self, GtkLayerShell.Edge.TOP)
        except Exception:
            return 0

    def _right_click(self, gesture, n_press, x, y):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        btn_refresh = Gtk.Button(label="Refresh now")
        btn_refresh.connect("clicked", lambda _: (self._async_fetch(), popover.popdown()))
        box.append(btn_refresh)

        aot_label = "Disable always on top" if self._always_on_top else "Enable always on top"
        btn_aot = Gtk.Button(label=aot_label)
        btn_aot.connect("clicked", lambda _: (self._toggle_always_on_top(), popover.popdown()))
        box.append(btn_aot)

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

    def _toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        if self._layer_shell:
            try:
                gi.require_version('GtkLayerShell', '0.1')
                from gi.repository import GtkLayerShell
                layer = GtkLayerShell.Layer.TOP if self._always_on_top \
                        else GtkLayerShell.Layer.BOTTOM
                GtkLayerShell.set_layer(self, layer)
            except Exception:
                pass
        else:
            self._x11_set_above_state(self._always_on_top)

    def _x11_set_above_state(self, enable):
        ctx = self._x11_ctx()
        if ctx is None:
            return
        import ctypes
        xlib, xdisplay, xwindow, xroot = ctx
        xlib.XInternAtom.restype = ctypes.c_ulong
        NET_WM_STATE       = xlib.XInternAtom(xdisplay, b"_NET_WM_STATE",       False)
        NET_WM_STATE_ABOVE = xlib.XInternAtom(xdisplay, b"_NET_WM_STATE_ABOVE", False)

        if enable:
            # Stamp the property directly on the window so the WM sees it
            xlib.XChangeProperty.restype = ctypes.c_int
            xlib.XChangeProperty(xdisplay, xwindow, NET_WM_STATE,
                                 ctypes.c_ulong(4),   # XA_ATOM
                                 32, 0,               # PropModeReplace
                                 ctypes.cast(ctypes.byref(ctypes.c_ulong(NET_WM_STATE_ABOVE)),
                                             ctypes.c_char_p),
                                 1)
        else:
            # Remove the property entirely so the WM can't re-read it
            xlib.XDeleteProperty(xdisplay, xwindow, NET_WM_STATE)

        # Send client message — use source=2 (pager) so GNOME Shell honours it
        class _Ev(ctypes.Structure):
            _fields_ = [("type",         ctypes.c_int),
                        ("serial",        ctypes.c_ulong),
                        ("send_event",    ctypes.c_int),
                        ("display",       ctypes.c_void_p),
                        ("window",        ctypes.c_ulong),
                        ("message_type",  ctypes.c_ulong),
                        ("format",        ctypes.c_int),
                        ("data",          ctypes.c_ulong * 5)]
        ev = _Ev()
        ev.type         = 33          # ClientMessage
        ev.window       = xwindow
        ev.message_type = NET_WM_STATE
        ev.format       = 32
        ev.data[0]      = 1 if enable else 0   # _NET_WM_STATE_ADD / _REMOVE
        ev.data[1]      = NET_WM_STATE_ABOVE
        ev.data[2]      = 0
        ev.data[3]      = 2           # source: pager (GNOME Shell respects this)
        xlib.XSendEvent(xdisplay, xroot, False, 0x00080000 | 0x00100000, ctypes.byref(ev))
        xlib.XFlush(xdisplay)

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    def _async_fetch(self):
        # Re-read config on every fetch so snap set changes take effect live
        provider_name = get_config("provider", DEFAULT_PROVIDER)
        if provider_name != self._provider_name:
            self._provider_name = provider_name
            self._provider_cls  = get_provider_class(provider_name)
            self._area.queue_draw()  # redraw title immediately

        kwargs = {
            "api_key":   get_config("api-key"),
            "admin_key": get_config("admin-key"),
        }
        threading.Thread(target=self._fetch_thread, kwargs=kwargs, daemon=True).start()

    def _fetch_thread(self, **kwargs):
        result = self._provider_cls.fetch(**kwargs)
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
        super().__init__()
        GLib.set_application_name("Token Watcher")

    def do_activate(self):
        win = TokenWatch(self)
        win.present()


def main():
    if not os.environ.get("GTK_A11Y"):
        os.environ["GTK_A11Y"] = "none"
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
