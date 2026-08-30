"""Operate every control in the settings window and check the daemon agrees.

Every other GUI check in this project asked whether a widget existed, or what
size it was allocated. None of them ever pressed anything, and that is
precisely where it broke: a handler that raised the moment it fired, and then
a stray edit that left the window inert after its first action. Both passed
every test that was written at the time.

So this presses things. It needs a display and a running daemon, and skips
cleanly without either -- which does mean it will not run in CI. That is a
real limitation, not a design choice: a GUI that is never operated is a GUI
whose faults are found by the user.

    systemctl --user start camscan-escl.service && python3 -m pytest tests/test_gui_clickthrough.py
"""

import json
import os
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8090"

gi = pytest.importorskip("gi", reason="PyGObject is not installed")

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
    pytest.skip("no display to open a window on", allow_module_level=True)

# OFF unless asked for. These open a real window and present it, which raises
# it above whatever you were doing and takes the keyboard with it -- once per
# run, in the middle of your typing. Nothing else in the suite touches the
# display, so the default `pytest tests/` now leaves the desktop alone.
#
#   CAMSCAN_GUI_TESTS=1 python -m pytest tests/test_gui_clickthrough.py
if os.environ.get("CAMSCAN_GUI_TESTS") != "1":
    pytest.skip(
        "opens and focuses a real window; set CAMSCAN_GUI_TESTS=1 to run",
        allow_module_level=True,
    )

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    with urllib.request.urlopen(f"{BASE}/preview/settings", timeout=3) as r:
        json.load(r)
except (urllib.error.URLError, OSError):  # pragma: no cover - env dependent
    pytest.skip("no daemon on 8090 to talk to", allow_module_level=True)

from camscan_escl import gui  # noqa: E402


def settings() -> dict:
    with urllib.request.urlopen(f"{BASE}/preview/settings", timeout=10) as r:
        return json.load(r)


def pump(seconds: float = 1.2) -> None:
    """Let GTK dispatch, the debounce fire, and the POST land."""
    end = time.time() + seconds
    ctx = GLib.MainContext.default()
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


@pytest.fixture(scope="module")
def window():
    """A real window, driven in-process. Restores the daemon afterwards."""
    before = settings()
    app = Gtk.Application(application_id="uk.co.justin.camscan.test")
    made = {}

    def activate(a):
        made["win"] = gui.Window(a, gui.Client(BASE))
        made["win"].present()

    app.connect("activate", activate)
    app.register()
    app.activate()
    pump(1.5)
    yield made["win"]

    made["win"].close()
    urllib.request.urlopen(urllib.request.Request(
        f"{BASE}/preview/settings",
        data=json.dumps({
            "coverage_mm": before["coverage_mm"],
            "anchor": before["anchor"],
            "rotate_deg": before["rotate_deg"],
            "landscape": before["landscape"],
            "papers": before["papers"],
            "preview_mode": before["preview_mode"],
        }).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=20)


@pytest.mark.parametrize("index,degrees", list(enumerate(d for _n, d in gui.ROTATIONS)))
def test_rotation_dropdown_reaches_the_daemon(window, index, degrees):
    window.rotation.set_selected(index)
    pump()
    assert settings()["rotate_deg"] == degrees


def test_video_mode_dropdown_reaches_the_daemon(window):
    """Every mode the camera offered, driven through the dropdown.

    Not parametrized on a fixed list: the modes are this camera's, read off
    it at runtime, so the test asks the window what it was given.
    """
    assert window._modes, "the daemon offered no streaming modes"
    for index, mode in enumerate(window._modes):
        window.mode.set_selected(index)
        pump()
        assert settings()["preview_mode"] == list(mode)


def test_a_bigger_video_mode_gives_a_bigger_canvas(window):
    """The whole point of the control: more preview pixels, same rig.

    The scannable area is a property of the camera and the anchor, so it must
    NOT move when the mode does -- only the canvas the marks are drawn on.
    """
    by_pixels = sorted(window._modes, key=lambda m: m[0] * m[1])
    if len(by_pixels) < 2:
        pytest.skip("camera offers only one mappable mode")

    window.mode.set_selected(window._modes.index(by_pixels[0]))
    pump()
    small = settings()

    window.mode.set_selected(window._modes.index(by_pixels[-1]))
    pump()
    large = settings()

    assert large["preview"]["width"] > small["preview"]["width"]
    assert large["still"] == small["still"], "the scannable area moved"
    assert large["streamed_mm"] == small["streamed_mm"], "the band moved"


@pytest.mark.parametrize("want", [True, False, True])
def test_landscape_checkbox_reaches_the_daemon(window, want):
    window.landscape_check.set_active(want)
    pump()
    assert settings()["landscape"] is want


@pytest.mark.parametrize("anchor", ["top-left", "center", "bottom-right", "right"])
def test_anchor_grid_reaches_the_daemon(window, anchor):
    window.anchor_buttons[anchor].set_active(True)
    pump()
    assert settings()["anchor"] == anchor


def test_paper_checkboxes_reach_the_daemon(window):
    for check in window.paper_checks.values():
        check.set_active(False)
    pump()
    window.paper_checks["A4"].set_active(True)
    window.paper_checks["B7"].set_active(True)
    pump(1.6)
    assert sorted(p[0] for p in settings()["papers"]) == ["A4", "B7"]


def test_nudge_button_widens_the_coverage(window):
    window.rotation.set_selected(0)
    pump(1.4)
    before = settings()["coverage_mm"][0]
    for widget in _walk(window):
        if isinstance(widget, Gtk.Button) and widget.get_label() == "+5%":
            widget.emit("clicked")
            break
    else:  # pragma: no cover - the button is built unconditionally
        pytest.fail("no +5% button in the window")
    pump(1.6)
    assert settings()["coverage_mm"][0] / before == pytest.approx(1.05, rel=1e-3)


def test_derived_height_keeps_the_frames_shape(window):
    # The bug this guards: a coverage of a different shape to the frame
    # stretches every scan, silently, and was shipped twice.
    window.rotation.set_selected(0)
    pump(1.4)
    now = settings()
    cov, still = now["coverage_mm"], now["still"]
    assert cov[0] / cov[1] == pytest.approx(still[0] / still[1], rel=1e-3)


def test_turning_the_camera_flips_the_derived_shape(window):
    window.rotation.set_selected(2)          # +90
    pump(1.6)
    now = settings()
    cov, still = now["coverage_mm"], now["still"]
    assert cov[0] / cov[1] == pytest.approx(still[1] / still[0], rel=1e-3)


def status() -> dict:
    with urllib.request.urlopen(f"{BASE}/preview/status", timeout=10) as r:
        return json.load(r)


def test_stop_and_start_buttons_drive_the_daemon(window):
    """The controls that were missing when the preview died silently.

    Ordered stop-then-start so the fixture leaves the preview running, which
    is the state every other test in this file needs.
    """
    assert status()["running"] is True

    window.stop_button.emit("clicked")
    pump(3.0)
    assert status()["running"] is False
    # The window must SAY so, not merely be right internally.
    assert "not running" in window.health_label.get_text().lower()

    window.start_button.emit("clicked")
    pump(9.0)
    live = status()
    assert live["running"] is True
    assert live["healthy"] is True
    assert "streaming" in window.health_label.get_text().lower()


def test_restart_button_brings_the_pipeline_back(window):
    window.restart_button.emit("clicked")
    pump(9.0)
    assert status()["healthy"] is True


def test_details_panel_names_the_resolved_devices(window):
    # The sentence that would have ended the outage in one glance.
    pump(2.5)
    text = window.details.get_text()
    assert "camera:" in text
    assert "/dev/" in text
    assert "V4L2 devices:" in text


def _walk(widget):
    yield widget
    child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
    while child:
        yield from _walk(child)
        child = child.get_next_sibling()
