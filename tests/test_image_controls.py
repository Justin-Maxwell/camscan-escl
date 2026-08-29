"""Brightness and contrast: parsing the device's ranges, and metering.

The metering loop can only be tested against a simulated camera. The real one
needs a rig, a page, and the light in the room, which is exactly why the loop
exists -- but it also means every property worth asserting has to be asserted
here.
"""

from __future__ import annotations

import subprocess

import pytest
from PIL import Image

from camscan_escl import capture

# Real `v4l2-ctl --list-ctrls` output from the C920 on this rig.
LIST_CTRLS = """
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=140 flags=has-min-max
                       contrast 0x00980901 (int)    : min=0 max=255 step=1 default=128 value=128 flags=has-min-max
                     saturation 0x00980902 (int)    : min=0 max=255 step=1 default=128 value=128 flags=has-min-max
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
                 zoom_absolute 0x009a090d (int)    : min=100 max=500 step=1 default=100 value=100 flags=has-min-max
"""


def test_control_ranges_are_read_from_the_device(monkeypatch):
    monkeypatch.setattr(
        capture.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, LIST_CTRLS, ""))
    ranges = capture.control_ranges("/dev/video1")

    assert set(ranges) == {"brightness", "contrast"}, "only what the GUI drives"
    assert ranges["brightness"] == {
        "name": "brightness", "min": 0, "max": 255, "step": 1,
        "default": 128, "value": 140,
    }
    # `value` is what the camera IS set to, which is not the config's request:
    # V4L2 state lives on the device and survives across processes.
    assert ranges["brightness"]["value"] != ranges["brightness"]["default"]


def test_control_ranges_survives_a_missing_v4l2_ctl(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no v4l2-ctl here")

    monkeypatch.setattr(capture.subprocess, "run", boom)
    # Best-effort throughout: a camera that will not report its controls still
    # takes photographs.
    assert capture.control_ranges("/dev/video1") == {}


class FakeCamera:
    """A scene the controls actually act on, so metering has something to find.

    luma = clamp((scene - 128) * (contrast / 128) + 128 + (brightness - 128))

    which is the shape of what brightness and contrast do on a real sensor:
    contrast scales about the mid-point, brightness shifts the whole thing.
    """

    def __init__(self, paper=150, ink=60):
        self.values = {"brightness": 128, "contrast": 128}
        self.paper, self.ink = paper, ink
        self.frames = 0

    def set(self, name, value):
        self.values[name] = value

    def _render(self, level: float) -> int:
        gain = self.values["contrast"] / 128
        offset = self.values["brightness"] - 128
        return int(max(0, min(255, (level - 128) * gain + 128 + offset)))

    def grab(self):
        self.frames += 1
        # Paper fills the frame; a band of ink gives it something to spread.
        # The ink must fall inside the middle half, because that is the only
        # part `meter` and `spread` look at -- the published preview has the
        # dimmed dead zone burned into its edges.
        image = Image.new("L", (200, 200), self._render(self.paper))
        ink = self._render(self.ink)
        for y in range(100, 200):
            for x in range(200):
                image.putpixel((x, y), ink)
        return image


RANGES = {
    "brightness": {"min": 0, "max": 255, "default": 128},
    "contrast": {"min": 0, "max": 255, "default": 128},
}


def test_auto_balance_brings_paper_close_to_the_target():
    camera = FakeCamera()
    before = capture.meter(camera.grab())
    result = capture.auto_balance(camera.grab, camera.set, RANGES)

    assert "brightness" in result["applied"]
    after = capture.meter(camera.grab())
    assert abs(after - capture.TARGET_LUMA) < abs(before - capture.TARGET_LUMA)
    # Close enough that paper reads as paper.
    assert abs(after - capture.TARGET_LUMA) <= 8, after


def test_auto_balance_does_not_clip_the_highlights():
    """232, not 255. A clipped highlight has lost the texture of the paper."""
    camera = FakeCamera(paper=250)      # already far too bright
    capture.auto_balance(camera.grab, camera.set, RANGES)
    assert capture.meter(camera.grab()) < 255


def test_auto_balance_lifts_a_dark_scene():
    camera = FakeCamera(paper=70, ink=20)
    before = capture.meter(camera.grab())
    capture.auto_balance(camera.grab, camera.set, RANGES)
    after = capture.meter(camera.grab())
    # Cannot reach the target -- the scene is darker than the controls can
    # lift -- but it must get most of the way and it must not go backwards.
    assert after > before + 100
    assert after > 180, after


def test_brightness_is_trimmed_after_contrast_moves_the_level():
    """Contrast shifts the level too, so brightness has to be settled last."""
    camera = FakeCamera()
    order = []

    def record(name, value):
        order.append(name)
        camera.set(name, value)

    capture.auto_balance(camera.grab, record, RANGES)
    assert order, "nothing was set"
    assert "contrast" in order
    assert order[-1] == "brightness", "the level must be settled after contrast"


def test_contrast_is_not_driven_to_the_point_of_crushing_the_shadows():
    """A bisection towards a spread target did exactly this, measured.

    Chasing an unreachable spread on a dark, flat scene drove contrast to its
    maximum, clamped the ink to 0, and left the picture flatter than it
    started. The scan picks the best spread actually observed instead of
    chasing a number it cannot reach.
    """
    camera = FakeCamera(paper=70, ink=20)     # dark and flat
    brightness_only = FakeCamera(paper=70, ink=20)
    capture.auto_balance(brightness_only.grab, brightness_only.set,
                         {"brightness": RANGES["brightness"]})
    capture.auto_balance(camera.grab, camera.set, RANGES)

    # Level beats range: touching contrast must not leave the picture worse
    # levelled than brightness alone could manage.
    assert capture.meter(camera.grab()) >= capture.meter(brightness_only.grab()) - 2
    assert capture.spread(camera.grab()) > 0, "the ink was crushed away"


def test_contrast_stops_once_there_is_enough_range():
    """Enough separation is the goal, not the most the control can produce."""
    camera = FakeCamera(paper=200, ink=50)    # already a wide tonal range
    capture.auto_balance(camera.grab, camera.set, RANGES)
    assert camera.values["contrast"] < RANGES["contrast"]["max"]
    assert capture.meter(camera.grab()) == pytest.approx(
        capture.TARGET_LUMA, abs=8)


def test_auto_balance_stays_inside_the_reported_range():
    camera = FakeCamera(paper=10)
    narrow = {"brightness": {"min": 100, "max": 160, "default": 128}}
    capture.auto_balance(camera.grab, camera.set, narrow)
    assert 100 <= camera.values["brightness"] <= 160


def test_auto_balance_gives_up_quietly_when_no_frame_arrives():
    camera = FakeCamera()
    result = capture.auto_balance(lambda: None, camera.set, RANGES)
    assert result["applied"] == {}


def test_auto_balance_ignores_controls_the_camera_lacks():
    camera = FakeCamera()
    result = capture.auto_balance(
        camera.grab, camera.set, {"brightness": RANGES["brightness"]})
    assert "contrast" not in result["applied"]
    assert "brightness" in result["applied"]


def test_meter_reads_the_centre_not_the_dimmed_edges():
    """The preview has the dead zone burned in, so the edges are not the scene."""
    image = Image.new("L", (200, 200), 20)      # dark "dimmed" surround
    for y in range(50, 150):
        for x in range(50, 150):
            image.putpixel((x, y), 240)        # bright paper in the middle
    assert capture.meter(image) == pytest.approx(240, abs=2)


def test_spread_measures_tonal_range():
    flat = Image.new("L", (100, 100), 128)
    assert capture.spread(flat) == 0

    mixed = Image.new("L", (100, 100), 200)
    for y in range(50, 100):
        for x in range(100):
            mixed.putpixel((x, y), 50)
    assert capture.spread(mixed) > 100
