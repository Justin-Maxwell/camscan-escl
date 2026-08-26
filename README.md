# camscan-escl

A minimal eSCL (AirScan) daemon that presents a USB webcam to scanning
front-ends as if it were a flatbed scanner. Target consumer: NAPS2 via its
**ESCL Driver → Manual IP** option; development harness: `sane-airscan`.

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

**`server.bind` gates who can use it.** At the default `127.0.0.1` the advert
carries `127.0.0.1`, so only this host can scan — other machines will see the
device and fail to connect. Widen `bind` to `0.0.0.0` to scan from elsewhere.

Check what is being advertised with:

```bash
avahi-browse -rt _uscan._tcp
```

## Calibrate before trusting the output

Two values decide whether a scan comes out at the right scale:

- **`rig.coverage_mm`** — the physical area the frame actually covers at rig
  height. Put a ruler under the camera and measure it. This is what maps an
  eSCL scan region onto the sensor.
- **`capture.focus.absolute`** — from a one-off focus sweep at the working
  distance. Autofocus left on will hunt between pages.

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

Two corrections to `docs/camscan-escl-spec.md`, both found by running §11
stage 2 against the real camera on this host:

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

## Known-unverified

**The capabilities XML has not been validated against the Mopria eSCL Scan
Technical Specification.** It reproduces the skeleton in spec §6, which that
document itself flags as written from recollection. Validating it — against
the spec and against AirSane's generated output — is the first task, per
§13. Everything downstream of client acceptance is untestable until then.

Discovery (§10) is no longer deferred: `scanimage -L` finds the device with
no URL given, and a scan through the discovered name returns a correctly
sized page. NAPS2's own device search has not yet been tried against it.
