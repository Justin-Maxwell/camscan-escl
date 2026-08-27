# Open issues

No git remote on this repo, so this file is the issue tracker. Newest first.
Close an item by deleting it and saying so in the commit message.

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
**Workaround:** `ffplay -f v4l2 -i /dev/video2`, which does not enumerate

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
