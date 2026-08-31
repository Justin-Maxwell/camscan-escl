"""The diagnostic and lifecycle endpoints, over real HTTP.

These exist because the daemon had no way to be asked what it was doing. It
was `active (running)` with a zombie ffmpeg behind it, `/preview/stream`
answered 200 and then sent nothing, and the only externally visible symptom
was a viewer reconnecting every 31 seconds.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from camscan_escl import preview as preview_mod
from camscan_escl import server
from camscan_escl.config import Config, PreviewConfig, ServerConfig


class DeadStream(preview_mod.PreviewStream):
    """A pipeline whose ffmpeg exits at once -- the failure, reproduced."""

    def _resolve_devices(self):
        self._resolved = {"camera": "/dev/fake0", "loopback": ""}
        return ("/dev/fake0", "")


@pytest.fixture
def daemon(request, tmp_path, monkeypatch):
    """A daemon whose preview is deliberately broken, unless asked otherwise.

    ADJUSTMENTS_PATH is redirected first, before anything can post. Both
    `/preview/settings` and `/preview/controls` call `save_adjustments(cfg)`
    with no path, which is the REAL `~/.config/camscan-escl/adjustments.json`
    -- so one POST from a test replaces a running daemon's saved rig with
    this file's defaults, and the loss only shows up at the next restart.
    Exactly the failure `test_generated_state_never_lands_in_the_real_config
    _directory` records for the ghost image, on the other file.
    """
    monkeypatch.setattr(server.config_mod, "ADJUSTMENTS_PATH",
                        tmp_path / "adjustments.json")
    argv = getattr(request, "param",
                   ["sh", "-c", "echo 'Not a video capture device.' >&2; exit 237"])
    cfg = Config(
        server=ServerConfig(port=0, bind="127.0.0.1"),
        preview=replace(PreviewConfig(), loopback_device=""),
        state_dir=tmp_path,
    )
    stream = DeadStream(cfg)
    # Patched on the instance's module so build_command stays untouched
    # elsewhere; the point is the lifecycle, not the ffmpeg arguments.
    stream._argv = argv
    original = preview_mod.build_command
    preview_mod.build_command = lambda *a, **k: argv
    httpd = server.serve(cfg, stream)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        preview_mod.build_command = original
        stream.stop()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, json.load(r)


def post_json(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_status_reports_a_stopped_preview_without_pretending(daemon):
    status, body = get_json(f"{daemon}/preview/status")
    assert status == 200
    assert body["running"] is False
    assert body["healthy"] is False
    assert body["summary"] == "Preview is not running."


def test_starting_a_broken_pipeline_answers_503_with_the_reason(daemon):
    code, body = post_json(f"{daemon}/preview/start")
    # Not 200. The old code would have called this a success.
    assert code == 503
    assert body["healthy"] is False
    assert body["exit_code"] == 237
    assert any("Not a video capture device" in line
               for line in body["stderr_tail"])


def test_the_stream_endpoint_refuses_with_an_explanation(daemon):
    post_json(f"{daemon}/preview/start")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{daemon}/preview/stream", timeout=30)
    assert excinfo.value.code == 503
    detail = excinfo.value.read().decode()
    # "preview not running" was true and useless; the reason is what a viewer
    # can act on.
    assert "237" in detail or "Not a video capture device" in detail


def test_the_frame_endpoint_explains_itself_too(daemon):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{daemon}/preview/frame", timeout=30)
    assert excinfo.value.code == 503
    assert excinfo.value.read().decode().strip()


@pytest.mark.parametrize(
    "daemon", [["sh", "-c", r"printf '\377\330x\377\331'; sleep 20"]],
    indirect=True)
def test_start_stop_round_trip_on_a_working_pipeline(daemon):
    code, body = post_json(f"{daemon}/preview/start")
    assert code == 200
    assert body["healthy"] is True
    assert body["frames_seen"] >= 1

    code, body = get_json(f"{daemon}/preview/status")
    assert body["running"] is True

    code, body = post_json(f"{daemon}/preview/stop")
    assert code == 200
    assert body["running"] is False
    # A requested stop is not a fault, so nothing stale is left showing.
    assert body["error"] is None

    code, body = post_json(f"{daemon}/preview/restart")
    assert code == 200
    assert body["healthy"] is True


def test_status_carries_the_device_inventory(daemon):
    _status, body = get_json(f"{daemon}/preview/status")
    # The inventory is what turns "no picture" into "the loopback took
    # /dev/video0 this boot", so it must reach the client.
    assert "inventory" in body and "devices" in body
    assert isinstance(body["inventory"], list)


def test_the_page_can_tell_when_its_overlay_has_gone_stale(daemon):
    """The overlay is server-rendered, so nothing in the page redraws itself.

    A settings change from the GUI moved the marks burned into the video while
    the page's own SVG stayed where it was, and the two disagreed with no way
    to tell which was current. The page polls this endpoint and reloads when
    it moves, so it has to move.
    """
    status, before = get_json(f"{daemon}/preview/geometry")
    assert status == 200
    assert [m[0] for m in before["marks"]] == ["A4", "A5", "Letter"]

    req = urllib.request.Request(
        f"{daemon}/preview/settings",
        data=json.dumps({"papers": [["A6", 105.0, 148.0]]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200

    _status, after = get_json(f"{daemon}/preview/geometry")
    assert [m[0] for m in after["marks"]] == ["A6"]
    assert after != before


def test_the_page_embeds_the_geometry_it_was_drawn_from(daemon):
    """Or the first poll sets its own baseline and loses any change made
    between the render and that request."""
    _status, geometry = get_json(f"{daemon}/preview/geometry")
    with urllib.request.urlopen(f"{daemon}/preview", timeout=30) as r:
        page = r.read().decode()
    assert json.dumps(geometry) in page
    assert "/preview/geometry" in page


def test_a_settings_post_never_writes_the_real_adjustments_file(daemon, tmp_path):
    """The suite overwrote a running daemon's saved rig, twice over.

    `_settings_post` and `_store_controls` both call `save_adjustments(cfg)`
    with no path, so any test that posts writes the user's live file with
    this module's Config() defaults. The daemon in memory is untouched, which
    is what makes it silent: the calibration is only lost at the next restart.
    """
    from camscan_escl import config as config_mod

    assert config_mod.ADJUSTMENTS_PATH.parent == tmp_path

    req = urllib.request.Request(
        f"{daemon}/preview/settings",
        data=json.dumps({"anchor": "top-left"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200

    written = json.loads(config_mod.ADJUSTMENTS_PATH.read_text())
    assert written["anchor"] == "top-left"
