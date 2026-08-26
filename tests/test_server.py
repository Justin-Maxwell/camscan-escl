"""End-to-end over real HTTP, with a fake capture command in place of a camera.

This exercises the round trip a client actually makes: POST ScanJobs, follow
the Location header, GET NextDocument, then GET it again and get a 404.
"""

import io
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from PIL import Image

from camscan_escl import escl, server
from camscan_escl.config import CaptureConfig, Config, FocusConfig, RigConfig, ServerConfig

FAKE_CAMERA = """
import sys
from PIL import Image
Image.new("RGB", (2304, 1536), (30, 90, 180)).save(sys.argv[1], "JPEG")
"""

FAILING_CAMERA = "import sys; sys.exit(1)"


def make_config(tmp_path, body=FAKE_CAMERA):
    script = tmp_path / "fake_camera.py"
    script.write_text(body)
    return Config(
        server=ServerConfig(port=0, bind="127.0.0.1"),
        capture=CaptureConfig(
            command=f"{sys.executable} {script} %f",
            timeout_s=30,
            focus=FocusConfig(disable_autofocus=False),
        ),
        rig=RigConfig(coverage_mm=(210.0, 297.0)),
    )


@pytest.fixture
def daemon(tmp_path, request):
    body = getattr(request, "param", FAKE_CAMERA)
    httpd = server.serve(make_config(tmp_path, body))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}/eSCL"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def post(url, body=b"", method="POST"):
    req = urllib.request.Request(url, data=body, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def test_capabilities_and_status(daemon):
    status, body, headers = get(f"{daemon}/ScannerCapabilities")
    assert status == 200
    assert headers["Content-Type"].startswith("text/xml")
    assert ET.fromstring(body).tag.endswith("ScannerCapabilities")

    status, body, _ = get(f"{daemon}/ScannerStatus")
    assert status == 200
    assert ET.fromstring(body).find("pwg:State", escl.NS).text == "Idle"


def test_full_scan_round_trip(daemon):
    status, _, headers = post(f"{daemon}/ScanJobs", _settings_body())
    assert status == 201
    location = headers["Location"]
    assert location.startswith("http://") and "/eSCL/ScanJobs/" in location

    status, jpeg, headers = get(f"{location}/NextDocument")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert Image.open(io.BytesIO(jpeg)).size == (1240, 1754)  # A4 at 150 dpi

    # Acceptance criterion 5: the second call ends the job with a 404.
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{location}/NextDocument")
    assert exc.value.code == 404


def test_two_scans_in_succession(daemon):
    for _ in range(2):
        _, _, headers = post(f"{daemon}/ScanJobs", _settings_body())
        status, jpeg, _ = get(f"{headers['Location']}/NextDocument")
        assert status == 200
        assert Image.open(io.BytesIO(jpeg)).size == (1240, 1754)


def test_second_concurrent_job_is_refused(daemon):
    post(f"{daemon}/ScanJobs", _settings_body())
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(f"{daemon}/ScanJobs", _settings_body())
    assert exc.value.code == 503


def test_delete_frees_the_slot(daemon):
    _, _, headers = post(f"{daemon}/ScanJobs", _settings_body())
    status, _, _ = post(headers["Location"], method="DELETE")
    assert status == 200
    status, _, _ = post(f"{daemon}/ScanJobs", _settings_body())
    assert status == 201


@pytest.mark.parametrize("daemon", [FAILING_CAMERA], indirect=True)
def test_capture_failure_leaves_the_daemon_idle_and_usable(daemon):
    _, _, headers = post(f"{daemon}/ScanJobs", _settings_body())
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{headers['Location']}/NextDocument")
    assert exc.value.code == 500

    # Acceptance criterion 7: still Idle, still selectable, no restart needed.
    _, body, _ = get(f"{daemon}/ScannerStatus")
    assert ET.fromstring(body).find("pwg:State", escl.NS).text == "Idle"
    status, _, _ = post(f"{daemon}/ScanJobs", _settings_body())
    assert status == 201


def test_unknown_path_is_404(daemon):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{daemon}/Nonsense")
    assert exc.value.code == 404


def _settings_body(width=2480, height=3508, dpi=150):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{escl.PWG}" xmlns:scan="{escl.SCAN}">
  <pwg:ScanRegions><pwg:ScanRegion>
    <pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>
    <pwg:Width>{width}</pwg:Width><pwg:Height>{height}</pwg:Height>
  </pwg:ScanRegion></pwg:ScanRegions>
  <scan:ColorMode>RGB24</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
</scan:ScanSettings>""".encode()
