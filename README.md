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

### The settings window

```bash
camscan-escl-gui
```

A live view with the controls beside it: frame coverage in mm, nudge buttons,
and checkboxes for which paper sizes to draw. There is no Apply button:
every control applies itself, the daemon rebuilds its filter chain, the
picture updates, and the setting persists to
`~/.config/camscan-escl/adjustments.json`. That file is written by the GUI
and is separate from your `config.toml` on purpose: generated output must not
overwrite a file whose comments carry the reasoning. Delete it to fall back.

This is how you calibrate `rig.coverage_mm`, the measurement every scan's
scale depends on. Put a real sheet under the camera, adjust until its mark
sits on the sheet's edges, and it is measured rather than guessed. The ±%
buttons change the width; the height follows, because it is fixed by the
shape of the frame — a coverage of a different shape means every scan comes
out stretched, which is not a choice worth offering.

**The published frame is the scannable area, not the camera's streaming mode.**
853×1280 here against a 720×1280 stream. The still reaches further than any
streaming mode can — a 16:9 stream of a 3:2 still is the full width with a
centred crop — so a frame the size of the stream can only ever show part of
what a scan captures.

That gives three zones, and they nest:

| zone | what it is | what fills it |
|---|---|---|
| stream | what the camera sends live, 720×1280 | live video, never resampled |
| scannable | the whole still, 853×1280 — the canvas | the last scan, faintly |
| extension | beyond the camera entirely | padding, only when a sheet overflows |

The scannable area is a fixed property of the camera, so **the border never
changes size when you change paper sizes**. Only a sheet larger than the
camera can capture at all moves anything, and then only on the side it
overflows.

**The anchor moves the marks, never the picture,** and it anchors against the
whole coverage — which is the scannable area, all of which is now on screen.
An edge-anchored scan therefore uses the whole sensor.

**An edge anchor also sets the size of the scannable area.** The live picture
is the middle 1296 of the sensor's 1536 rows, so taking the whole still as the
scannable area puts its edge — the line the anchored mark is drawn flush
against — 120 rows outside the picture, and you cannot watch a sheet register
against a line you cannot see. So the strip on the anchored edge is dropped
and the two edges become one line. It costs 8% of the sensor, works at any
camera height, and is not a setting: an anchor names an edge, and there is no
rig where it names one and the strip outside it should be kept. A centred
anchor registers against nothing and keeps both strips. Set the anchor before
measuring `rig.coverage_mm` — it changes the area that measurement describes.

**When a paper does not fit**, the picture shrinks and a border appears —
but only on the sides the marks actually run off. The anchored edge never
overflows, so it never gets one: the video stays flush against exactly the
edge you are lining paper up against, and the border is opposite, where the
oversized sheet is spilling. Its width is the size of the spill, so the
overflowing edge is drawn on it and can be seen.

**The border carries the last scan, faintly.** The border is not empty space —
it is space the *scanner* reaches and the live view cannot. After every scan
the captured frame is washed back towards the padding and composited across
the whole canvas, so the border says what is actually out there instead of
being a blank margin.

It fills that region exactly, always. The camera has not moved, so the still
maps onto the scannable area precisely — which means the ghost survives every
settings change and is never conditional on a paper size overflowing. It is a
still and does not move with the page; that is why it is faint rather than
shown as picture.

The dead-zone dimming is clipped to the live picture and deliberately kept
*off* the border. Dimming means "the camera sees this but no scan can reach
it"; the border is the opposite, and greying it would wash out the very thing
it exists to carry.

Two files are kept: the captured still, unrotated, and the overlay composited
from it. A settings change moves where the still *belongs*, but it does not
make the still wrong — it is a photograph of the desk. So the overlay is
**redrawn** from the stored still, not discarded: tick a paper size, drag the
coverage or turn the camera and the border follows, with no rescan. The still
is stored unrotated precisely so `rotate_deg` can change without invalidating
it.

The overlay is removed only when there is no border left to fill, so a stale
one cannot be picked up on the next pipeline restart. Turn the whole thing off
with `scan_ghost = false`; `scan_ghost_opacity` sets how much shows through.

`preview.max_pad` (default `0.35`) caps the total border as a fraction of the
frame, so a wildly oversized paper cannot shrink the video to a postage stamp.
Past the cap the overflow clips instead, and the sidebar still says so in
millimetres — what the sheet needs against what the camera can see. Set
`fit_marks = false` for no border at all.

**Sizes that share an edge** are drawn at decreasing thicknesses, largest
outermost. Anchoring to a corner makes coincident edges the normal case, and
equal outlines stack so that only the last colour drawn can be seen.

**Landscape** lays the marks across the frame. Nothing is rotated: eSCL has
no orientation field, a landscape scan simply *is* a region wider than it is
tall, and NAPS2 can define one as a custom page size. Tick it and ask the
client for the landscape size — if the two disagree you get a correctly
sized image of the wrong area.

**Picture** gives brightness and contrast, as V4L2 controls on the camera
rather than ffmpeg filters — so they reach the scan and not just the preview.
They apply live, with no pipeline restart. **Auto** meters the picture and
picks both: brightness by bisection to put paper just below clipping, then a
bounded scan for the contrast giving the most tonal range without blowing the
highlights, then brightness again because contrast moved the level. If the
contrast pass leaves the level worse than brightness alone managed, it is
reverted — on a scene too dark to reach the target, more contrast buys
separation by pushing the whole picture further down.

**The Daemon panel** at the bottom says whether the preview is actually working,
and gives you Start, Stop and Restart. The dot is green only when frames are
genuinely arriving — not merely when a process was started, which is a
distinction that cost a session. Open **Details** for what each configured
device resolved to, every V4L2 node on the machine, and the tail of ffmpeg's
stderr. Restart re-resolves the device numbers, so it is what to press after
replugging the camera.

If the daemon is not answering at all, Start falls back to
`systemctl --user start camscan-escl.service` — but only when the GUI is
pointed at this machine, since there is nothing to ask on a remote host.

It speaks to the daemon over HTTP, so it never touches the camera and runs
fine from another machine with `--url http://<host>:8090`.

### When the preview is blank

```bash
camscan-escl --diagnose
```

Prints every V4L2 node with its card name and kind, what each configured
device resolves to, and what the running daemon says about itself. The same
information is served as JSON at `/preview/status`, and the lifecycle verbs
are `POST /preview/start`, `/preview/stop` and `/preview/restart`.

**Device numbers are not stable across boots**, which is the failure this was
built for. `v4l2loopback` is autoloaded on this host with no `video_nr=`, so
it races USB enumeration for the first free node; on 2026-08-29 it won and
took `/dev/video0`. A config pinned to the previous boot then had the daemon
reading from its own loopback and writing to the camera's *metadata* node.
ffmpeg exited 237 with "Not a video capture device", and because its stderr
went to `/dev/null` and nothing checked that the child had survived, the unit
stayed `active (running)` and the journal kept saying `preview streaming
1280x720`. The only visible symptom was a viewer reconnecting every 31
seconds — a 30-second socket timeout plus a 1-second retry.

So name devices by what they are:

```toml
[capture]
device = "card:HD Pro Webcam C920"

[preview]
loopback_device = "card:OBS Virtual Camera"
```

`auto` also works and picks the first node that can do the job, skipping
loopbacks when choosing a camera. A literal `/dev/videoN` is still accepted,
but it is now *verified* rather than trusted: point it at the wrong kind of
device and the daemon says so in a sentence instead of failing silently.

**It needs PyGObject**, which ships with a Fedora desktop but is not a
dependency of this package — the daemon must stay able to run headless
without a GUI stack, so it is not pulled in. A venv therefore cannot see it
unless it was built to:

```bash
uv venv --system-site-packages
```

An existing venv can be converted by setting
`include-system-site-packages = true` in its `pyvenv.cfg`. Then reinstall, so
the entry point is generated, and put it on PATH:

```bash
uv pip install -e .
```

```bash
ln -s "$PWD/.venv/bin/camscan-escl-gui" ~/.local/bin/camscan-escl-gui
```

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
loopback_device = "card:camscan preview"
```

Named by `card_label` rather than by number. The `video_nr=9` above pins it
for a module *you* load, but a distribution package may load v4l2loopback for
you with no pin at all — Fedora's OBS packaging does, via
`/usr/lib/modules-load.d/v4l2loopback.conf` — and then the number is whatever
it won that boot. `/dev/video9` also works if you did set `video_nr`.

**Kamoso does not work for this, and nor does anything else built on
GStreamer.** Measured: GStreamer's device monitor enumerates only the C920
and silently drops the loopback, emitting `GstIntRange` criticals while
probing it — its single discrete frame rate makes a degenerate range and the
device is discarded. Forcing the V4L2 provider ahead of the PipeWire one
does not help. The device itself is fine: `v4l2-ctl` reports a clean
`YU12 1280x720 @30fps` and `gst-launch v4l2src device=/dev/video2` plays it
happily when named explicitly. It is enumeration that fails, not capture.

What does work, both pre-installed on Fedora:

```bash
ffplay -f v4l2 -window_title "camscan preview" -i "$(camscan-escl --print-loopback)"
```

```bash
gst-launch-1.0 v4l2src device="$(camscan-escl --print-loopback)" ! videoconvert ! autovideosink
```

Neither needs a device picker, which sidesteps the enumeration problem
entirely. Do not open the C920 itself — the daemon holds it and any app
trying will get `Device or resource busy`. The marks are drawn by ffmpeg as part of the same
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
mode is some other shape. Measured here: 16:9 modes are the still's full width
with a centred vertical crop, so the preview shows the middle 1296 of 1536
rows and **the scanner sees 120 rows more at the top and bottom**. In a mode
taller than 3:2 it is columns instead — 4:3 shows the middle 2048 of 2304, so
the scanner sees 128 columns more at each side. The page says which, and marks
that leave the frame are labelled. With an edge anchor it is one edge rather
than two.

**The streaming mode is a free variable.** The settings window's *Video*
dropdown lists whatever this camera reports — asked of the driver, not a list
baked into the daemon — and picking one changes only how many pixels the
preview carries. The canvas, the band, which axis the strips fall on, the
strip the anchored edge drops and where every mark lands are all derived, so
nothing else needs touching, and what a scan captures does not move. On the
C920 that is 17 sizes across six aspect ratios, from 160×90 to 1920×1080; the
largest gives a 1180×1920 canvas against 787×1280 at the default.

A mode's field of view is the largest centred rectangle of its shape that fits
the sensor. **Which axis the unstreamed strips fall on depends on the mode**,
not just on the camera's rotation: 16:9 on a 3:2 sensor keeps the full width
and leaves 120 rows over, while 4:3 keeps the full height and leaves 128
columns over. Turn the camera and those move again — a 4:3 preview on a
portrait-mounted camera puts the strips above and below the picture, so a
`top-left` anchor drops the top one.

## Calibrate before trusting the output

Two values decide whether a scan comes out at the right scale:

- **`rig.coverage_mm`** — the physical area the scannable area actually covers
  at rig height. Put a ruler under the camera and measure it. This is what
  maps an eSCL scan region onto the sensor. Set `rig.anchor` first: an edge
  anchor makes the scannable area 8% smaller, so a measurement taken before it
  is wrong after it. There is no target height, only a window — 218.5 mm to
  239.8 mm of edge-anchored coverage width keeps A4, A5 and Letter inside the
  frame and at or above the declared 150 dpi.
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

- **`capture.image.power_line_frequency`** — on by default, as `"auto"`, and
  it should need no attention. The camera cancels flicker at the mains
  frequency, and the C920 powers up expecting 60 Hz; on a 50 Hz grid that
  bands the picture under artificial light and reads as a bad camera rather
  than a bad setting. `"auto"` takes the host's timezone from
  `/etc/localtime` and looks it up, so a New Zealand host pins 50 without
  being asked. Set `"50"` or `"60"` outright for a rig running off a grid its
  host is not on, `"disabled"` to turn the camera's filter off, or `""` to
  leave the control wherever something else left it.

- **`capture.exposure`** — off by default, and worth turning on. Left on
  auto, the sensor re-decides every capture: a scan on this rig came back with
  89% of its pixels at 250+ luma from a scene that metered correctly minutes
  later. Sweep for a value that holds mid-grey, then pin it:

```bash
CAM=$(camscan-escl --print-camera); for t in 60 90 120 160 220; do v4l2-ctl -d "$CAM" -c auto_exposure=1 -c exposure_time_absolute=$t; ffmpeg -loglevel error -f v4l2 -pix_fmt yuyv422 -video_size 2304x1536 -i "$CAM" -frames:v 3 -update 1 -y /tmp/e$t.png; done
```

  Pick the one where paper is bright but not clipped, put it in
  `time_absolute`, and set `lock = true`. Restore auto with
  `v4l2-ctl -d "$(camscan-escl --print-camera)" -c auto_exposure=3 -c white_balance_automatic=1`.

  Stop the preview first — `camscan-escl-gui`'s Stop button, or
  `curl -X POST localhost:8090/preview/stop` — or the daemon will be holding
  the camera and every `ffmpeg` above will get `Device or resource busy`.

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

5. **§6's platen is too small for a landscape page.** eSCL has no
   orientation field at all — verified against NAPS2's `EsclScanSettings`,
   which carries only Width, Height, offsets, format, resolution and colour.
   A landscape scan simply *is* a region wider than it is tall, and NAPS2 can
   define one as a custom page size. Against the declared 2550-unit width a
   landscape A4 was clamped to 1275px where the contract says 1754 — the same
   silent truncation as erratum 3. The platen now declares the longest side
   of any supported paper in both axes.

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
