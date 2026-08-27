"""eSCL XML: capabilities, status, and ScanSettings parsing.

WARNING (spec §6): the capabilities document below is written from the spec's
skeleton, which is itself from recollection and has NOT been validated against
the Mopria eSCL Scan Technical Specification. Validating it — against the spec
and against AirSane's generated XML — is the first task, not an afterthought.
"""

from __future__ import annotations

import uuid as _uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

PWG = "http://www.pwg.org/schemas/2010/12/sm"
SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"

NS = {"pwg": PWG, "scan": SCAN}

# eSCL geometry is always in units of 1/300 inch, whatever the resolution.
UNITS_PER_INCH = 300
MM_PER_INCH = 25.4

A4 = (2480, 3508)
LETTER = (2550, 3300)

# The declared platen must contain every paper size the client may ask for:
# Letter is wider than A4, A4 is taller than Letter. The spec's skeleton
# declares A4's 2480 as MaxWidth, and sane-airscan then silently clamps a
# Letter request to 2480 units -- a 1240px-wide page where 1275 was
# contracted. Regions past the camera's real coverage pad white anyway, so
# declaring the union costs nothing and honours the contract for both.
#
# Both orientations, not just portrait. NAPS2 can define a custom page size,
# and a landscape A4 asks for 3508 units of width -- against a declared 2550
# the client clamps and returns 1275px where the contract says 1754, silently.
# Measured. eSCL has no orientation flag at all: a landscape scan IS a region
# wider than it is tall, so the platen has to admit one.
_SIDES = (*A4, *LETTER)
MAX_REGION = (max(_SIDES), max(_SIDES))

COLOR_MODES = ("RGB24", "Grayscale8")


def device_uuid(serial: str) -> str:
    """A stable UUID derived from the serial, so it survives restarts.

    Both the capabilities document and the DNS-SD TXT record must carry the
    same value: a client that finds the device twice, once by search and once
    by Manual IP, has to recognise it as one device.
    """
    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"camscan-escl.{serial}"))


def _units_to_mm(units: float) -> float:
    return units * MM_PER_INCH / UNITS_PER_INCH


@dataclass(frozen=True)
class ScanRegion:
    x: int
    y: int
    width: int
    height: int

    @property
    def mm(self) -> tuple[float, float, float, float]:
        return (
            _units_to_mm(self.x),
            _units_to_mm(self.y),
            _units_to_mm(self.width),
            _units_to_mm(self.height),
        )


@dataclass(frozen=True)
class ScanSettings:
    region: ScanRegion
    x_resolution: int
    y_resolution: int
    color_mode: str

    @property
    def expected_size(self) -> tuple[int, int]:
        """The dimension contract of spec §5. The JPEG must match exactly."""
        return (
            max(1, round(self.region.width * self.x_resolution / UNITS_PER_INCH)),
            max(1, round(self.region.height * self.y_resolution / UNITS_PER_INCH)),
        )


def _find_int(root: ET.Element, *paths: str) -> int | None:
    for path in paths:
        el = root.find(path, NS)
        if el is not None and el.text and el.text.strip():
            try:
                return int(float(el.text.strip()))
            except ValueError:
                continue
    return None


def parse_scan_settings(body: bytes, default_dpi: int) -> ScanSettings:
    """Parse a scan:ScanSettings document, filling omitted values with A4.

    Clients vary in which elements they send and in whether geometry sits in
    the pwg or scan namespace, so every lookup tries both.
    """
    root = ET.fromstring(body) if body.strip() else ET.Element("empty")

    x_res = _find_int(root, "scan:XResolution", "pwg:XResolution") or default_dpi
    y_res = _find_int(root, "scan:YResolution", "pwg:YResolution") or x_res

    region_el = None
    for path in (
        "pwg:ScanRegions/pwg:ScanRegion",
        "scan:ScanRegions/scan:ScanRegion",
        "pwg:ScanRegions/scan:ScanRegion",
        "scan:ScanRegions/pwg:ScanRegion",
    ):
        region_el = root.find(path, NS)
        if region_el is not None:
            break

    if region_el is None:
        region = ScanRegion(0, 0, A4[0], A4[1])
    else:
        region = ScanRegion(
            x=_find_int(region_el, "pwg:XOffset", "scan:XOffset") or 0,
            y=_find_int(region_el, "pwg:YOffset", "scan:YOffset") or 0,
            width=_find_int(region_el, "pwg:Width", "scan:Width") or A4[0],
            height=_find_int(region_el, "pwg:Height", "scan:Height") or A4[1],
        )

    # `or` is wrong for Elements: a childless Element is falsy even when found.
    mode_el = root.find("scan:ColorMode", NS)
    if mode_el is None:
        mode_el = root.find("pwg:ColorMode", NS)
    color_mode = (mode_el.text or "").strip() if mode_el is not None else ""
    if color_mode not in COLOR_MODES:
        color_mode = "RGB24"

    return ScanSettings(region, x_res, y_res, color_mode)


def capabilities_xml(make_and_model: str, serial: str, dpi: int) -> bytes:
    """ScannerCapabilities. Declares exactly one discrete resolution (§6).

    scan:UUID is not decoration. NAPS2's ManualIpForm gates device creation on
    `caps.Uuid != null && caps.MakeAndModel != null`; with a null UUID it
    creates no device, reports no error, and the dialog simply does not
    advance. Confirmed from a packet capture against NAPS2 8.3.2: 200 OK, the
    whole document read, socket left open, nothing wrong at any layer below.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities
    xmlns:pwg="{PWG}"
    xmlns:scan="{SCAN}">
  <pwg:Version>2.6</pwg:Version>
  <pwg:MakeAndModel>{_esc(make_and_model)}</pwg:MakeAndModel>
  <pwg:SerialNumber>{_esc(serial)}</pwg:SerialNumber>
  <scan:UUID>{device_uuid(serial)}</scan:UUID>
  <scan:Platen>
    <scan:PlatenInputCaps>
      <scan:MinWidth>16</scan:MinWidth>
      <scan:MinHeight>16</scan:MinHeight>
      <scan:MaxWidth>{MAX_REGION[0]}</scan:MaxWidth>
      <scan:MaxHeight>{MAX_REGION[1]}</scan:MaxHeight>
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
                <scan:XResolution>{dpi}</scan:XResolution>
                <scan:YResolution>{dpi}</scan:YResolution>
              </scan:DiscreteResolution>
            </scan:DiscreteResolutions>
          </scan:SupportedResolutions>
        </scan:SettingProfile>
      </scan:SettingProfiles>
    </scan:PlatenInputCaps>
  </scan:Platen>
</scan:ScannerCapabilities>
""".encode()


def status_xml(state: str) -> bytes:
    """ScannerStatus. Never Down on a transient failure (§7)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus
    xmlns:pwg="{PWG}"
    xmlns:scan="{SCAN}">
  <pwg:Version>2.6</pwg:Version>
  <pwg:State>{state}</pwg:State>
</scan:ScannerStatus>
""".encode()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
