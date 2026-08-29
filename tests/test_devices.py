"""Finding V4L2 devices by what they are rather than by what number they got.

The bug these guard: across the boot of 2026-08-29 the numbers moved.
v4l2loopback is autoloaded with no `video_nr=` and won the race for
/dev/video0, so a config pinned to the previous boot pointed the camera at the
loopback and the loopback at the C920's metadata node. ffmpeg exited 237 and
the daemon reported that it was streaming for half an hour.
"""

from __future__ import annotations

import pytest

from camscan_escl import devices
from camscan_escl.devices import (
    CAP_META_CAPTURE,
    CAP_VIDEO_CAPTURE,
    CAP_VIDEO_OUTPUT,
    DeviceError,
    V4L2Device,
)


def make(path, card, caps, driver="uvcvideo"):
    return V4L2Device(path=path, card=card, driver=driver,
                      bus_info="test", caps=caps)


# The exact layout measured on the failing boot.
RIG = [
    make("/dev/video0", "OBS Virtual Camera", CAP_VIDEO_OUTPUT, "v4l2 loopback"),
    make("/dev/video1", "HD Pro Webcam C920", CAP_VIDEO_CAPTURE),
    make("/dev/video2", "HD Pro Webcam C920", CAP_META_CAPTURE),
]


def test_card_name_skips_the_metadata_node_of_the_same_camera():
    # The C920 owns two nodes with an IDENTICAL card string. Matching on the
    # name alone is a coin toss, and the metadata node fails exactly the way
    # the loopback did.
    assert devices.resolve("card:HD Pro Webcam C920", "capture", RIG) == "/dev/video1"


def test_card_name_finds_the_loopback_for_output():
    assert devices.resolve("card:OBS Virtual Camera", "output", RIG) == "/dev/video0"


def test_card_match_is_case_insensitive_and_partial():
    assert devices.resolve("card:c920", "capture", RIG) == "/dev/video1"


def test_auto_picks_the_real_camera_not_the_loopback():
    # A loopback with a producer attached advertises VIDEO_CAPTURE, so "auto"
    # could otherwise point the capture at this daemon's own output.
    busy_loopback = [
        make("/dev/video0", "OBS Virtual Camera", CAP_VIDEO_CAPTURE, "v4l2 loopback"),
        make("/dev/video1", "HD Pro Webcam C920", CAP_VIDEO_CAPTURE),
    ]
    assert devices.resolve("auto", "capture", busy_loopback) == "/dev/video1"


def test_auto_output_finds_the_loopback():
    assert devices.resolve("auto", "output", RIG) == "/dev/video0"


def test_a_busy_loopback_still_counts_as_an_output():
    # exclusive_caps=1 flips what v4l2loopback advertises once ffmpeg attaches.
    # Verified live: the same node read (output) idle and (capture) in use.
    # Testing the capability alone called a working loopback unusable.
    busy = [make("/dev/video0", "OBS Virtual Camera", CAP_VIDEO_CAPTURE,
                 "v4l2 loopback")]
    assert devices.resolve("card:OBS Virtual Camera", "output", busy) == "/dev/video0"
    assert busy[0].kind == "loopback"


def test_a_literal_path_pointed_at_the_wrong_device_is_refused_with_a_reason():
    # THE bug, as a test. The old config said /dev/video0 was the camera.
    with pytest.raises(DeviceError) as excinfo:
        devices.resolve("/dev/video0", "capture", RIG)
    message = str(excinfo.value)
    assert "OBS Virtual Camera" in message
    assert "cannot capture" in message
    # And it must say what to do instead, not merely that something is wrong.
    assert "card:" in message


def test_a_literal_path_to_the_metadata_node_is_refused_for_output():
    # The other half of the same misconfiguration: loopback_device was
    # /dev/video2, which is the C920's metadata node.
    with pytest.raises(DeviceError) as excinfo:
        devices.resolve("/dev/video2", "output", RIG)
    assert "metadata" in str(excinfo.value)


def test_an_unknown_card_lists_what_is_actually_present():
    with pytest.raises(DeviceError) as excinfo:
        devices.resolve("card:Brother DCP", "capture", RIG)
    message = str(excinfo.value)
    for path in ("/dev/video0", "/dev/video1", "/dev/video2"):
        assert path in message


def test_empty_spec_is_an_error_not_a_silent_default():
    with pytest.raises(DeviceError):
        devices.resolve("", "capture", RIG)


def test_describe_never_raises_and_explains_a_failure():
    good = devices.describe("card:HD Pro Webcam C920", "capture", RIG)
    assert good["ok"] and good["resolved"] == "/dev/video1"

    bad = devices.describe("/dev/video0", "capture", RIG)
    assert not bad["ok"]
    assert bad["resolved"] is None
    assert "cannot capture" in bad["detail"]


def test_node_ordering_is_numeric_not_lexical():
    assert devices._node_key("/dev/video10") > devices._node_key("/dev/video9")


def test_device_caps_is_preferred_over_the_union_capabilities():
    """A node must be judged on ITS capability, not its siblings'.

    `capabilities` covers everything the physical device owns across all its
    nodes, so the C920's metadata node reports VIDEO_CAPTURE there because a
    sibling can capture. Believing that field is how the metadata node looks
    like a camera.
    """
    import struct

    caps_union = CAP_VIDEO_CAPTURE | CAP_META_CAPTURE | devices.CAP_DEVICE_CAPS
    packed = struct.pack(
        devices._CAP_FMT, b"uvcvideo", b"HD Pro Webcam C920", b"usb",
        0, caps_union, CAP_META_CAPTURE,
    )
    driver, card, bus, _v, caps, device_caps = struct.unpack(devices._CAP_FMT, packed)
    effective = device_caps if caps & devices.CAP_DEVICE_CAPS else caps
    node = make("/dev/video2", devices._text(card), effective)
    assert not node.can_capture
    assert node.kind == "metadata"
