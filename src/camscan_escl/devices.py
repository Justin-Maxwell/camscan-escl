"""Find V4L2 devices by what they are, not by what number they got.

`/dev/videoN` is not a stable name. The number is handed out in probe order,
and on this rig `v4l2loopback` is autoloaded at boot from
`/usr/lib/modules-load.d/v4l2loopback.conf` with no `video_nr=`, so it races
USB enumeration for the first free node. Measured on the boot of 2026-08-29:

    /dev/video0  OBS Virtual Camera   (loopback, output only)
    /dev/video1  HD Pro Webcam C920   (video capture)
    /dev/video2  HD Pro Webcam C920   (metadata capture)

The config in use had been written against an earlier boot, where the numbers
must have fallen the other way round -- it named /dev/video0 as the camera and
/dev/video2 as the loopback, which is only sane if the loopback lost that
race. That earlier layout is inferred from the config, not observed.

Either way the daemon then read from the loopback and wrote to a metadata
node, and ffmpeg exited with "Not a video capture device" (verified: exit
237). So a device is named here by its card and by the capability it must
have, and the number is looked up fresh every time the pipeline starts.

The capability half is not optional. The C920 owns TWO nodes with the SAME
card string -- one video capture, one metadata -- so matching on the name
alone is a coin toss, and the metadata node fails in exactly the same way the
loopback does.

Spec forms, for `capture.device` and `preview.loopback_device`:

    /dev/video1                  a literal path, used as given
    card:HD Pro Webcam C920      first node with that card and the right role
    auto                         first node with the right role, any card
    ""                           unset; the caller decides what that means
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

# struct v4l2_capability, from linux/videodev2.h:
#   __u8 driver[16]; __u8 card[32]; __u8 bus_info[32];
#   __u32 version, capabilities, device_caps; __u32 reserved[3];
_CAP_FMT = "16s32s32sIII12x"
_CAP_SIZE = struct.calcsize(_CAP_FMT)

# _IOR('V', 0, struct v4l2_capability)
VIDIOC_QUERYCAP = (2 << 30) | (_CAP_SIZE << 16) | (ord("V") << 8) | 0

CAP_VIDEO_CAPTURE = 0x00000001
CAP_VIDEO_OUTPUT = 0x00000002
CAP_META_CAPTURE = 0x00800000
CAP_DEVICE_CAPS = 0x80000000

# What a caller wants the device *for*, and the capability bit that proves it.
ROLES = {"capture": CAP_VIDEO_CAPTURE, "output": CAP_VIDEO_OUTPUT}

# v4l2loopback identifies itself here. Matched on the driver rather than the
# capability bits because those MOVE: with `exclusive_caps=1`, which is what
# /usr/lib/modprobe.d/98-v4l2loopback.conf sets, the device advertises
# VIDEO_OUTPUT while idle and VIDEO_CAPTURE once a producer attaches -- so it
# is the consumers' view that wins the moment the preview starts working.
# Verified live: the same /dev/video0 reported (output) with the pipeline
# stopped and (capture) with ffmpeg feeding it. Testing the capability alone
# therefore called a healthy loopback unusable exactly when it was in use.
LOOPBACK_DRIVER = "v4l2loopback"

CARD_PREFIX = "card:"
AUTO = "auto"


class DeviceError(RuntimeError):
    """No device matched the spec, and the message says what was there."""


def _text(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", "replace")


@dataclass(frozen=True)
class V4L2Device:
    path: str
    card: str
    driver: str
    bus_info: str
    caps: int

    @property
    def can_capture(self) -> bool:
        return bool(self.caps & CAP_VIDEO_CAPTURE)

    @property
    def can_output(self) -> bool:
        return bool(self.caps & CAP_VIDEO_OUTPUT)

    @property
    def is_metadata(self) -> bool:
        return bool(self.caps & CAP_META_CAPTURE) and not self.can_capture

    @property
    def is_loopback(self) -> bool:
        return self.driver.strip().lower().replace(" ", "") == LOOPBACK_DRIVER

    def has_role(self, role: str) -> bool:
        # A loopback is always a legitimate publish target, whichever
        # direction exclusive_caps has it advertising this second.
        if role == "output" and self.is_loopback:
            return True
        bit = ROLES.get(role)
        return bool(bit and self.caps & bit)

    @property
    def kind(self) -> str:
        """A short word for the diagnostic panel."""
        if self.is_loopback:
            return "loopback"
        if self.can_capture:
            return "capture"
        if self.can_output:
            return "output"
        if self.caps & CAP_META_CAPTURE:
            return "metadata"
        return "other"


def probe(path: str) -> V4L2Device | None:
    """QUERYCAP one node. Returns None if it cannot be opened or queried.

    Opened O_RDWR|O_NONBLOCK, which is what v4l2-ctl does: QUERYCAP does not
    start streaming, so this is safe to call against a device the daemon is
    already using -- and it has to be, since the diagnostic runs while the
    preview holds the camera.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            log.debug("cannot open %s: %s", path, exc)
            return None
    try:
        buf = fcntl.ioctl(fd, VIDIOC_QUERYCAP, bytes(_CAP_SIZE))
    except OSError as exc:
        log.debug("QUERYCAP failed on %s: %s", path, exc)
        return None
    finally:
        os.close(fd)

    driver, card, bus, _version, caps, device_caps = struct.unpack(_CAP_FMT, buf)
    # device_caps describes THIS node; capabilities describes everything the
    # physical device owns across all its nodes. Using the latter would say
    # the C920's metadata node can capture video, because a sibling node can.
    effective = device_caps if caps & CAP_DEVICE_CAPS else caps
    return V4L2Device(
        path=path,
        card=_text(card),
        driver=_text(driver),
        bus_info=_text(bus),
        caps=effective,
    )


def _node_key(path: str) -> tuple[int, str]:
    """Sort /dev/video10 after /dev/video9, not between 1 and 2."""
    digits = "".join(c for c in os.path.basename(path) if c.isdigit())
    return (int(digits) if digits else 1 << 30, path)


def enumerate_devices() -> list[V4L2Device]:
    """Every /dev/video* node that answers QUERYCAP, in node-number order."""
    found = []
    for path in sorted(glob.glob("/dev/video*"), key=_node_key):
        device = probe(path)
        if device is not None:
            found.append(device)
    return found


def _match_card(device: V4L2Device, wanted: str) -> bool:
    card, wanted = device.card.strip().lower(), wanted.strip().lower()
    return card == wanted or wanted in card


def resolve(spec: str, role: str = "capture",
            devices: list[V4L2Device] | None = None) -> str:
    """Turn a device spec into a path, or raise DeviceError explaining why not.

    `role` is "capture" or "output", and it is checked even for a literal
    path: pointing the camera at the loopback is precisely the failure this
    module exists to stop, and it is better caught here with a sentence than
    by ffmpeg with "No such device" on a stderr that used to go to DEVNULL.
    """
    spec = (spec or "").strip()
    if not spec:
        raise DeviceError("no device configured")
    if devices is None:
        devices = enumerate_devices()

    if spec.lower().startswith(CARD_PREFIX):
        wanted = spec[len(CARD_PREFIX):]
        named = [d for d in devices if _match_card(d, wanted)]
        if not named:
            raise DeviceError(
                f"no V4L2 device has a card matching {wanted!r}. Present: "
                + (_inventory(devices) or "nothing")
            )
        usable = [d for d in named if d.has_role(role)]
        if not usable:
            raise DeviceError(
                f"{wanted!r} matched {len(named)} node(s) but none can {role}: "
                + _inventory(named)
            )
        return usable[0].path

    if spec.lower() == AUTO:
        usable = [d for d in devices if d.has_role(role)]
        if role == "capture":
            # Never auto-select a loopback as the camera. A busy one
            # advertises VIDEO_CAPTURE, and it is very likely THIS daemon's
            # own published output -- so "auto" could quietly point the
            # capture at the preview it is producing. Naming it explicitly
            # still works, for anyone who really means it.
            usable = [d for d in usable if not d.is_loopback]
        if not usable:
            raise DeviceError(
                f"no V4L2 device can {role}. Present: "
                + (_inventory(devices) or "nothing")
            )
        return usable[0].path

    # A literal path. Verify it rather than trusting the number.
    device = next((d for d in devices if d.path == spec), None)
    if device is None:
        if not os.path.exists(spec):
            raise DeviceError(f"{spec} does not exist")
        raise DeviceError(f"{spec} exists but is not a usable V4L2 device")
    if not device.has_role(role):
        raise DeviceError(
            f"{spec} is {device.card!r} ({device.kind}), which cannot {role}. "
            "Device numbers move between boots -- name it as "
            f"'{CARD_PREFIX}<card name>' instead. Present: "
            + _inventory(devices)
        )
    return device.path


def _inventory(devices: list[V4L2Device]) -> str:
    return ", ".join(f"{d.path}={d.card!r} ({d.kind})" for d in devices)


def describe(spec: str, role: str = "capture",
             devices: list[V4L2Device] | None = None) -> dict:
    """Resolve for the diagnostic panel: never raises, always explains."""
    if devices is None:
        devices = enumerate_devices()
    out: dict = {"spec": spec, "role": role, "resolved": None, "ok": False,
                 "detail": None}
    try:
        path = resolve(spec, role, devices)
    except DeviceError as exc:
        out["detail"] = str(exc)
        return out
    device = next((d for d in devices if d.path == path), None)
    out["resolved"] = path
    out["ok"] = True
    if device is not None:
        out["card"] = device.card
        out["driver"] = device.driver
        out["detail"] = f"{device.card} ({device.kind})"
    return out


def inventory() -> list[dict]:
    """Every V4L2 node and what it is, for the diagnostic panel."""
    return [
        {"path": d.path, "card": d.card, "driver": d.driver,
         "bus_info": d.bus_info, "kind": d.kind}
        for d in enumerate_devices()
    ]
