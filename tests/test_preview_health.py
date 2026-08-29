"""A preview that has died must say so.

The failure being guarded here ran for half an hour undetected on 2026-08-29.
ffmpeg exited 237 with "Not a video capture device" a few milliseconds after
`Popen` returned, and:

  - `_start_locked` never checked, so `running` stayed True;
  - stderr went to DEVNULL, so the message was destroyed;
  - `_read_frames` hit EOF and broke out but left `running` True;
  - so `/preview/stream` passed its guard, sent 200, and blocked;
  - so the viewer timed out at 30s and reconnected, forever, saying nothing.

Every assertion below is one of those links.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from camscan_escl import preview
from camscan_escl.config import Config


@pytest.fixture
def cfg():
    # A loopback is configured so the resolution path is exercised; the fake
    # resolver below decides what it becomes.
    return replace(Config(), preview=replace(
        Config().preview, loopback_device="card:Test Loopback"))


@pytest.fixture
def no_hardware(monkeypatch):
    """Resolve device specs without touching the machine's device tree.

    Patched in both modules that reach for it: `preview` starts the pipeline,
    and `config.device_report` resolves again for the diagnostic panel.
    """
    def resolve(spec, role="capture", devices=None):
        return f"/dev/fake-{role}"

    monkeypatch.setattr(preview.devices, "resolve", resolve)
    monkeypatch.setattr(preview.devices, "enumerate_devices", list)


def run_with_command(monkeypatch, cfg, argv, wait=5.0):
    monkeypatch.setattr(preview, "build_command", lambda *a, **k: argv)
    stream = preview.PreviewStream(cfg)
    ok = stream.start(wait=wait)
    return stream, ok


def test_a_pipeline_that_dies_is_not_reported_as_running(
        monkeypatch, cfg, no_hardware):
    # `false` is the shortest possible stand-in for ffmpeg exiting 237.
    stream, ok = run_with_command(monkeypatch, cfg, ["false"])
    try:
        assert ok is False, "start() must not claim success for a dead pipeline"
        # The precise lie that caused the outage.
        assert stream.running is False
        assert stream.healthy is False
        assert stream.status()["exit_code"] == 1
    finally:
        stream.stop()


def test_the_reason_survives_instead_of_going_to_devnull(
        monkeypatch, cfg, no_hardware):
    stream, _ok = run_with_command(
        monkeypatch, cfg,
        ["sh", "-c", "echo 'Not a video capture device.' >&2; exit 237"])
    try:
        status = stream.status()
        assert status["exit_code"] == 237
        assert any("Not a video capture device" in line
                   for line in status["stderr_tail"]), status["stderr_tail"]
        # And it reaches the one-line summary a human actually reads.
        assert "237" in status["summary"]
    finally:
        stream.stop()


def test_frames_returns_promptly_when_the_pipeline_dies(
        monkeypatch, cfg, no_hardware):
    """The generator must end, not block for its 60s stall timeout.

    This is what made /preview/stream answer 200 and then hold the connection
    open with nothing to send, which the viewer could only interpret as a
    30-second socket timeout.
    """
    stream, _ok = run_with_command(monkeypatch, cfg, ["false"])
    try:
        started = time.monotonic()
        assert list(stream.frames(stall_timeout=30.0)) == []
        assert time.monotonic() - started < 5.0
    finally:
        stream.stop()


def test_an_unresolvable_camera_is_reported_rather_than_launched(
        monkeypatch, cfg):
    def refuse(spec, role="capture", devices=None):
        raise preview.devices.DeviceError(
            f"{spec} is 'OBS Virtual Camera' (loopback), which cannot capture")

    monkeypatch.setattr(preview.devices, "resolve", refuse)
    monkeypatch.setattr(preview.devices, "enumerate_devices", list)
    launched = []
    monkeypatch.setattr(preview.subprocess, "Popen",
                        lambda *a, **k: launched.append(a))

    stream = preview.PreviewStream(cfg)
    assert stream.start(wait=2.0) is False
    assert not launched, "must not run ffmpeg against a device known to be wrong"
    assert "cannot capture" in stream.status()["summary"]


def test_a_missing_loopback_degrades_rather_than_stops(monkeypatch, cfg):
    """The loopback is a convenience; the web preview and scans do not need it."""
    def resolve(spec, role="capture", devices=None):
        if role == "output":
            raise preview.devices.DeviceError("no loopback here")
        return "/dev/fake-capture"

    monkeypatch.setattr(preview.devices, "resolve", resolve)
    monkeypatch.setattr(preview.devices, "enumerate_devices", list)
    # Emits one JPEG then holds the pipe open, so the stream counts as healthy.
    stream, ok = run_with_command(
        monkeypatch, cfg,
        ["sh", "-c", r"printf '\377\330junk\377\331'; sleep 5"])
    try:
        assert ok is True
        assert stream.healthy is True
    finally:
        stream.stop()


def test_a_healthy_pipeline_says_so(monkeypatch, cfg, no_hardware):
    stream, ok = run_with_command(
        monkeypatch, cfg,
        ["sh", "-c", r"printf '\377\330junk\377\331'; sleep 5"])
    try:
        assert ok is True
        status = stream.status()
        assert status["healthy"] is True
        assert status["frames_seen"] >= 1
        assert status["error"] is None
        assert "Streaming from /dev/fake-capture" in status["summary"]
    finally:
        stream.stop()


def test_a_deliberate_stop_is_not_recorded_as_a_fault(
        monkeypatch, cfg, no_hardware):
    """Stopping is not crashing, and the GUI must not show a stale error."""
    stream, _ok = run_with_command(
        monkeypatch, cfg,
        ["sh", "-c", r"printf '\377\330junk\377\331'; sleep 30"])
    stream.stop()
    status = stream.status()
    assert status["running"] is False
    assert status["error"] is None
    assert status["summary"] == "Preview is not running."


def test_a_zoomed_camera_is_reported_even_though_it_looks_healthy(
        monkeypatch, cfg, no_hardware):
    """Measured: zoom changes the STREAM's field of view and not the still.

    So every crop mark is placed against a relationship that no longer holds,
    while the picture itself looks perfectly fine and the scan is unaffected.
    Nothing else in the pipeline can notice, and the daemon never sets these
    controls -- but V4L2 state lives on the device and survives whatever last
    touched it.
    """
    from camscan_escl import capture as capture_mod

    monkeypatch.setattr(capture_mod, "geometry_controls", lambda *a, **k: {
        "pan_absolute": {"value": 0, "default": 0, "at_default": True},
        "zoom_absolute": {"value": 200, "default": 100, "at_default": False},
    })
    stream, ok = run_with_command(
        monkeypatch, cfg,
        ["sh", "-c", r"printf '\377\330junk\377\331'; sleep 5"])
    try:
        assert ok is True
        status = stream.status()
        assert status["healthy"] is True, "it really is streaming"
        assert "zoom_absolute" in status["summary"]
        assert "misplaced" in status["summary"]
    finally:
        stream.stop()


def test_status_is_json_safe(monkeypatch, cfg, no_hardware):
    """It is served straight over HTTP, so it must serialise."""
    import json

    stream, _ok = run_with_command(monkeypatch, cfg, ["false"])
    try:
        json.dumps(stream.status())
    finally:
        stream.stop()
