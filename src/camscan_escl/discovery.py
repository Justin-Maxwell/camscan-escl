"""DNS-SD advertisement of the scanner as _uscan._tcp (spec §10).

In-process rather than a static Avahi service file, so the advertisement
cannot outlive the daemon: a front-end that finds the device can always
reach it. `--print-avahi-service` emits the static-file alternative for
anyone who wants the other trade-off.

`zeroconf` is an optional dependency. Without it the daemon runs exactly as
before and says so once; Manual IP still works.
"""

from __future__ import annotations

import logging
import socket

from .config import Config
from .escl import device_uuid

log = logging.getLogger(__name__)

SERVICE_TYPE = "_uscan._tcp.local."


def txt_records(cfg: Config) -> dict[str, str]:
    """The TXT set a client uses to decide the device is a usable eSCL scanner."""
    return {
        "txtvers": "1",
        "vers": "2.6",
        "rs": "eSCL",                     # the base path, without a leading /
        "ty": cfg.scanner.make_and_model,
        "note": "camscan-escl",
        "pdl": "image/jpeg",
        "cs": "color,grayscale",
        "is": "platen",
        "duplex": "F",
        "representation": "",
        "uuid": _uuid(cfg),
        "adminurl": f"http://{_advertised_host(cfg)}:{cfg.server.port}/eSCL",
    }


def _uuid(cfg: Config) -> str:
    """The same UUID the capabilities document reports. One source, not two."""
    return device_uuid(cfg.scanner.serial)


def _advertised_host(cfg: Config) -> str:
    if cfg.server.bind in ("0.0.0.0", "::", ""):
        return _primary_address()
    return cfg.server.bind


def _primary_address() -> str:
    """Best guess at the address other hosts would use to reach us."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, sends nothing
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Advertiser:
    """Publishes the service for the lifetime of the daemon."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._zc = None
        self._info = None

    def start(self) -> bool:
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError:
            log.warning(
                "zeroconf is not installed, so the scanner will not appear in a "
                "front-end's device search. Install it (pip install zeroconf), or "
                "use Manual IP, or run --print-avahi-service."
            )
            return False

        # IPVersion.All means zeroconf also opens a ::1 socket, and on a host
        # with no loopback IPv6 route every send on it logs a warning that
        # says nothing about whether the advert works -- it registers fine
        # regardless. Drop that one message rather than the whole logger, so
        # a real zeroconf failure still surfaces.
        logging.getLogger("zeroconf").addFilter(
            lambda record: "Network is unreachable" not in record.getMessage()
        )

        cfg = self._cfg
        address = _advertised_host(cfg)
        instance = cfg.discovery.name

        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(address)],
            port=cfg.server.port,
            properties=txt_records(cfg),
            server=f"{socket.gethostname().split('.')[0]}-camscan.local.",
        )

        try:
            # IPVersion.All, not the default. zeroconf defaults to V4Only, so
            # it neither listens on ff02::fb nor answers there. NAPS2 browses
            # over both families -- a packet capture shows it querying
            # _uscan._tcp from a link-local address every two seconds, and
            # nothing on this host ever answering IPv6. Responding on both
            # costs nothing and removes the only asymmetry the capture shows.
            self._zc = Zeroconf(ip_version=IPVersion.All)
            self._zc.register_service(self._info, allow_name_change=True)
        except Exception as exc:
            log.warning("could not advertise over DNS-SD: %s", exc)
            self.stop()
            return False

        log.info("advertising %s as %s on %s:%d", SERVICE_TYPE, instance, address, cfg.server.port)
        if address.startswith("127."):
            log.warning(
                "bind is loopback, so the advertised address is %s and only this "
                "host can use it. Widen server.bind to scan from elsewhere.",
                address,
            )
        return True

    def stop(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
            except Exception:
                pass
            finally:
                self._zc.close()
                self._zc = None


def avahi_service_xml(cfg: Config) -> str:
    """The static-file alternative, for /etc/avahi/services/camscan-escl.service.

    Survives daemon restarts, at the cost of advertising a scanner that may
    not be running -- which is exactly the 'Connection refused' failure.
    """
    txt = txt_records(cfg)
    lines = "\n".join(
        f"    <txt-record>{k}={_esc(v)}</txt-record>" for k, v in txt.items()
    )
    return f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- camscan-escl: install as /etc/avahi/services/camscan-escl.service -->
<service-group>
  <name>{_esc(cfg.discovery.name)}</name>
  <service>
    <type>_uscan._tcp</type>
    <port>{cfg.server.port}</port>
{lines}
  </service>
</service-group>
"""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
