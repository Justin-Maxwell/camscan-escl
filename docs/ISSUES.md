# Open issues

This file is the issue tracker. Newest first. Close an item by deleting it and
saying so in the commit message.

(It used to say there was no git remote. There is one now —
`github.com/Justin-Maxwell/camscan-escl` — but the tracker stays here.)

---

## 10. Next session: the "drop a side" fix

**Status:** agreed in principle, not designed
**Depends on:** issue 8, which establishes that the camera will not move the
streamed band

The streamed band is the middle 1296 of the sensor's 1536 rows, fixed and
centred, so 120 rows on each side are scannable but never visible live. An
edge-anchored page therefore has its alignment edge in the ghost, not in the
moving picture.

The fix as understood: **give up one of the two strips**. Define the scannable
area as the streamed band plus the 120-row strip on ONE side only. The coverage
edge on the anchored side then coincides with the stream edge, so an
edge-anchored page is fully visible live — bought with about 8% of the sensor
on the far side.

> **Confirm the intent before building this.** Justin called it "your 'drop a
> side' fix", and the above is a reconstruction from the geometry rather than
> a proposal made in so many words. He also called it "yuk, but meh", which
> fits throwing away sensor area. Worth thirty seconds of checking rather than
> a session of building the wrong thing.

---

## 9. Mains frequency is never set, and the camera's default is wrong here

**Status:** open
**Control:** `power_line_frequency` — 0 Disabled, 1 = 50 Hz, 2 = 60 Hz

Currently reads **1 (50 Hz)**, which is right for New Zealand. But the C920's
**default is 2 (60 Hz)**, and the daemon never sets it. Nothing here put it on
50 — something else did, and a replug or a module reload will silently put it
back to 60, which bands the picture under artificial light at 50 Hz mains.

Same hazard class as issue 8: persistent V4L2 state the daemon depends on and
does not own. Unlike pan/tilt/zoom this one costs image quality rather than
geometry, so it will look like a bad camera rather than a bad setting.

Should be pinned alongside focus and exposure, with a config key.

**Autodetection** — Justin's instinct is that it should not need asking, and
there are two routes:

- *From the host.* `timedatectl` gives `Pacific/Auckland`; most of the world is
  50 Hz, the Americas and a few others 60. A timezone-to-frequency table is
  small, static and gets it right nearly everywhere. Cheap, and wrong only for
  someone running the rig outside their own grid.
- *From the picture.* Under artificial light a mismatched setting produces
  banding that beats at the difference frequency. Capture a short run at fixed
  exposure, look for horizontal periodicity, pick whichever setting has less.
  Robust and self-correcting, but needs artificial light to decide at all.

The host route is the one to build first; the picture route is the check.

---

## 1. NAPS2's device search ignores a valid eSCL advert — report upstream

**Status:** open, blocked on someone writing it up
**Affects:** NAPS2 8.3.2 (`naps2-8.3.2-1.x86_64`), which bundles `NAPS2.Mdns 1.0.1`
**Workaround:** Manual IP, which works

NAPS2's ESCL device search never contacts this daemon, though everything its
own code requires is present. The evidence is three packet captures, kept
locally and **not published** — they record this host's LAN traffic including
neighbouring devices, and have not been vetted for that. Summarised:

- `three.pcapng` — NAPS2 queries `_uscan._tcp` every 2 s over IPv4 and IPv6;
  we answer in ~30 ms with PTR + TXT + SRV + A. No connection to 8090 in 274
  frames.
- `four.pcapng` — after we began answering on IPv6 too, still nothing, in 371
  frames.
- `five.pcapng` — **the decisive one.** Records republished through Avahi's
  own responder, cache freshly restarted, no ghost instances. Avahi answers
  48 ms after NAPS2's query with six records in the *answers* section: PTR,
  TXT, SRV, two AAAA and an A. No connection to 8090 in 2572 frames.

Every condition in `NAPS2.Escl/Client/EsclServiceLocator.cs` is met:
`ServiceInstanceName.Labels[1] == "_uscan"`, a lowercase `uuid` TXT key, an
address record, and an SRV target and port that resolve. Our responses carry
the IP TTL of 255 that RFC 6762 §11 mandates; NAPS2's own queries carry
TTL 1, which suggests a hand-rolled stack.

Since a battle-tested responder publishing textbook records fails identically,
the fault is on the client side and nothing we publish can fix it.

**Before filing:** check whether a NAPS2 newer than 8.3.2 behaves differently,
and whether `NAPS2.Mdns` has moved past 1.0.1. This may already be fixed.

---

## 6. A scan takes ~6s, and ~4.8s of that is the camera, not us

**Status:** understood, not fixable without trading resolution

Measured breakdown of a scan with the preview running, after the SIGKILL fix
that removed 5.3s of dead air:

| phase | time |
|---|---|
| release the camera from the preview | 0.16 s |
| capture | 4.8 s |
| resume the preview | 0.00 s |

The capture is the camera. Timing ffmpeg's own debug output puts 0.16s in
process startup and **2.33s between opening the fd and the stream being
ready** — the uvcvideo driver and the camera firmware doing UVC probe/commit,
reserving USB bandwidth, allocating buffers, and waiting for a first frame.

Open-to-ready by mode, which localises it to data volume on the wire rather
than resolution as such:

| mode | open → ready | frame |
|---|---|---|
| 640x480 YUYV | 0.27 s | 0.61 MB |
| 1920x1080 YUYV | 0.61 s | 4.15 MB |
| 2304x1536 YUYV | 2.33 s | 7.08 MB |
| 1920x1080 **MJPG** | 0.29 s | compressed |

Same pixel count, half the time, when compressed.

`v4l2-ctl --list-frameintervals` reports **2.000 fps** as the only rate for
2304x1536, and the C920 is a **USB 2.0 device** (`/sys/.../version` = 2.00,
so a USB 3 port cannot help — the ceiling is the camera's). 7.08 MB at 2 fps
is 113 Mbps on a 480 Mbps bus shared with the camera's audio interfaces, so
the frame rate is bandwidth-limited and each frame is 0.5s apart.

The only lever is MJPG 1920x1080: a capture of ~0.6s and a scan of ~2s. The
cost is 1080 rows against 1536, and 16:9, so the preview geometry would need
redoing. Note we already upscale — A4 at 150 dpi wants 1754 rows and the
sensor gives 1536 — so 1080 would mean upscaling 62% and the declared dpi
would drift further from honest. Not recommended without a decision to drop
the declared resolution too.

---

## 5. GStreamer will not enumerate the v4l2loopback preview device

**Status:** open, worked around
**Workaround:** `ffplay -f v4l2 -i "$(camscan-escl --print-loopback)"`, which
does not enumerate

> Device numbers below were written on a boot where the loopback was
> `/dev/video2`. They move — see `camscan-escl --diagnose` for what they are
> now, and the "When the preview is blank" section of the README for why.

Kamoso, and anything else built on GStreamer, lists only the real camera and
never the loopback the preview is published on. Measured with GStreamer's own
`DeviceMonitor`: one device returned, and `GstIntRange` criticals raised while
probing the loopback — its single discrete frame rate (30/1) yields a range
whose start is not less than its end, so caps construction fails and the
device is dropped. Demoting `pipewiredeviceprovider` and forcing
`v4l2deviceprovider` changes nothing.

Capture itself is fine. `v4l2-ctl -d /dev/video2 --list-formats-ext` reports a
clean `YU12 1280x720 @30fps`, `gst-launch-1.0 v4l2src device=/dev/video2`
plays it, and ffmpeg reads frames from it. Only enumeration fails.

Worth trying: publishing more than one frame-rate or a rate *range* on the
loopback, which may be enough for GStreamer to build valid caps. Whether
ffmpeg's v4l2 output can advertise that is unknown.

---

## 8. Camera zoom silently invalidates every crop mark

**Status:** open, detected but not prevented
**Detected by:** `/preview/status`, and the Daemon panel's one-line summary

`zoom_absolute` changes the **streaming** mode's field of view and leaves the
**still** untouched. Measured on this rig, one setting at a time, cross-
correlating a streaming frame against the still it was captured beside:

| pan | tilt | zoom | stream sits at row | centred crop predicts |
|---|---|---|---|---|
| 0 | 0 | 100 | 65 | 66 |
| +18000 | 0 | 100 | 67 | 66 |
| −18000 | 0 | 100 | 67 | 66 |
| 0 | +18000 | 100 | 67 | 66 |
| 0 | 0 | **200** | **56** | 66 |
| **+18000** | 0 | **200** | **52** | 66 |

So at the default zoom of 100 pan and tilt do nothing. At 200 the stream is
visibly zoomed while the still is pixel-identical, and pan then shifts it
further.

**Pan has no room; tilt has room and is ignored anyway.** The stream is the
still's full width, so there is nowhere to pan — but it is only 1296 of 1536
rows, leaving 240 spare, so tilt could in principle slide the streamed band up
and down the sensor. It does not. Measured by comparing stream frames against
each other, which is far more sensitive than measuring each against the still:

| tilt | reads back | vertical shift vs tilt=0 |
|---|---|---|
| −36000 | −36000 | 0.0 px |
| −18000 | −18000 | 0.0 px |
| +18000 | +18000 | 0.0 px |
| +36000 | +36000 | 0.0 px |

The control accepts the value and reports it back, the captures are genuinely
distinct (different checksums, mean absolute difference 7.41 between the
extremes — sensor noise), and nothing moves.

**Zooming does unlock tilt, and it still does not help.** Tilt engages at
`zoom_absolute = 105`, the first step above default:

| zoom | tilt travel | field of view retained |
|---|---|---|
| 100 | 0 px | 100% |
| 105 | 32 px | 96% |
| 110 | 68 px | 91% |
| 125 | 180 px | 80% |
| 150 | 104 px | 67% |
| 200 | 168 px | 56% |

But the crop window is confined to the same band the 1× stream already
occupies. Located by correlating each frame against the full still:

| frame | covers still rows |
|---|---|
| zoom 100, tilt 0 | 120 – 1416 |
| zoom 125, tilt −36000 | 384 – 1420 |
| zoom 125, tilt +36000 | 120 – 1156 |

The tilt extremes span rows 120–1420, which is the 1× band to within the
measurement's granularity. Tilt slides a *smaller* window around inside the
same active area; it never reaches the 240 rows outside it.

So the appealing idea — tilting the streamed band to sit over whichever edge
the crop marks are anchored to, so alignment could be watched live instead of
through the ghost — cannot work. Zooming to unlock tilt costs field of view,
softens the picture (it is a digital crop, upscaled from fewer sensor pixels),
shifts the streamed band relative to the still so every crop mark moves, and
buys no new view in return.

The way to get a page inside the live band is to raise the camera until the
coverage is wide enough. The band is a fixed 84.375% of the coverage, centred,
so a sheet is fully live once `coverage_width >= paper_width / 0.84375`:
249 mm for A4, 256 mm for Letter and Legal. At 256 mm the sensor still
delivers 152 dpi, above the declared 150.

Every crop mark is placed by the centred-crop relationship between those two
modes, so a non-default zoom moves the marks and nothing in the pipeline can
tell. The scan itself is unaffected, which makes it worse: the output is fine
and the positioning aid lies.

The daemon never sets these controls, but V4L2 state lives on the device and
survives whatever last touched it — OBS, a stray `v4l2-ctl`, another app.

**Not prevented**, only reported, because resetting them on every capture
would fight anything else deliberately using the camera zoomed. If that turns
out to be nobody, pin them alongside focus and exposure.

---

## 7. GTK warns about the brightness/contrast sliders on every start

**Status:** open, cosmetic, believed to be a GTK bug
**Symptom:** `GtkGizmo (slider) reported min width -2, but sizes must be >= 0`,
twice, at startup

A `Gtk.Scale` inside a `Gtk.ScrolledWindow` emits it. Narrowed by elimination:
reproduced with a stock, unconfigured `Gtk.Scale`, and unaffected by the size
request (none / width only / width and height), by `draw_value`, by `hexpand`,
by the scroll policy (`NEVER`, `AUTOMATIC`, `EXTERNAL`), and by whether a
`Gtk.Paned` is involved. It does *not* appear with the same scale outside a
scroller.

GTK clamps the value to 0 and the sliders lay out and work correctly, so this
is noise rather than a defect. Dropping the `ScrolledWindow` would remove it,
but the sidebar is taller than the window and needs to scroll. Worth
re-checking after a GTK update, and worth reporting upstream with the
elimination above if it survives one.

---

## 2. `rig.coverage_mm` is still a placeholder — scans are the wrong scale

**Status:** open, needs a ruler and five minutes
**Blocks:** every claim that output is correct

**Now visible**: open `/preview` and the crop marks show exactly what this
setting claims. With the placeholder the A4 mark fills the frame precisely
and Letter comes out 1316 px wide in a 1280 px frame, which is the placeholder
announcing itself.

`coverage_mm = [210.0, 297.0]` is a guess, not a measurement. Scans come out
at the right *pixel dimensions* because the units contract is enforced, but
the mapping from eSCL region to sensor area is wrong until the real area the
frame covers at rig height is measured. This is the last thing between the
daemon and trustworthy output — and it is silent, which is what makes it
dangerous.

---

## 3. Exposure lock is off, and needs a sweep at the final camera position

**Status:** open, waiting on the camera being mounted permanently
**See:** README "Calibrate", `[capture.exposure]`

The mechanism is built and verified against the hardware, but ships off
because the right values are rig-specific. Until it is on, every scan is at
the mercy of ambient light: a scan during this work came back with 89% of its
pixels at 250+ luma, from sun hitting the desk at the camera's angle.

---

## 4. The capabilities XML has never been validated against the Mopria spec

**Status:** open since the beginning; spec §13 calls it the first task

`escl.py`'s document reproduces the skeleton in spec §6, which that document
itself flags as written from recollection. It demonstrably satisfies
`sane-airscan` and NAPS2's Manual IP path, which is weaker evidence than it
sounds — two clients agreeing does not make a document conformant, and the
missing `scan:UUID` proved how a plausible-looking document can fail silently.
Validate against the Mopria eSCL Scan Technical Specification and against
AirSane's generated output.
