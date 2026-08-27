# camscan-escl

A minimal eSCL (AirScan) daemon that presents a USB webcam to scanning
front-ends as if it were a flatbed scanner. Target consumer: NAPS2 via its
**ESCL Driver → Manual IP** option; development harness: `sane-airscan`.

Open work is tracked in [docs/ISSUES.md](docs/ISSUES.md) — there is no git
remote, so that file is the issue tracker.

The full design, and the reasoning for it, is in
[docs/camscan-escl-spec.md](docs/camscan-escl-spec.md). Read §5 — the units
contract — before touching the imaging path.

## Install

```bash
uv pip install -e '.[dev]'
```

Requires Python 3.11+, Pillow, and one of `fswebcam` / `ffmpeg` plus
`v4l2-ctl` on the host.

## Run

```bash
camscan-escl -v
```

Config is TOML at `~/.config/camscan-escl/config.toml`; every key has a
built-in default, so it runs without one. Start from
[config.example.toml](config.example.toml).

```bash
cp config.example.toml ~/.config/camscan-escl/config.toml
```

As a user service — which is how it should run, since anything started from a
terminal dies with that terminal and the front-end then reports only
`Connection refused`:

```bash
cp systemd/camscan-escl.service ~/.config/systemd/user/
```

Edit `ExecStart` if you run from a project venv, then:

```bash
systemctl --user enable --now camscan-escl.service
```

The unit runs under `ProtectSystem=strict` and `PrivateTmp`; capture still
works, because the capture command writes only to its own temp directory.
To survive a full logout, linger has to be on — this needs root:

```bash
sudo loginctl enable-linger "$USER"
```

## Discovery

The daemon advertises itself over DNS-SD as `_uscan._tcp`, so NAPS2's device
search finds it without Manual IP. That needs the optional dependency:

```bash
uv pip install -e '.[discovery]'
```

Without it the daemon runs normally, logs one warning, and Manual IP still
works. Turn the advert off with `--no-discovery` or `discovery.enable = false`.

Advertisement is in-process, not a static Avahi file, so it disappears when
the daemon does — a front-end cannot offer a device that is not listening. If
you want the opposite trade-off (advert survives restarts, may point at
nothing):

```bash
camscan-escl --print-avahi-service | sudo tee /etc/avahi/services/camscan-escl.service
```

### NAPS2's device search does not find this daemon. Use Manual IP.

`sane-airscan` finds it fine (`scanimage -L`, no URL needed). NAPS2 does not,
and the fault is not in what we advertise. Established by packet capture:

- NAPS2 queries `_uscan._tcp` every two seconds over IPv4 **and** IPv6, and
  we answer within 50 ms with PTR, TXT, SRV and an address record.
- Every condition its own `EsclServiceLocator` checks is satisfied:
  `Labels[1] == "_uscan"`, a lowercase `uuid` TXT key, an A record, an SRV
  target and port that resolve. Our responses carry the RFC 6762-mandated
  IP TTL of 255; NAPS2's own queries carry TTL 1.
- **Publishing identical records through Avahi's responder instead of ours
  changes nothing.** Avahi answers with six records in the answers section,
  A and AAAA both, clean cache, no ghost instances -- and across 2572
  captured frames NAPS2 never opens a socket to port 8090.

So the daemon is discoverable and NAPS2's mDNS client does not act on it.
Manual IP works and is the supported route. The packet captures behind this
are kept locally and not published — they record a home LAN, neighbouring
devices included. Ask if you want them for an upstream report.

**`server.bind` gates who can use it, and it gates discovery too.** At the
default `127.0.0.1` the advert carries `127.0.0.1`. `sane-airscan` tolerates
that on the same host; **NAPS2's device search does not — it reports "No
devices found"**, because it browses on real interfaces and will not use a
loopback record. So discovery in practice needs:

```toml
[server]
bind = "0.0.0.0"
```

Understand what that opens: **eSCL has no authentication**, so anyone who can
reach the port can trigger a capture from the webcam. On a laptop that leaves
trusted networks, this is the wrong default — which is why the shipped default
stays on loopback.

Reaching it from *another* host also needs firewalld opened, which the local
case does not:

```bash
sudo firewall-cmd --add-port=8090/tcp --add-service=mdns --permanent
```

Check what is being advertised with:

```bash
avahi-browse -rt _uscan._tcp
```

## Preview and crop marks

```
http://<host>:8090/preview
```

A live 1280×720 stream with dashed rectangles showing where A4, A5 and Letter
will land, given your `rig.coverage_mm`. This is how you calibrate that
setting: put a real sheet under the camera, line it up with its mark, scan,
and see whether the scan matches. It is also how you notice that a paper size
does not fit at all — with the default A4 coverage, the Letter mark comes out
1316 px wide in a 1280 px frame.

### Crop marks inside Kamoso, or any webcam app

A browser tab is a poor place to line up paper. Point the daemon at a
[v4l2loopback](https://github.com/umlaeute/v4l2loopback) device and it
publishes the marked-up video as a virtual webcam, so Kamoso, Cheese or a
video call shows the crop marks burned in — live, at full frame rate.

```bash
sudo dnf install akmod-v4l2loopback
sudo modprobe v4l2loopback video_nr=9 card_label="camscan preview" exclusive_caps=1
```

`exclusive_caps=1` matters: without it some apps do not recognise the device
as a camera. To have it survive a reboot:

```bash
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf
printf 'options v4l2loopback video_nr=9 card_label="camscan preview" exclusive_caps=1\n' | sudo tee /etc/modprobe.d/v4l2loopback.conf
```

Then point the daemon at it and restart:

```toml
[preview]
enable = true
loopback_device = "/dev/video9"
```

Kamoso will list "camscan preview" alongside the real camera. **Open that
one, not the C920** — the C920 itself is held by the daemon and will report
`Device or resource busy`. The marks are drawn by ffmpeg as part of the same
pipeline that reads the camera, so this costs one camera read, not two;
reading it twice is impossible anyway.

During a scan the loopback goes quiet for a few seconds while the camera is
handed over, then resumes. The device does not disappear, so the app does not
need reopening.

**The daemon holds the camera while the preview runs.** V4L2 streaming access
is exclusive — a second process gets `Device or resource busy` — so nothing
else can use the webcam meanwhile. A scan releases the device, captures, and
resumes the stream automatically; a scan costs about 11 s with the preview on
against about 5 s without, the difference being the stop and restart. To free
the camera for Kamoso or similar, set `preview.enable = false` (or run with
`--no-preview`) and restart.

**The preview does not show everything the scan captures.** The still is
2304×1536 (3:2) and on the C920 that is the only 3:2 mode; every streamable
mode is 16:9 or 4:3. Measured here: 16:9 modes are the still's full width with
a centred vertical crop, so the preview shows the middle 1296 of 1536 rows and
**the scanner sees 120 rows more at the top and bottom**. The page says so, and
marks that leave the frame are labelled. 4:3 modes are zoomed to a narrower
horizontal field and cannot be mapped by cropping at all, so the daemon
refuses to start with a 4:3 preview rather than draw crop marks that lie.

## Calibrate before trusting the output

Two values decide whether a scan comes out at the right scale:

- **`rig.coverage_mm`** — the physical area the frame actually covers at rig
  height. Put a ruler under the camera and measure it. This is what maps an
  eSCL scan region onto the sensor.
- **`capture.focus`** — leave it on autofocus, which is the default. The
  camera focuses itself well at rig distances, and the fixed value this
  project used to ship was measurably softer than what autofocus picks. Pin
  it only if you actually observe hunting between pages. If you need a
  number:

```bash
camscan-escl --focus-sweep
```

  That scores real captures, sharpest first. Run it with the rig in its final
  position and a page underneath — the answer is only true for the distance
  it was measured at — and stop the daemon first, or it will be holding the
  camera. Treat the scores sceptically: variance of the Laplacian rewards
  noise as much as detail, so look at the frames rather than trusting the
  ranking, and compare against what autofocus settles on.

- **`capture.exposure`** — off by default, and worth turning on. Left on
  auto, the sensor re-decides every capture: a scan on this rig came back with
  89% of its pixels at 250+ luma from a scene that metered correctly minutes
  later. Sweep for a value that holds mid-grey, then pin it:

```bash
for t in 60 90 120 160 220; do v4l2-ctl -d /dev/video0 -c auto_exposure=1 -c exposure_time_absolute=$t; ffmpeg -loglevel error -f v4l2 -pix_fmt yuyv422 -video_size 2304x1536 -i /dev/video0 -frames:v 3 -update 1 -y /tmp/e$t.png; done
```

  Pick the one where paper is bright but not clipped, put it in
  `time_absolute`, and set `lock = true`. Restore auto with
  `v4l2-ctl -d /dev/video0 -c auto_exposure=3 -c white_balance_automatic=1`.

`scanner.resolution_dpi` should be one the geometry can deliver
(≈ `3454 / distance_cm`). Declaring more than that would mean upscaling to
meet the dimension contract, which fabricates detail.

## Layout

| Path | What |
|---|---|
| `src/camscan_escl/escl.py` | XML generation and `ScanSettings` parsing |
| `src/camscan_escl/imaging.py` | crop / pad / scale — the units contract |
| `src/camscan_escl/capture.py` | subprocess capture, focus, size verification |
| `src/camscan_escl/jobs.py` | one-at-a-time job records, 5-minute reaping |
| `src/camscan_escl/server.py` | the `/eSCL` HTTP surface |
| `src/camscan_escl/discovery.py` | DNS-SD advertisement, Avahi file generation |
| `src/camscan_escl/preview.py` | live stream, camera handover, crop-mark geometry |
| `src/camscan_escl/previewpage.py` | the positioning page and its SVG overlay |

## Testing

```bash
python3 -m pytest -q
```

The suite runs without a camera: `tests/test_server.py` substitutes a script
that writes a synthetic 2304×1536 frame, and drives the real HTTP round trip
(POST ScanJobs → NextDocument → 404 on the second call). The units contract
is asserted for A4 and Letter across resolutions.

Then work through the spec's staged plan (§11) against real hardware — curl
first, `scanimage` second, NAPS2 last.

```bash
curl -s http://127.0.0.1:8090/eSCL/ScannerCapabilities | xmllint --format -
scanimage -d 'airscan:escl:camscan:http://127.0.0.1:8090/eSCL' --format=jpeg > /tmp/t.jpg
```

## Spec errata

Corrections to `docs/camscan-escl-spec.md`, found by running §11 against the
real camera and a real front-end on this host:

1. **§11's `scanimage -d 'escl:…'` device name is wrong on Fedora 44.** There
   is no `libsane-escl.so`; the backend ships only as `libsane-airscan.so`,
   so the backend name has to lead: `airscan:escl:camscan:<url>`. The short
   form fails with a bare `Invalid argument` and no further explanation.
2. **§8's `fswebcam` default does not reach 2304×1536 on this host.** Measured:
   it negotiates MJPG and returns 1920×1080, silently, which is the risk §13
   lists third. The ffmpeg form pins `yuyv422` and does get the still mode, so
   it is now the shipped default; the fswebcam line is kept commented in
   `config.example.toml`.
3. **§6's `MaxWidth` of 2480 breaks US Letter.** Letter is 2550 units wide,
   so a client clamps the request to the declared maximum and returns a
   1240px-wide page where the units contract says 1275 — silently, which is
   exactly the failure mode §5 warns about. The daemon declares the union of
   both paper sizes (2550 × 3508) instead. `tests/test_escl.py` guards it.

4. **§6's capabilities skeleton omits `scan:UUID`, and NAPS2 requires it.**
   `ManualIpForm` creates a device only when `caps.Uuid` *and*
   `caps.MakeAndModel` are both non-null, and on a null UUID it reports
   nothing at all — the dialog sits there looking like a network failure. A
   packet capture showed a clean `200 OK` and the whole document read, which
   is how it was found. The same UUID goes in the advert's TXT record.

## Known-unverified

**The capabilities XML has not been validated against the Mopria eSCL Scan
Technical Specification.** It reproduces the skeleton in spec §6, which that
document itself flags as written from recollection. Validating it — against
the spec and against AirSane's generated output — is the first task, per
§13. Everything downstream of client acceptance is untestable until then.

Discovery (§10) is no longer deferred: `scanimage -L` finds the device with
no URL given, and a scan through the discovered name returns a correctly
sized page. NAPS2's device search does not act on the advert -- see
"Discovery" above for the evidence, and use Manual IP with NAPS2.
