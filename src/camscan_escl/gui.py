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
    ("Letter", 215.9, 279.4),
    ("Legal", 215.9, 355.6),
)

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
        self.set_default_size(1180, 760)
        self._stop = threading.Event()
        self._applying = False

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.set_child(root)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        root.append(self.picture)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        root.append(self._sidebar())

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
        box.set_size_request(290, -1)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Frame coverage</b>")
        box.append(head)

        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup(
            "<small>The area the camera sees, in mm. Put a real sheet under "
            "the camera and adjust until its mark sits on the edges. Every "
            "scan's scale depends on this.</small>"
        )
        box.append(hint)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.width_spin = Gtk.SpinButton.new_with_range(20, 2000, 1)
        self.height_spin = Gtk.SpinButton.new_with_range(20, 2000, 1)
        for i, (label, spin) in enumerate((("Width", self.width_spin),
                                           ("Height", self.height_spin))):
            spin.set_digits(1)
            spin.set_hexpand(True)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            grid.attach(spin, 1, i, 1, 1)
            grid.attach(Gtk.Label(label="mm", xalign=0), 2, i, 1, 1)
        box.append(grid)

        # Proportional nudges: at a fixed camera height the frame keeps its
        # aspect ratio, so scaling both together is the move that matches
        # raising or lowering the camera.
        nudge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                        homogeneous=True)
        for label, factor in (("−5%", 0.95), ("−1%", 0.99),
                              ("+1%", 1.01), ("+5%", 1.05)):
            b = Gtk.Button(label=label)
            b.connect("clicked", self._on_nudge, factor)
            nudge.append(b)
        box.append(nudge)

        box.append(Gtk.Separator())
        papers = Gtk.Label(xalign=0)
        papers.set_markup("<b>Paper sizes</b>")
        box.append(papers)

        self.paper_checks = {}
        for name, mm_w, mm_h in KNOWN_PAPERS:
            check = Gtk.CheckButton(label=f"{name}  ({mm_w:g} × {mm_h:g} mm)")
            self.paper_checks[name] = check
            box.append(check)

        box.append(Gtk.Separator())
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply)
        box.append(self.apply_button)

        revert = Gtk.Button(label="Reload from daemon")
        revert.connect("clicked", lambda _b: self._load_settings())
        box.append(revert)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim-label")
        box.append(self.status)
        return box

    # -- behaviour ------------------------------------------------------

    def _say(self, text: str) -> None:
        self.status.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")

    def _load_settings(self) -> None:
        data = self.client.settings()
        self._applying = True
        try:
            w, h = data["coverage_mm"]
            self.width_spin.set_value(w)
            self.height_spin.set_value(h)
            active = {p[0] for p in data["papers"]}
            for name, check in self.paper_checks.items():
                check.set_active(name in active)
        finally:
            self._applying = False
        self._say(f"coverage {w:g} × {h:g} mm")

    def _on_nudge(self, _button, factor: float) -> None:
        self.width_spin.set_value(self.width_spin.get_value() * factor)
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
