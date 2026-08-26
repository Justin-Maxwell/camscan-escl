# `camscan-escl` — implementation spec

A minimal eSCL (AirScan) daemon that presents a USB webcam to scanning
front-ends as if it were a flatbed scanner.

Target consumer: NAPS2 on Fedora 44 / KDE Plasma, via its **ESCL Driver →
Manual IP** option. Secondary consumer: `sane-airscan`, used as the
development harness.

---

## 1. Why this exists

- The host has a Logitech C920 that can deliver **2304×1536** stills.
- No scanning front-end can reach it.
    - SANE's `v4l` backend targets **V4L1**, and hardcodes a scan-area clamp
      of **767 × 511** in `backend/v4l.c`, applied *after* the device
      capability query.
    - So no loopback device, config edit or camera swap raises that ceiling.
    - Verified empirically: `scanimage` through that backend returns 640×480.
- `sane-gphoto2` reaches PTP cameras, not UVC webcams.
    - The attached Pixel 10 enumerates over PTP but reports
      `Capture not supported by the driver`.
- eSCL is the one remaining path into NAPS2 that bypasses SANE entirely.

**The bet:** the eSCL surface needed for a single-page JPEG scanner is small
enough to implement directly, and NAPS2 accepts a manually addressed device,
so no service discovery is required to be useful.

---

## 2. Scope

### In scope

- Single flatbed ("Platen") source. One page per scan job.
- JPEG output. Colour and greyscale.
- One declared resolution, derived from the physical rig geometry.
- Capture by shelling out to a configurable external command.
- Plain HTTP on a configurable TCP port.

### Out of scope for v1

- mDNS / DNS-SD advertisement. See §10.
- ADF, duplex, multi-page jobs.
- TLS. NAPS2's dialog defaults to HTTPS ticked; the user unticks it.
- PDF or raster output formats.
- Live preview. `fswebcam` is a blind capture; framing is a rig-setup
  concern, not a per-scan one.
- Any image correction — deskew, dewarp, perspective. The front-end does
  that, or a later version does.

---

## 3. Architecture

```
NAPS2  ──HTTP──►  camscan-escl  ──subprocess──►  fswebcam / ffmpeg
 (ESCL driver)      (this daemon)                     (v4l2)
                          │
                          └──► Pillow: crop / scale to contract dimensions
```

- Single process, no persistent state beyond in-memory job records.
- Python 3.11+, stdlib `http.server` is sufficient. Pillow for image work.
- One job at a time. Reject a second concurrent `POST /ScanJobs` with `503`.
- Systemd user unit, `linger` enabled, matching existing host conventions.

---

## 4. HTTP interface

Base path is **`/eSCL`**. NAPS2's Manual IP dialog collects host and port
only, so the path is assumed by the client and must not be configurable
away from this default.

| Method | Path | Success | Body |
|---|---|---|---|
| `GET` | `/eSCL/ScannerCapabilities` | `200` | `text/xml` |
| `GET` | `/eSCL/ScannerStatus` | `200` | `text/xml` |
| `POST` | `/eSCL/ScanJobs` | `201` | empty, `Location:` header set |
| `GET` | `/eSCL/ScanJobs/{id}/NextDocument` | `200` / `404` | `image/jpeg` |
| `DELETE` | `/eSCL/ScanJobs/{id}` | `200` | empty |

### Notes per endpoint

- **`POST /ScanJobs`**
    - Request body is a `scan:ScanSettings` XML document.
    - Response `Location` must be an **absolute** URL:
      `http://{host}:{port}/eSCL/ScanJobs/{uuid}`.
    - Build the host portion from the request's `Host:` header, not from a
      configured value — the client may reach the daemon by IP, hostname or
      Tailscale name, and a mismatch here breaks the follow-up GET.
- **`GET .../NextDocument`**
    - First call: perform the capture, return the JPEG.
    - Subsequent calls: `404`. This is how the client learns the job has no
      more pages. It is not an error condition.
    - Capture latency is seconds; do not impose a short timeout.
- **`DELETE .../ScanJobs/{id}`**
    - Clients may or may not call it. Reap jobs older than 5 minutes
      regardless.

---

## 5. The units contract — read this twice

**eSCL expresses all geometry in units of 1/300 inch, regardless of the
scan resolution.** This is the single most common source of silent
misbehaviour in eSCL implementations.

- A4 = 210 × 297 mm = 8.268 × 11.693 in = **2480 × 3508** units.
- US Letter = 8.5 × 11 in = **2550 × 3300** units.

### The dimension contract

The client computes expected pixel dimensions itself:

```
expected_px_width  = ScanRegion.Width  × XResolution ÷ 300
expected_px_height = ScanRegion.Height × YResolution ÷ 300
```

**The returned JPEG must match those dimensions exactly.** If it does not,
behaviour ranges from a wrong-sized page to an outright client error, and
the failure is not self-explaining.

So the capture pipeline is:

1. Grab the native frame (2304×1536).
2. Map the requested `ScanRegion` onto the physical area the camera covers,
   using `rig.coverage_mm` from config.
3. Crop to that sub-rectangle.
4. Scale to `expected_px_width × expected_px_height`.
5. Convert colour mode if `Grayscale8` was requested.
6. Encode JPEG at the configured quality.

If the requested region exceeds the camera's physical coverage, **pad with
white** rather than scaling the content — the page genuinely was not in
frame, and stretching it would silently lie about scale.

---

## 6. ScannerCapabilities

This document is what populates NAPS2's Resolution, Bit depth and Page size
dropdowns. Get it wrong and the client either refuses the device or offers
settings the daemon cannot honour.

### ⚠ Validate the schema before trusting the skeleton below

The XML that follows is written from recollection of the eSCL schema and
**has not been checked against the specification**. Before implementing:

- Obtain the *Mopria Alliance eSCL Scan Technical Specification* (published
  publicly, behind a licence click-through) and validate element names,
  ordering and namespaces against it.
- Cross-check against a real implementation — `AirSane`
  (`SimulPiscator/AirSane`) generates conforming capabilities XML and is the
  most readable reference.
- Treat any discrepancy as the spec being right and this document being
  wrong.

### Skeleton

```xml
<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <pwg:Version>2.6</pwg:Version>
  <pwg:MakeAndModel>camscan-escl (Logitech C920)</pwg:MakeAndModel>
  <pwg:SerialNumber>camscan-0001</pwg:SerialNumber>
  <scan:Platen>
    <scan:PlatenInputCaps>
      <scan:MinWidth>16</scan:MinWidth>
      <scan:MinHeight>16</scan:MinHeight>
      <scan:MaxWidth>2480</scan:MaxWidth>
      <scan:MaxHeight>3508</scan:MaxHeight>
      <scan:MaxScanRegions>1</scan:MaxScanRegions>
      <scan:SettingProfiles>
        <scan:SettingProfile>
          <scan:ColorModes>
            <scan:ColorMode>RGB24</scan:ColorMode>
            <scan:ColorMode>Grayscale8</scan:ColorMode>
          </scan:ColorModes>
          <scan:DocumentFormats>
            <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
            <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
          </scan:DocumentFormats>
          <scan:SupportedResolutions>
            <scan:DiscreteResolutions>
              <scan:DiscreteResolution>
                <scan:XResolution>150</scan:XResolution>
                <scan:YResolution>150</scan:YResolution>
              </scan:DiscreteResolution>
            </scan:DiscreteResolutions>
          </scan:SupportedResolutions>
        </scan:SettingProfile>
      </scan:SettingProfiles>
    </scan:PlatenInputCaps>
  </scan:Platen>
</scan:ScannerCapabilities>
```

### On declaring resolution

Declare **one** discrete resolution, and declare one the rig can actually
deliver. The optical ceiling is fixed by geometry:

```
DPI ≈ 3454 / distance_cm      (camera portrait, 1536 px across the short axis
                               at the 2304×1536 still mode)
```

- A4 needs ≥ 26.4 cm to fit the frame at all.
- At 30 cm with placement margin: ~115 DPI in the 1080p mode, ~166 DPI in
  the 2304×1536 still mode.

Declaring 150 DPI is honest at a ~23–25 cm rig height. Declaring 300 is not,
and upscaling to meet the dimension contract would fabricate detail.

Make it a config value so the rig can be re-measured without a code change.

---

## 7. ScannerStatus

```xml
<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">
  <pwg:Version>2.6</pwg:Version>
  <pwg:State>Idle</pwg:State>
</scan:ScannerStatus>
```

- `Idle` when no job is active, `Processing` while a capture is running.
- Some clients poll this before offering a scan. Never return `Down` on a
  transient failure — return `Idle` and fail the job instead, so the device
  stays selectable.

---

## 8. Capture

### Default command

```
fswebcam -d /dev/video0 -r 2304x1536 --no-banner -S 4 --jpeg 92 %f
```

- `-S 4` discards four frames before capturing. **Not optional.** The first
  buffers off a freshly opened device are stale or mid-exposure, and at
  2 fps in this mode the effect is severe.
- The command must be a config string with `%f` substituted for a temp path.
  Do not hardcode `fswebcam`.

### Fallback

`fswebcam` may negotiate MJPG and silently land at 1920×1080, because
**2304×1536 exists only in the YUYV format** on this device. Verify the
output dimensions after every capture; if they do not match the configured
native size, log it loudly rather than silently scaling.

The ffmpeg equivalent, which pins the format explicitly:

```
ffmpeg -loglevel error -f v4l2 -pix_fmt yuyv422 -video_size 2304x1536 \
       -i /dev/video0 -frames:v 3 -update 1 -y %f
```

### Focus

Run before capture, values from config:

```
v4l2-ctl -d /dev/video0 -c focus_automatic_continuous=0 -c focus_absolute=N
```

- The control is named `focus_automatic_continuous` on current kernels and
  `focus_auto` on older ones. Detect which exists; do not assume.
- `N` comes from a one-off sweep at the rig's working distance. Autofocus
  left enabled will hunt and produce inconsistent sharpness between pages.

---

## 9. Configuration

TOML at `~/.config/camscan-escl/config.toml`.

```toml
[server]
port = 8090
bind = "127.0.0.1"          # widen only if scanning from another host

[scanner]
make_and_model = "camscan-escl (Logitech C920)"
serial = "camscan-0001"
resolution_dpi = 150
jpeg_quality = 88

[capture]
command = "fswebcam -d /dev/video0 -r 2304x1536 --no-banner -S 4 --jpeg 92 %f"
native_width = 2304
native_height = 1536
timeout_s = 30

[capture.focus]
device = "/dev/video0"
absolute = 40               # from the focus sweep
disable_autofocus = true

[rig]
coverage_mm = [210.0, 297.0]   # physical area the frame covers at rig height
```

`rig.coverage_mm` is the calibration that makes the units contract correct.
Measure it: put a ruler under the camera and record what the frame spans.

---

## 10. Discovery (deferred, not abandoned)

NAPS2 has **Manual IP**, so v1 does not need mDNS. When it's wanted:

- Advertise `_uscan._tcp` on the configured port.
- TXT records at minimum: `rs=eSCL`, `ty={make_and_model}`,
  `pdl=image/jpeg`, `cs=color,grayscale`, `vers=2.6`, `representation=`.
- Either `python-zeroconf` in-process, or a static Avahi service file in
  `/etc/avahi/services/`. The Avahi file is less code and survives daemon
  restarts; the in-process route keeps advertisement and reality in sync.

---

## 11. Test plan

Work through these in order. Do not skip to NAPS2 — its error reporting is
far worse than `scanimage`'s.

### Stage 1 — endpoints in isolation

```bash
curl -s http://127.0.0.1:8090/eSCL/ScannerCapabilities | xmllint --format -
curl -s http://127.0.0.1:8090/eSCL/ScannerStatus | xmllint --format -
```

Both must be well-formed XML with correct namespaces.

### Stage 2 — sane-airscan, no discovery

`sane-airscan` accepts a URL directly in the device name, format
`protocol:Device Name:URL`:

```bash
scanimage -d 'escl:camscan:http://127.0.0.1:8090/eSCL' -L
scanimage -d 'escl:camscan:http://127.0.0.1:8090/eSCL' \
          --format=jpeg > /tmp/t.jpg
python3 -c "from PIL import Image; print(Image.open('/tmp/t.jpg').size)"
```

Enable the protocol trace when it misbehaves — it is the fastest debugging
tool available here:

```ini
# /etc/sane.d/airscan.conf
[debug]
enable = true
trace = ~/airscan-trace
```

### Stage 3 — dimension contract

For each of A4 and Letter, at the declared resolution, assert the returned
pixel dimensions equal `region × resolution ÷ 300`. Automate this; it is the
regression most likely to reappear.

### Stage 4 — NAPS2

- Profile Settings → Choose device → **ESCL Driver** → **Manual IP**.
- Enter host and port. **Untick HTTPS** — it is ticked by default and the
  daemon serves plain HTTP.
- Confirm the Resolution dropdown shows only the declared value, and Bit
  depth offers 24-bit Colour and greyscale.
- Scan. Confirm the page appears at the expected size.

---

## 12. Acceptance criteria

1. `scanimage -L` lists the device via a manually specified eSCL URL.
2. A scan through `scanimage` returns a JPEG whose dimensions satisfy the
   units contract for both A4 and Letter.
3. NAPS2 connects via Manual IP with HTTPS unticked, and its dropdowns
   reflect the declared capabilities.
4. A scan initiated from NAPS2's Scan button produces a correctly sized page
   in the NAPS2 GUI.
5. A second `NextDocument` on the same job returns `404` and the job ends
   cleanly rather than hanging.
6. Two scans in succession work — no stale frame from the first appearing in
   the second.
7. The daemon survives a capture failure (camera unplugged) without needing
   a restart, and reports `Idle` afterwards.

---

## 13. Known risks

- **Capability negotiation is the tail that eats the schedule.** The
  protocol surface is small; client fussiness is not. Budget accordingly.
- **The capabilities XML in §6 is unverified.** Validating it against the
  Mopria spec and AirSane is the first task, not an afterthought.
- **`fswebcam` may not reach 2304×1536** because that mode is YUYV-only.
  The verify-dimensions-after-capture check exists for this.
- **Blind capture.** There is no viewfinder in this design. Acceptable only
  because the rig is fixed and NAPS2 shows the result afterwards. If page
  framing turns out to need per-page adjustment, this architecture is the
  wrong one and `suhren/camscan` — which has a live feed and page detection
  — is the better tool.

---

## 14. Prior art worth reading first

- `SimulPiscator/AirSane` — C++ eSCL server. Wraps SANE rather than a
  command, so the wrong shape overall, but its capabilities XML generation
  and job lifecycle are the best available reference.
- `markosjal/AirScan` — PHP eSCL server built explicitly to wrap a
  command-line scanner, which is precisely this shape. Old, tested on
  Ubuntu 16.04, author describes the eSCL side as incomplete. Read for the
  approach, not the code.
- `alexpevzner/sane-airscan` — the client used in Stage 2. Its source is the
  authority on what a client actually requires versus what the spec permits.
