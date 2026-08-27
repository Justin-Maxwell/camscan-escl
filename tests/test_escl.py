"""ScanSettings parsing and the two generated documents."""

import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace

from camscan_escl import discovery, escl
from camscan_escl.config import Config

SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{pwg}" xmlns:scan="{scan}">
  <pwg:Version>2.6</pwg:Version>
  <scan:Intent>Document</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>2480</pwg:Width>
      <pwg:Height>3508</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <scan:ColorMode>Grayscale8</scan:ColorMode>
  <scan:XResolution>150</scan:XResolution>
  <scan:YResolution>150</scan:YResolution>
  <pwg:InputSource>Platen</pwg:InputSource>
</scan:ScanSettings>
""".format(pwg=escl.PWG, scan=escl.SCAN).encode()


def test_parses_a_full_request():
    s = escl.parse_scan_settings(SETTINGS, 150)
    assert (s.region.x, s.region.y) == (0, 0)
    assert (s.region.width, s.region.height) == escl.A4
    assert s.color_mode == "Grayscale8"
    assert s.expected_size == (1240, 1754)


def test_empty_body_falls_back_to_a4_at_declared_dpi():
    s = escl.parse_scan_settings(b"", 150)
    assert (s.region.width, s.region.height) == escl.A4
    assert (s.x_resolution, s.y_resolution) == (150, 150)
    assert s.color_mode == "RGB24"


def test_unknown_colour_mode_falls_back_to_rgb():
    body = SETTINGS.replace(b"Grayscale8", b"BlackAndWhite1")
    assert escl.parse_scan_settings(body, 150).color_mode == "RGB24"


def test_region_in_the_scan_namespace_is_also_accepted():
    body = SETTINGS.replace(b"pwg:ScanRegion", b"scan:ScanRegion").replace(
        b"pwg:XOffset", b"scan:XOffset"
    )
    s = escl.parse_scan_settings(body, 150)
    assert (s.region.width, s.region.height) == escl.A4


def test_units_are_three_hundredths_of_an_inch():
    # A4 is 210 x 297 mm; round-tripping the units must agree to <0.5 mm.
    region = escl.ScanRegion(0, 0, *escl.A4)
    _, _, w_mm, h_mm = region.mm
    assert abs(w_mm - 210.0) < 0.5
    assert abs(h_mm - 297.0) < 0.5


def test_capabilities_is_well_formed_with_one_resolution():
    root = ET.fromstring(escl.capabilities_xml("camscan-escl (C920)", "camscan-0001", 150))
    assert root.tag == f"{{{escl.SCAN}}}ScannerCapabilities"
    res = root.findall(".//scan:DiscreteResolution", escl.NS)
    assert len(res) == 1
    assert res[0].find("scan:XResolution", escl.NS).text == "150"
    modes = [e.text for e in root.findall(".//scan:ColorMode", escl.NS)]
    assert modes == ["RGB24", "Grayscale8"]


def test_declared_platen_contains_both_a4_and_letter():
    # Regression: MaxWidth=2480 (A4) makes sane-airscan clamp a Letter request
    # to 1240px where the contract says 1275. Observed on real hardware.
    root = ET.fromstring(escl.capabilities_xml("m", "s", 150))
    max_w = int(root.find(".//scan:MaxWidth", escl.NS).text)
    max_h = int(root.find(".//scan:MaxHeight", escl.NS).text)
    assert max_w >= max(escl.A4[0], escl.LETTER[0])
    assert max_h >= max(escl.A4[1], escl.LETTER[1])


def test_capabilities_carries_a_uuid_matching_the_advert():
    # Regression: NAPS2's ManualIpForm creates no device, and says nothing at
    # all, when caps.Uuid is null. Verified by packet capture against 8.3.2 --
    # the request succeeds and the dialog just sits there.
    root = ET.fromstring(escl.capabilities_xml("m", "camscan-0001", 150))
    el = root.find("scan:UUID", escl.NS)
    assert el is not None and el.text
    uuid.UUID(el.text)  # a syntactically valid UUID, not just any string

    cfg = replace(Config(), scanner=replace(Config().scanner, serial="camscan-0001"))
    assert discovery.txt_records(cfg)["uuid"] == el.text


def test_status_is_well_formed():
    root = ET.fromstring(escl.status_xml("Idle"))
    assert root.find("pwg:State", escl.NS).text == "Idle"


def test_platen_admits_landscape_as_well_as_portrait():
    # eSCL has no orientation flag: a landscape scan IS a region wider than
    # it is tall. NAPS2 can define a custom page size, so a landscape A4 asks
    # for 3508 units of width -- and against a declared 2550 the client
    # clamps and returns 1275px where the contract says 1754. Measured.
    root = ET.fromstring(escl.capabilities_xml("m", "s", 150))
    max_w = int(root.find(".//scan:MaxWidth", escl.NS).text)
    max_h = int(root.find(".//scan:MaxHeight", escl.NS).text)
    longest = max(*escl.A4, *escl.LETTER)
    assert max_w >= longest, "cannot request a landscape page"
    assert max_h >= longest
