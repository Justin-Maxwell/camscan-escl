"""A small settings window with a live view, for positioning and calibrating.

GTK4 through PyGObject, which is already on a Fedora desktop -- no new
dependency for what is a convenience tool. It talks to the daemon over HTTP
like any other client, so it never touches the camera and can be run from
another machine.

The point of it is calibration. `rig.coverage_mm` is the measurement every
scan depends on, and the honest way to get it is to put a real sheet under
the camera and adjust until the mark sits on its edges. That is a knob and a
picture, which is a GUI, not a config file and a restart.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

# Sizes offered as checkboxes. Name, width mm, height mm.
KNOWN_PAPERS = (
    ("A4", 210.0, 297.0),
    ("A5", 148.0, 210.0),
    ("A6", 105.0, 148.0),
    ("B5", 176.0, 250.0),      # NAPS2 offers it
    ("B7", 88.0, 125.0),
    ("Letter", 215.9, 279.4),
    ("Legal", 215.9, 355.6),
)

# Must match preview.MARK_COLOURS, and in the same order: the swatch beside a
# checkbox is a promise about which line in the picture it refers to.
SWATCHES = ("#ff0000", "#00ff00", "#00ffff", "#ffff00", "#ff00ff")

ANCHORS = (
    ("top-left", "top", "top-right"),
    ("left", "center", "right"),
    ("bottom-left", "bottom", "bottom-right"),
)

WINDOW_W = 1180
SIDEBAR_W = 250

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class Client:
    """The daemon's HTTP API, such as it is."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def settings(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/preview/settings", timeout=10) as r:
            return json.load(r)

    def update(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/preview/settings",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def stream(self):
        """Yield JPEG frames from the daemon's MJPEG endpoint."""
        with urllib.request.urlopen(f"{self.base}/preview/stream", timeout=30) as r:
            buf = bytearray()
            while True:
                chunk = r.read(16384)
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    start = buf.find(SOI)
                    end = buf.find(EOI, start + 2) if start >= 0 else -1
                    if start < 0 or end < 0:
                        break
                    yield bytes(buf[start:end + 2])
                    del buf[:end + 2]


class Window(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, client: Client) -> None:
        super().__init__(application=app, title="camscan-escl")
        self.client = client
        self.set_default_size(WINDOW_W, 760)
        self._stop = threading.Event()
        self._applying = False
        self._frame = (2304, 1536)   # replaced by the daemon's real value

        # Paned, not Box. In a Box the sidebar took two thirds of the width
        # and left the video a thumbnail, even with hexpand False on the
        # sidebar and True on the picture -- GtkBox would not hand the
        # expanding child the space. A Paned puts the divider where it is
        # told, and lets the divider be dragged, which is what someone
        # squinting at a crop mark actually wants.
        root = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        root.set_wide_handle(True)
        self.set_child(root)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        root.set_start_child(self.picture)
        root.set_resize_start_child(True)
        root.set_shrink_start_child(False)

        sidebar = self._sidebar()
        root.set_end_child(sidebar)
        root.set_resize_end_child(False)
        root.set_shrink_end_child(False)
        root.set_position(WINDOW_W - SIDEBAR_W - 12)

        self.connect("close-request", self._on_close)
        try:
            self._load_settings()
        except Exception as exc:  # noqa: BLE001 - show it, do not crash
            self._say(f"cannot reach daemon: {exc}")
        threading.Thread(target=self._read_stream, daemon=True).start()

    # -- layout ---------------------------------------------------------

    def _sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)
        # Sized for the widest label it must hold, not for the window. The
        # labels are capped in characters too -- a wrapping label's natural
        # width is otherwise whatever the text wants, which is what squeezed
        # the picture down to a thumbnail.
        box.set_size_request(SIDEBAR_W, -1)
        box.set_hexpand(False)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Frame coverage</b>")
        box.append(head)

        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, width_chars=30)
        hint.set_markup(
            "<small>The area the camera sees, in mm. Put a real sheet under "
            "the camera and adjust until its mark sits on the edges. Every "
            "scan's scale depends on this.</small>"
        )
        box.append(hint)

        self.lock_check = Gtk.CheckButton(
            label="Lock to frame shape")
        self.lock_check.set_active(True)
        self.lock_check.set_tooltip_text(
            "With the camera square-on, a millimetre is the same number of "
            "pixels in both directions, so the coverage must have the same "
            "shape as the frame. Unlock only for a tilted camera.")
        self.lock_check.connect("toggled", lambda _c: self._sync_height())
        box.append(self.lock_check)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.width_spin = Gtk.SpinButton.new_with_range(20, 2000, 1)
        self.height_spin = Gtk.SpinButton.new_with_range(20, 2000, 1)
        for i, (label, spin) in enumerate((("Width", self.width_spin),
                                           ("Height", self.height_spin))):
            spin.set_digits(1)
            # NOT hexpand. It propagates up and makes the whole sidebar
            # greedy, which is what squeezed the picture to a thumbnail.
            spin.set_hexpand(False)
            spin.set_width_chars(7)
            spin.set_max_width_chars(7)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            grid.attach(spin, 1, i, 1, 1)
            grid.attach(Gtk.Label(label="mm", xalign=0), 2, i, 1, 1)
        self.width_spin.connect("value-changed", lambda _s: self._sync_height())
        box.append(grid)

        self.dpi_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=30,
                                   width_chars=30)
        self.dpi_label.add_css_class("dim-label")
        box.append(self.dpi_label)

        # Proportional nudges: at a fixed camera height the frame keeps its
        # aspect ratio, so scaling both together is the move that matches
        # raising or lowering the camera.
        nudge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        nudge.set_halign(Gtk.Align.START)
        for label, factor in (("−5%", 0.95), ("−1%", 0.99),
                              ("+1%", 1.01), ("+5%", 1.05)):
            b = Gtk.Button(label=label)
            # halign START above stops the box stretching these; a size
            # request is only a minimum, so it cannot make them square on its
            # own.
            b.set_size_request(46, 40)
            b.connect("clicked", self._on_nudge, factor)
            nudge.append(b)
        box.append(nudge)

        box.append(Gtk.Separator())
        anchor_head = Gtk.Label(xalign=0)
        anchor_head.set_markup("<b>Anchor</b>")
        box.append(anchor_head)
        anchor_hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=30,
                                width_chars=30)
        anchor_hint.set_markup(
            "<small>Which part of the frame a scan is taken from.</small>"
        )
        box.append(anchor_hint)

        agrid = Gtk.Grid(column_spacing=4, row_spacing=4)
        agrid.set_halign(Gtk.Align.START)
        self.anchor_buttons = {}
        first = None
        for r, row in enumerate(ANCHORS):
            for c, name in enumerate(row):
                b = Gtk.ToggleButton()
                b.set_size_request(40, 40)
                b.set_tooltip_text(name)
                if first is None:
                    first = b
                else:
                    b.set_group(first)
                b.connect("toggled", self._on_anchor, name)
                self.anchor_buttons[name] = b
                agrid.attach(b, c, r, 1, 1)
        box.append(agrid)

        box.append(Gtk.Separator())
        papers = Gtk.Label(xalign=0)
        papers.set_markup("<b>Paper sizes</b>")
        box.append(papers)

        self.paper_checks = {}
        self.paper_swatches = {}
        for name, mm_w, mm_h in KNOWN_PAPERS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(14, 14)
            swatch.set_valign(Gtk.Align.CENTER)
            swatch.set_draw_func(self._draw_swatch, name)
            self.paper_swatches[name] = swatch
            check = Gtk.CheckButton(label=f"{name}  {mm_w:g}×{mm_h:g}")
            check.connect("toggled", lambda _c: self._refresh_swatches())
            self.paper_checks[name] = check
            row.append(swatch)
            row.append(check)
            box.append(row)

        box.append(Gtk.Separator())
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply)
        box.append(self.apply_button)

        revert = Gtk.Button(label="Reload from daemon")
        revert.connect("clicked", lambda _b: self._load_settings())
        box.append(revert)

        self.status = Gtk.Label(xalign=0, wrap=True, max_width_chars=30,
                                width_chars=30)
        self.status.add_css_class("dim-label")
        box.append(self.status)
        return box

    # -- behaviour ------------------------------------------------------

    def _sync_height(self) -> None:
        """Keep the coverage the same shape as the frame, and report the dpi.

        A coverage whose aspect differs from the frame's means one millimetre
        is a different number of pixels across than down, and every scan comes
        out stretched by that ratio. It was 2.05x on this rig before anyone
        noticed, because the nudge buttons scaled both axes together and so
        preserved the error perfectly.
        """
        if self._applying:
            return
        fw, fh = self._frame
        if self.lock_check.get_active():
            self._applying = True
            try:
                self.height_spin.set_value(self.width_spin.get_value() * fh / fw)
            finally:
                self._applying = False

        w = self.width_spin.get_value()
        h = self.height_spin.get_value() or 1
        across, down = fw / w, fh / h
        skew = max(across, down) / min(across, down)
        # The honest ceiling: dots per inch actually on the sensor.
        dpi = min(across, down) * 25.4
        note = f"≈{dpi:.0f} dpi on the sensor"
        if skew > 1.02:
            note += f"  —  <b>stretched {skew:.2f}×</b>"
        self.dpi_label.set_markup(f"<small>{note}</small>")

    def _enabled_order(self) -> list[str]:
        """Enabled papers, in the order the daemon will colour them."""
        return [n for n, _w, _h in KNOWN_PAPERS
                if self.paper_checks[n].get_active()]

    def _draw_swatch(self, area, cr, width, height, name) -> None:
        order = self._enabled_order()
        if name not in order:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)   # not drawn on the video
        else:
            hexcolour = SWATCHES[order.index(name) % len(SWATCHES)]
            r = int(hexcolour[1:3], 16) / 255
            g = int(hexcolour[3:5], 16) / 255
            b = int(hexcolour[5:7], 16) / 255
            cr.set_source_rgb(r, g, b)
        cr.rectangle(0, 0, width, height)
        cr.fill()

    def _refresh_swatches(self) -> None:
        # Colours are assigned by position among the enabled sizes, so
        # ticking one box changes the colour of the ones after it.
        for swatch in self.paper_swatches.values():
            swatch.queue_draw()

    def _on_anchor(self, button, name: str) -> None:
        if button.get_active() and not self._applying:
            self._on_apply(None)

    def _say(self, text: str) -> None:
        self.status.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")

    def _load_settings(self) -> None:
        data = self.client.settings()
        self._applying = True
        try:
            self._frame = tuple(data.get("still", self._frame))
            w, h = data["coverage_mm"]
            self.width_spin.set_value(w)
            self.height_spin.set_value(h)
            active = {p[0] for p in data["papers"]}
            for name, check in self.paper_checks.items():
                check.set_active(name in active)
            anchor = data.get("anchor", "center")
            if anchor in self.anchor_buttons:
                self.anchor_buttons[anchor].set_active(True)
        finally:
            self._applying = False
        self._refresh_swatches()
        self._say(f"coverage {w:g} × {h:g} mm")

    def _on_nudge(self, _button, factor: float) -> None:
        # Width only; _sync_height derives the rest when locked. Scaling both
        # independently is what let a 2x stretch survive unnoticed.
        self.width_spin.set_value(self.width_spin.get_value() * factor)
        if not self.lock_check.get_active():
            self.height_spin.set_value(self.height_spin.get_value() * factor)
        self._on_apply(None)

    def _on_apply(self, _button) -> None:
        if self._applying:
            return
        payload = {
            "coverage_mm": [self.width_spin.get_value(),
                            self.height_spin.get_value()],
            "papers": [list(p) for p in KNOWN_PAPERS
                       if self.paper_checks[p[0]].get_active()],
            "anchor": next((n for n, b in self.anchor_buttons.items()
                            if b.get_active()), "center"),
        }
        self.apply_button.set_sensitive(False)
        self._say("applying…")

        def work():
            try:
                self.client.update(payload)
                GLib.idle_add(self._say, "applied")
            except urllib.error.HTTPError as exc:
                GLib.idle_add(self._say, f"rejected: {exc.read().decode()[:120]}")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._say, f"failed: {exc}")
            GLib.idle_add(self.apply_button.set_sensitive, True)

        threading.Thread(target=work, daemon=True).start()

    def _read_stream(self) -> None:
        """Reconnect for as long as the window is open.

        The daemon holds the stream open across a scan, but it can still end
        -- a restart, a settings change that rebuilds the pipeline -- and a
        viewer that gives up on the first drop is useless.
        """
        while not self._stop.is_set():
            try:
                for frame in self.client.stream():
                    if self._stop.is_set():
                        return
                    GLib.idle_add(self._show_frame, frame)
            except Exception:  # noqa: BLE001 - retry regardless
                pass
            self._stop.wait(1.0)

    def _show_frame(self, data: bytes) -> bool:
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        try:
            loader.write(data)
            loader.close()
        except GLib.Error:
            return False
        pixbuf = loader.get_pixbuf()
        if pixbuf is not None:
            self.picture.set_pixbuf(pixbuf)
        return False

    def _on_close(self, *_args) -> bool:
        self._stop.set()
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="camscan-escl-gui",
        description="Live view and calibration for camscan-escl.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8090",
                        help="daemon base URL (default: %(default)s)")
    args = parser.parse_args(argv)

    client = Client(args.url)
    app = Gtk.Application(application_id="uk.co.justin.camscan.escl")
    app.connect("activate", lambda a: Window(a, client).present())
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
