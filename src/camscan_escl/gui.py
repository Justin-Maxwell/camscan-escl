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
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
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

# Camera rotation, as offered in the dropdown.
#
# The LABEL is what the user sees happen: +90° turns the picture CLOCKWISE.
# The VALUE is capture.rotate_deg, and PIL's `rotate` and ffmpeg's `cclock`
# both count counter-clockwise, so the two run opposite ways on purpose.
# They used to agree, which meant the dropdown's "+90°" turned the picture
# anticlockwise and the labels were simply wrong.
#
# Only the labelling changed: a config already holding rotate_deg = 270 shows
# as "+90°" now instead of "−90°", and the picture it produces is identical.
ROTATIONS = (("None", 0), ("−90°", 90), ("+90°", 270), ("180°", 180))

ANCHORS = (
    ("top-left", "top", "top-right"),
    ("left", "center", "right"),
    ("bottom-left", "bottom", "bottom-right"),
)

WINDOW_W = 1180
SIDEBAR_W = 246

SOI, EOI = b"\xff\xd8", b"\xff\xd9"

# The systemd user unit, for the case the GUI cannot reach the daemon at all.
UNIT = "camscan-escl.service"

# How often to ask the daemon how it is doing. Two seconds is fast enough that
# pressing Start feels answered and slow enough to be free.
POLL_MS = 2000


class Client:
    """The daemon's HTTP API, such as it is."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def settings(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/preview/settings", timeout=10) as r:
            return json.load(r)

    def status(self) -> dict:
        """Why the preview is or is not working, from the daemon's own mouth."""
        with urllib.request.urlopen(f"{self.base}/preview/status", timeout=10) as r:
            return json.load(r)

    def _command(self, verb: str, timeout: int = 30) -> dict:
        """POST one of the lifecycle verbs. A 503 is an answer, not a crash.

        `start` legitimately returns 503 when the pipeline will not come up,
        and its body is the status object explaining why -- which is the whole
        point, so it must not be thrown away as an HTTPError.
        """
        req = urllib.request.Request(
            f"{self.base}/preview/{verb}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return json.loads(body)
            except ValueError:
                return {"summary": f"{verb} failed: HTTP {exc.code}",
                        "healthy": False, "running": False}

    def controls(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/preview/controls", timeout=10) as r:
            return json.load(r)

    def set_controls(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/preview/controls",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def auto_controls(self) -> dict:
        # Generous: the metering loop takes a fresh frame per step, and a
        # step costs a couple of frame intervals plus a v4l2-ctl call.
        return self._command("controls/auto", timeout=60)

    def start(self) -> dict:
        return self._command("start")

    def stop(self) -> dict:
        return self._command("stop")

    def restart(self) -> dict:
        return self._command("restart")

    @property
    def is_local(self) -> bool:
        """Whether systemctl on this machine could plausibly control it.

        A GUI pointed at another host can ask the daemon to restart its
        preview over HTTP, but it cannot start a daemon that is not answering
        -- there is nothing to ask. Offering a systemd button in that case
        would just fail confusingly.
        """
        host = urllib.parse.urlsplit(self.base).hostname or ""
        return host in ("127.0.0.1", "::1", "localhost", socket.gethostname())

    def update(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/preview/settings",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    # Frames arrive at the preview frame rate, so a read that blocks for more
    # than a few seconds means the pipeline has stalled. This was 30s, which
    # is why a dead preview looked like a viewer reconnecting every 31
    # seconds -- and why nothing on screen ever said so.
    STREAM_TIMEOUT = 8

    def stream(self):
        """Yield JPEG frames from the daemon's MJPEG endpoint."""
        with urllib.request.urlopen(f"{self.base}/preview/stream",
                                    timeout=self.STREAM_TIMEOUT) as r:
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
        # One status request in flight at a time. Without this a slow or
        # hanging daemon would have the poller stacking threads on it.
        self._polling = False
        self._status: dict | None = None
        self._can_systemctl = bool(shutil.which("systemctl")) and client.is_local
        self._frame = (2304, 1536)   # replaced by the daemon's real value
        self._apply_timer = None
        self._control_timer = None
        self._control_ranges: dict = {}
        self._rotate = 0             # capture.rotate_deg, from the daemon
        # Handlers fire while the sidebar is still being built -- a DropDown
        # emits notify::selected as its model is set -- and the sections are
        # built in reading order, so the anchor's controls exist before the
        # coverage widgets they want to update. Nothing acts until the whole
        # sidebar is assembled.
        self._ready = False

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

        # Scrolled, because the sidebar can now be taller than the window --
        # picture controls and daemon controls pushed it past 987px against a
        # 760px default, and GTK responded by growing the window until the
        # video pane was the only thing left on screen. Scrolling bounds it
        # instead of letting it shove the picture out of the way.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._sidebar())
        scroller.set_propagate_natural_width(True)
        scroller.set_vexpand(True)
        root.set_end_child(scroller)
        root.set_resize_end_child(False)
        # Shrinkable, because GtkPaned insists on measuring the end child one
        # pixel below its minimum and warning about it. Letting it shrink
        # costs nothing -- the divider can be dragged back.
        root.set_shrink_end_child(True)
        # SIDEBAR_W plus its margins plus the wide handle. Budgeted
        # generously rather than exactly: being a pixel short makes GTK warn
        # that it cannot honour the minimum, and the sidebar does not expand
        # into any slack anyway.
        root.set_position(WINDOW_W - (SIDEBAR_W + 28 + 16))

        self.connect("close-request", self._on_close)
        try:
            self._load_settings()
        except Exception as exc:  # noqa: BLE001 - show it, do not crash
            self._say(f"cannot reach daemon: {exc}")
        threading.Thread(target=self._read_stream, daemon=True).start()
        # Ask immediately, then keep asking. The first answer is what turns
        # "the window is blank" into "the loopback could not be resolved".
        self._poll_status()
        self._poll_id = GLib.timeout_add(POLL_MS, self._poll_status)
        self._load_controls()

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
        # No hard minimum. The labels are capped in characters, which sets
        # the natural width already, and a minimum only gave GtkPaned
        # something to measure one pixel short of and warn about.
        box.set_hexpand(False)
        # Natural height, so the controls do not sit above a slab of nothing.
        box.set_valign(Gtk.Align.START)

        # Order follows the job: choose the sizes, say where in the frame,
        # then size the frame to match.
        box.append(self._papers_section())
        box.append(Gtk.Separator())
        box.append(self._anchor_section())
        box.append(Gtk.Separator())
        box.append(self._coverage_section())

        # The running commentary belongs with the coverage controls it talks
        # about -- how many dpi, which papers will not fit -- and not after
        # the daemon panel, which has to be the last thing in the sidebar.
        self.status = Gtk.Label(xalign=0, wrap=True, max_width_chars=30,
                                width_chars=30)
        self.status.add_css_class("dim-label")
        box.append(self.status)

        box.append(Gtk.Separator())
        box.append(self._picture_section())

        # Daemon controls last. They are what you need on the day the picture
        # is missing, and clutter on every other day -- and the calibration
        # controls above are what the window is actually for.
        box.append(Gtk.Separator())
        box.append(self._daemon_section())
        self._ready = True
        return box

    def _heading(self, text: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<b>{text}</b>")
        return label

    def _note(self, text: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, width_chars=30)
        label.set_markup(f"<small>{text}</small>")
        return label

    def _picture_section(self) -> Gtk.Widget:
        """Brightness and contrast, as controls on the camera itself.

        Not ffmpeg filters. These reach the scan as well as the preview,
        which is the point -- brightening only the preview would make the
        window lie about what a scan is going to look like.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._heading("Picture"))

        self.control_scales: dict[str, Gtk.Scale] = {}
        self._control_handlers: dict[str, int] = {}
        grid = Gtk.Grid(column_spacing=8, row_spacing=2)
        for row, name in enumerate(("brightness", "contrast")):
            label = Gtk.Label(label=name.capitalize(), xalign=0)
            # Range replaced from the daemon's reading of the device; 0..255
            # is the C920's and not a V4L2 guarantee.
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 255, 1)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_hexpand(False)
            scale.set_size_request(150, -1)
            scale.set_sensitive(False)      # until the daemon reports a range
            handler = scale.connect("value-changed", self._on_control, name)
            self._control_handlers[name] = handler
            self.control_scales[name] = scale
            grid.attach(label, 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)
        box.append(grid)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.auto_button = Gtk.Button(label="Auto")
        self.auto_button.set_tooltip_text(
            "Meter the live picture and choose both. Aims to put paper just "
            "below clipping, with enough range left for the print on it. "
            "Needs the preview running and a page under the camera.")
        self.auto_button.connect("clicked", self._on_auto)
        self.auto_button.set_size_request(70, 34)
        row.append(self.auto_button)
        self.reset_button = Gtk.Button(label="Reset")
        self.reset_button.set_tooltip_text(
            "Hand both controls back to the camera's own defaults.")
        self.reset_button.connect("clicked", self._on_reset_controls)
        self.reset_button.set_size_request(70, 34)
        row.append(self.reset_button)
        box.append(row)

        self.picture_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=30,
                                      width_chars=30)
        self.picture_note.add_css_class("dim-label")
        box.append(self.picture_note)
        return box

    def _daemon_section(self) -> Gtk.Widget:
        """Is it working, why not, and the two buttons that might fix it."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._heading("Daemon"))

        # A coloured dot beside a sentence. The dot is for the glance across
        # the room; the sentence is for actually fixing it.
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.health_dot = Gtk.DrawingArea()
        self.health_dot.set_size_request(12, 12)
        self.health_dot.set_valign(Gtk.Align.START)
        self.health_dot.set_margin_top(4)
        self.health_dot.set_draw_func(self._draw_health)
        self._health = None          # None unknown, True up, False down
        line.append(self.health_dot)
        self.health_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=28,
                                      width_chars=28)
        self.health_label.set_markup("<small>checking…</small>")
        line.append(self.health_label)
        box.append(line)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        buttons.set_halign(Gtk.Align.START)
        self.start_button = Gtk.Button(label="Start")
        self.start_button.set_tooltip_text(
            "Start the preview pipeline. If the daemon itself is not "
            "answering, this starts its systemd user unit instead.")
        self.start_button.connect("clicked", self._on_start)
        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.set_tooltip_text(
            "Stop the preview and hand the camera back, so another "
            "application can open it. Scanning still works.")
        self.stop_button.connect("clicked", self._on_stop)
        self.restart_button = Gtk.Button(label="Restart")
        self.restart_button.set_tooltip_text(
            "Rebuild the pipeline. Device numbers are resolved afresh, so "
            "this is what to press after replugging the camera.")
        self.restart_button.connect("clicked", self._on_restart)
        for b in (self.start_button, self.stop_button, self.restart_button):
            b.set_size_request(70, 34)
            buttons.append(b)
        box.append(buttons)

        # Collapsed by default: the detail matters on the day it matters, and
        # is clutter every other day.
        self.details = Gtk.Label(xalign=0, wrap=True, max_width_chars=34,
                                 width_chars=34, selectable=True)
        self.details.add_css_class("dim-label")
        self.details.set_markup("<small>no detail yet</small>")
        expander = Gtk.Expander(label="Details")
        expander.set_child(self.details)
        box.append(expander)
        return box

    def _draw_health(self, _area, cr, width, height) -> None:
        if self._health is None:
            cr.set_source_rgb(0.55, 0.55, 0.55)
        elif self._health:
            cr.set_source_rgb(0.30, 0.75, 0.35)
        else:
            cr.set_source_rgb(0.85, 0.25, 0.25)
        radius = min(width, height) / 2
        cr.arc(width / 2, height / 2, radius, 0, 2 * 3.141592653589793)
        cr.fill()

    def _papers_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._heading("Paper sizes"))

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
            check.connect("toggled", self._on_paper_toggled)
            self.paper_checks[name] = check
            row.append(swatch)
            row.append(check)
            box.append(row)
        return box

    def _anchor_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._heading("Anchor"))

        # Camera rotation sits beside the grid because the two are read
        # together: turning the camera turns the frame, so it changes which
        # corner every anchor names and which way a Landscape page lies.
        rot_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rot_row.append(Gtk.Label(label="Camera", xalign=0))
        self.rotation = Gtk.DropDown.new_from_strings(
            [label for label, _deg in ROTATIONS])
        self.rotation.set_tooltip_text(
            "Turn the picture at the head of the pipeline, for a camera "
            "mounted on its side. Everything downstream — the marks, the "
            "anchor, the scan — then shares one upright view.")
        self.rotation.connect("notify::selected", self._on_rotation)
        rot_row.append(self.rotation)
        box.append(rot_row)

        self.landscape_check = Gtk.CheckButton(label="Landscape")
        self.landscape_check.set_tooltip_text(
            "Lay the crop marks across the frame, for landscape pages. "
            "Nothing is rotated: ask the client for a landscape page size "
            "and eSCL carries the orientation in the region itself.")
        self.landscape_check.connect("toggled", self._on_landscape)
        box.append(self.landscape_check)

        agrid = Gtk.Grid(column_spacing=10, row_spacing=4)
        agrid.set_halign(Gtk.Align.START)
        self.anchor_buttons = {}
        first = None
        for r, row in enumerate(ANCHORS):
            for c, name in enumerate(row):
                b = Gtk.CheckButton()
                b.set_tooltip_text(name)
                b.set_halign(Gtk.Align.CENTER)
                if first is None:
                    first = b
                else:
                    b.set_group(first)
                b.connect("toggled", self._on_anchor, name)
                self.anchor_buttons[name] = b
                agrid.attach(b, c, r, 1, 1)
        box.append(agrid)
        return box

    def _coverage_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._heading("Frame coverage"))
        box.append(self._note("Adjust to fit frame to a same-size sheet."))

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
        # Height is derived from the width and the frame's shape, so it is
        # shown rather than typed into.
        self.height_spin.set_sensitive(False)
        self.height_spin.set_tooltip_text(
            "Derived from the width and the shape of the frame.")
        self.width_spin.connect("value-changed", self._on_width_changed)
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
        return box

    # -- behaviour ------------------------------------------------------

    def _on_width_changed(self, _spin) -> None:
        self._sync_height()
        self._schedule_apply()

    def _sync_height(self) -> None:
        """Keep the coverage the same shape as the frame, and report the dpi.

        A coverage whose aspect differs from the frame's means one millimetre
        is a different number of pixels across than down, and every scan comes
        out stretched by that ratio. It was 2.05x on this rig before anyone
        noticed, because the nudge buttons scaled both axes together and so
        preserved the error perfectly.
        """
        if self._applying or not self._ready:
            return
        fw, fh = self._frame
        # NOT swapped for the Landscape tick. That only lays the marks across
        # the frame; the camera's field of view is the same physical area
        # either way. Swapping here made the coverage portrait-shaped against
        # a landscape frame and stretched every scan 2.25x. Only a physically
        # turned camera -- capture.rotate_deg -- changes which way round the
        # frame is.
        if self._rotate % 180 == 90:
            fw, fh = fh, fw
        # Always locked. With the camera square-on a millimetre is the same
        # number of pixels in both directions, so a coverage of a different
        # shape to the frame just means every scan comes out stretched --
        # 2.05x on this rig before anyone noticed. Not a choice worth having.
        self._applying = True
        try:
            self.height_spin.set_value(self.width_spin.get_value() * fh / fw)
        finally:
            self._applying = False
        self._update_dpi_note()

    def _update_dpi_note(self) -> None:
        """Report the honest resolution. Read-only, so it is safe on load.

        Split out of `_sync_height` because that returns early while settings
        are being loaded -- which is exactly when the note was wanted, and why
        it sat empty until the first time a control was touched.
        """
        fw, fh = self._frame
        if self._rotate % 180 == 90:
            fw, fh = fh, fw
        w = self.width_spin.get_value() or 1
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

    def _on_paper_toggled(self, _check) -> None:
        self._refresh_swatches()
        self._schedule_apply()

    def _refresh_swatches(self) -> None:
        # Colours are assigned by position among the enabled sizes, so
        # ticking one box changes the colour of the ones after it.
        for swatch in self.paper_swatches.values():
            swatch.queue_draw()

    def _on_rotation(self, dropdown, _param) -> None:
        idx = dropdown.get_selected()
        if idx < len(ROTATIONS):
            self._rotate = ROTATIONS[idx][1]
        # A turned camera turns the frame, so the derived height flips too.
        self._sync_height()
        self._schedule_apply()

    def _on_landscape(self, _button) -> None:
        # Rotation changes which way round the frame is, so the derived
        # height must be recomputed before anything is sent.
        self._sync_height()
        self._schedule_apply()

    def _on_anchor(self, button, name: str) -> None:
        if button.get_active():
            self._schedule_apply()

    def _say(self, text: str) -> None:
        self.status.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")

    # -- camera picture controls -----------------------------------------

    def _load_controls(self) -> None:
        """Fetch the real ranges and current values, off the main thread."""
        def work():
            try:
                data = self.client.controls()
                GLib.idle_add(self._apply_controls, data)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._note_picture, f"controls unavailable: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _apply_controls(self, data: dict) -> bool:
        ranges = data.get("ranges") or {}
        self._control_ranges = ranges
        if not ranges:
            self._note_picture("this camera exposes no image controls")
            for scale in self.control_scales.values():
                scale.set_sensitive(False)
            self.auto_button.set_sensitive(False)
            self.reset_button.set_sensitive(False)
            return False

        wanted = data.get("values") or {}
        actual = data.get("actual") or {}
        for name, scale in self.control_scales.items():
            entry = ranges.get(name)
            if entry is None:
                scale.set_sensitive(False)
                continue
            scale.set_sensitive(True)
            # Setting a range or a value re-emits value-changed, which would
            # POST straight back to the daemon and fight the user's drag.
            scale.handler_block(self._control_handlers[name])
            try:
                scale.set_range(entry["min"], entry["max"])
                scale.set_increments(1, max(1, (entry["max"] - entry["min"]) // 10))
                # Prefer what the camera IS set to over what the config asked
                # for: the two differ when a control was refused, or left over
                # from another process, since V4L2 state lives on the device.
                current = wanted.get(name)
                if current is None:
                    current = actual.get(name, entry.get("default"))
                if current is not None:
                    scale.set_value(current)
            finally:
                scale.handler_unblock(self._control_handlers[name])
        self.auto_button.set_sensitive(True)
        self.reset_button.set_sensitive(True)
        return False

    def _note_picture(self, text: str) -> bool:
        self.picture_note.set_markup(
            f"<small>{GLib.markup_escape_text(text)}</small>")
        return False

    def _on_control(self, _scale, _name: str) -> None:
        # Debounced, but only lightly: these are device-side controls, so
        # nothing rebuilds and a POST costs a v4l2-ctl call. The delay is
        # here to avoid one request per pixel of drag, not to protect a
        # pipeline restart the way the coverage controls must.
        if self._applying or not self._ready:
            return
        if self._control_timer:
            GLib.source_remove(self._control_timer)
        self._control_timer = GLib.timeout_add(150, self._send_controls)

    def _send_controls(self) -> bool:
        self._control_timer = None
        payload = {name: int(scale.get_value())
                   for name, scale in self.control_scales.items()
                   if scale.get_sensitive()}
        if not payload:
            return False

        def work():
            try:
                self.client.set_controls(payload)
                GLib.idle_add(self._note_picture, ", ".join(
                    f"{k} {v}" for k, v in sorted(payload.items())))
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._note_picture, f"failed: {exc}")

        threading.Thread(target=work, daemon=True).start()
        return False

    def _on_reset_controls(self, _button) -> None:
        """Hand both controls back to the camera's own defaults."""
        payload = {name: entry.get("default")
                   for name, entry in (self._control_ranges or {}).items()
                   if entry.get("default") is not None}
        if not payload:
            return
        for name, value in payload.items():
            scale = self.control_scales.get(name)
            if scale is None:
                continue
            scale.handler_block(self._control_handlers[name])
            try:
                scale.set_value(value)
            finally:
                scale.handler_unblock(self._control_handlers[name])
        self._note_picture("reset to camera defaults")
        self._send_controls()

    def _on_auto(self, _button) -> None:
        self.auto_button.set_sensitive(False)
        self._note_picture("metering…")

        def work():
            try:
                data = self.client.auto_controls()
                GLib.idle_add(self._after_auto, data)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._after_auto, {"error": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _after_auto(self, data: dict) -> bool:
        self.auto_button.set_sensitive(True)
        if data.get("error"):
            return self._note_picture(f"auto failed: {data['error']}")
        auto = data.get("auto") or {}
        applied = auto.get("applied") or {}
        if not applied:
            # The daemon answers 503 with a plain-text reason when the
            # preview is down, which _command turns into a summary.
            return self._note_picture(
                data.get("summary") or "auto could not meter the picture")
        self._apply_controls(data)
        measured = auto.get("measured") or {}
        note = ", ".join(f"{k} {v}" for k, v in sorted(applied.items()))
        if "luma" in measured:
            note += f"  —  paper at {measured['luma']:.0f}/255"
        return self._note_picture(note)

    # -- daemon health ---------------------------------------------------

    def _poll_status(self) -> bool:
        """Ask the daemon how it is, off the main thread. Repeats forever."""
        if self._stop.is_set():
            return False
        if not self._polling:
            self._polling = True

            def work():
                try:
                    status = self.client.status()
                    GLib.idle_add(self._apply_status, status, None)
                except Exception as exc:  # noqa: BLE001 - unreachable is a state
                    GLib.idle_add(self._apply_status, None, str(exc))

            threading.Thread(target=work, daemon=True).start()
        return True

    def _apply_status(self, status: dict | None, error: str | None) -> bool:
        self._polling = False
        self._status = status

        if status is None:
            self._health = False
            self._set_health(f"daemon not answering: {error}")
            hint = "The daemon is not reachable at all."
            if self._can_systemctl:
                hint += f"\n\nPress Start to run:\nsystemctl --user start {UNIT}"
            self._set_details(hint)
            self.start_button.set_sensitive(True)
            self.stop_button.set_sensitive(False)
            self.restart_button.set_sensitive(self._can_systemctl)
            return False

        healthy = bool(status.get("healthy"))
        self._health = healthy
        self._set_health(status.get("summary", "no summary"))
        self._set_details(self._describe(status))
        # Start is pointless while it is already streaming, and Stop is
        # pointless while it is not.
        self.start_button.set_sensitive(not status.get("running"))
        self.stop_button.set_sensitive(bool(status.get("running")))
        self.restart_button.set_sensitive(True)
        return False

    def _set_health(self, text: str) -> None:
        self.health_label.set_markup(
            f"<small>{GLib.markup_escape_text(text)}</small>")
        self.health_dot.queue_draw()

    def _set_details(self, text: str) -> None:
        self.details.set_markup(
            f"<small><tt>{GLib.markup_escape_text(text)}</tt></small>")

    @staticmethod
    def _describe(status: dict) -> str:
        """The status object as something readable in a narrow column."""
        lines = []
        for entry in status.get("devices", []):
            mark = "ok " if entry.get("ok") else "!! "
            target = entry.get("resolved") or "unresolved"
            lines.append(f"{mark}{entry.get('name')}: {target}")
            if not entry.get("ok") and entry.get("detail"):
                lines.append(f"   {entry['detail']}")

        if status.get("inventory"):
            lines.append("")
            lines.append("V4L2 devices:")
            for entry in status["inventory"]:
                lines.append(
                    f"  {entry['path']}  {entry['card']} [{entry['kind']}]")

        facts = []
        if status.get("pid"):
            facts.append(f"pid {status['pid']}")
        if status.get("frames_seen") is not None:
            facts.append(f"{status['frames_seen']} frames")
        if status.get("uptime_s") is not None:
            facts.append(f"up {status['uptime_s']}s")
        if status.get("exit_code") is not None:
            facts.append(f"exit {status['exit_code']}")
        if facts:
            lines.append("")
            lines.append(", ".join(facts))

        if status.get("stderr_tail"):
            lines.append("")
            lines.append("ffmpeg said:")
            lines += [f"  {line}" for line in status["stderr_tail"]]
        return "\n".join(lines) or "no detail"

    def _lifecycle(self, verb: str) -> None:
        """Run a start/stop/restart without freezing the window.

        Start waits for a real frame on the daemon side, which can take a few
        seconds, and a GUI that locks up while it happens looks broken in the
        same way the thing it is fixing looked broken.
        """
        self._set_health(f"{verb}ing…")
        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(False)
        self.restart_button.set_sensitive(False)

        def work():
            try:
                status = getattr(self.client, verb)()
                GLib.idle_add(self._apply_status, status, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._after_http_lifecycle_failed, verb, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _after_http_lifecycle_failed(self, verb: str, error: str) -> bool:
        """HTTP could not do it. Fall back to systemd if this is the same host.

        This is the case the window was missing entirely: the daemon is not
        merely unhealthy, it is not there, and nothing in the GUI could do
        anything about it.
        """
        if self._can_systemctl and verb in ("start", "restart"):
            self._set_health(f"daemon unreachable; running systemctl {verb}…")
            threading.Thread(target=self._systemctl, args=(verb,),
                             daemon=True).start()
            return False
        self._apply_status(None, error)
        return False

    def _systemctl(self, verb: str) -> None:
        try:
            result = subprocess.run(
                ["systemctl", "--user", verb, UNIT],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            GLib.idle_add(self._apply_status, None, f"systemctl failed: {exc}")
            return
        if result.returncode != 0:
            message = result.stderr.strip() or f"exit {result.returncode}"
            GLib.idle_add(self._apply_status, None, f"systemctl: {message}")
            return
        # The unit is up; the next poll reports what the daemon actually says.
        GLib.idle_add(self._set_health, f"systemctl {verb} ok, waiting…")
        GLib.timeout_add(1500, self._poll_status)

    def _on_start(self, _button) -> None:
        self._lifecycle("start")

    def _on_stop(self, _button) -> None:
        self._lifecycle("stop")

    def _on_restart(self, _button) -> None:
        self._lifecycle("restart")

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
            self._rotate = int(data.get("rotate_deg", 0))
            for i, (_label, deg) in enumerate(ROTATIONS):
                if deg == self._rotate % 360:
                    self.rotation.set_selected(i)
                    break
            self.landscape_check.set_active(bool(data.get("landscape", False)))
            anchor = data.get("anchor", "center")
            if anchor in self.anchor_buttons:
                self.anchor_buttons[anchor].set_active(True)
        finally:
            self._applying = False
        self._refresh_swatches()
        self._update_dpi_note()
        self._show_fit(data)

    def _show_fit(self, data: dict) -> None:
        """Say in words what the zoom-out used to say by padding the frame.

        A paper too big for what the camera sees is now clipped rather than
        shrunk into view, so nothing on screen announces it any more. The
        numbers do: how wide the sheet is against how wide the camera's view
        actually is.
        """
        # One decimal. The nudge buttons multiply, so the stored value carries
        # a tail of digits no ruler can measure and no reader wants to see.
        coverage = data.get("coverage_mm") or [0, 0]
        note = f"coverage {coverage[0]:.1f} × {coverage[1]:.1f} mm"
        streamed = data.get("streamed_mm")
        if streamed:
            # The difference between these two is the border: scannable, but
            # only ever seen as the last scan rather than live.
            note += f"\nlive stream {streamed[0]:.1f} × {streamed[1]:.1f} mm"
        oversize = data.get("does_not_fit") or []
        if oversize:
            names = ", ".join(entry["name"] for entry in oversize)
            available = oversize[0]["available"]
            note += (f"\n{names} will not fit: the camera sees only "
                     f"{available[0]:.1f} × {available[1]:.1f} mm. Raise it.")
        self._say(note)

    def _on_nudge(self, _button, factor: float) -> None:
        # Width only; _sync_height derives the height. Scaling both
        # independently is what let a 2x stretch survive unnoticed.
        self.width_spin.set_value(self.width_spin.get_value() * factor)

    def _schedule_apply(self) -> None:
        """Apply shortly after the last change, not on every keystroke.

        Each change restarts the ffmpeg pipeline, so holding a nudge button
        would otherwise queue a restart per click.
        """
        if self._applying or not self._ready:
            return
        if self._apply_timer:
            GLib.source_remove(self._apply_timer)
        self._apply_timer = GLib.timeout_add(350, self._apply_now)

    def _apply_now(self) -> bool:
        self._apply_timer = None
        self._on_apply(None)
        return False

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
            # Marks only. NAPS2 can define a landscape page size, and eSCL
            # carries orientation in the region's own dimensions, so the
            # client asks for a wide region and nothing needs rotating --
            # which is what keeps up on the preview being up on the scan.
            "landscape": self.landscape_check.get_active(),
            "rotate_deg": self._rotate,
        }
        self._say("applying…")

        def work():
            try:
                # The response is the new settings, so the fit note refreshes
                # from what the daemon actually accepted rather than from what
                # was sent -- which is the only version that can be trusted.
                data = self.client.update(payload)
                GLib.idle_add(self._show_fit, data)
            except urllib.error.HTTPError as exc:
                GLib.idle_add(self._say, f"rejected: {exc.read().decode()[:120]}")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._say, f"failed: {exc}")

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
                reason = "the stream ended"
            except urllib.error.HTTPError as exc:
                # 503 carries the daemon's own explanation in the body, which
                # is the sentence worth showing: which device would not
                # resolve, or what ffmpeg said before it died.
                try:
                    reason = exc.read().decode("utf-8", "replace").strip()
                except OSError:
                    reason = f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001 - retry regardless
                reason = str(exc)
            # Reported, not swallowed. A bare `pass` here is what let a broken
            # preview look like an idle one for a whole session.
            if not self._stop.is_set():
                GLib.idle_add(self._say, f"no video: {reason}")
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
        if getattr(self, "_poll_id", None):
            GLib.source_remove(self._poll_id)
            self._poll_id = None
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
