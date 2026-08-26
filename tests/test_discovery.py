"""DNS-SD advertisement content (spec §10).

The advertisement itself is verified against avahi-browse by hand; what is
worth pinning here is the TXT set, since a client silently ignores a device
whose records it does not like.
"""

import xml.etree.ElementTree as ET

from camscan_escl import discovery
from camscan_escl.config import Config, ScannerConfig, ServerConfig


def cfg(bind="127.0.0.1", port=8090):
    return Config(server=ServerConfig(port=port, bind=bind),
                  scanner=ScannerConfig(serial="camscan-0001"))


def test_required_txt_records_are_present():
    txt = discovery.txt_records(cfg())
    assert txt["rs"] == "eSCL"          # the base path; without it, clients skip us
    assert txt["pdl"] == "image/jpeg"
    assert txt["cs"] == "color,grayscale"
    assert txt["vers"] == "2.6"
    assert txt["is"] == "platen"
    assert txt["ty"]
    assert "representation" in txt


def test_rs_has_no_leading_slash():
    # A leading slash yields a double slash in the URL clients build.
    assert not discovery.txt_records(cfg())["rs"].startswith("/")


def test_uuid_is_stable_across_restarts():
    assert discovery.txt_records(cfg())["uuid"] == discovery.txt_records(cfg())["uuid"]


def test_uuid_differs_per_serial():
    other = Config(scanner=ScannerConfig(serial="camscan-0002"))
    assert discovery.txt_records(cfg())["uuid"] != discovery.txt_records(other)["uuid"]


def test_wildcard_bind_advertises_a_routable_address():
    host = discovery._advertised_host(cfg(bind="0.0.0.0"))
    assert host != "0.0.0.0"


def test_avahi_service_file_is_well_formed_xml():
    root = ET.fromstring(discovery.avahi_service_xml(cfg(port=9000)))
    assert root.tag == "service-group"
    service = root.find("service")
    assert service.find("type").text == "_uscan._tcp"
    assert service.find("port").text == "9000"
    records = {r.text.split("=", 1)[0] for r in service.findall("txt-record")}
    assert {"rs", "ty", "pdl", "cs", "vers"} <= records
