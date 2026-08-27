"""Entry point: `camscan-escl` / `python -m camscan_escl`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import config as config_mod
from . import discovery, preview
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="camscan-escl", description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=None,
                        help=f"TOML config (default: {config_mod.DEFAULT_CONFIG_PATH})")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--bind", help="override server.bind")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--no-discovery", action="store_true",
                        help="do not advertise over DNS-SD")
    parser.add_argument("--no-preview", action="store_true",
                        help="do not hold the camera for the live preview")
    parser.add_argument("--print-avahi-service", action="store_true",
                        help="print a static Avahi service file and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        cfg = config_mod.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"camscan-escl: config error: {exc}", file=sys.stderr)
        return 2

    if args.port or args.bind:
        from dataclasses import replace
        cfg = replace(cfg, server=replace(
            cfg.server,
            port=args.port or cfg.server.port,
            bind=args.bind or cfg.server.bind,
        ))

    if args.print_avahi_service:
        print(discovery.avahi_service_xml(cfg), end="")
        return 0

    stream = preview.PreviewStream(cfg)
    httpd = serve(cfg, stream)
    if not args.no_preview:
        stream.start()
    logging.info(
        "camscan-escl listening on http://%s:%d/eSCL (config: %s)",
        cfg.server.bind, cfg.server.port, cfg.source_path or "built-in defaults",
    )

    advertiser = None
    if cfg.discovery.enable and not args.no_discovery:
        advertiser = discovery.Advertiser(cfg)
        advertiser.start()

    # Turn SIGTERM into the same exception Ctrl-C raises, so the `finally`
    # below actually runs. Without this, `systemctl restart` kills the process
    # outright, the advertisement is never withdrawn, and the PTR lingers in
    # every cache on the link for its full 4500-second TTL. Those ghosts are
    # not cosmetic: a browsing client finds an instance that no longer answers
    # SRV or TXT, and `avahi-browse` reports "Failed to resolve ... Timeout".
    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        if advertiser is not None:
            advertiser.stop()
        stream.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
