"""Entry point: `camscan-escl` / `python -m camscan_escl`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import config as config_mod
from . import capture, discovery, preview
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
    parser.add_argument("--focus-sweep", nargs="?", const="0,5,10,20,40,80,160,255",
                        metavar="VALUES",
                        help="score focus settings on real captures and exit. "
                             "Run it with the rig in its final position and a "
                             "page underneath; the answer is only true for the "
                             "distance it was measured at.")
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

    if args.focus_sweep:
        try:
            values = [int(v) for v in args.focus_sweep.replace(",", " ").split()]
        except ValueError:
            print("camscan-escl: --focus-sweep takes numbers, e.g. 0,20,40",
                  file=sys.stderr)
            return 2
        ranked = capture.focus_sweep(cfg.capture, values)
        if not ranked:
            print("camscan-escl: no capture succeeded", file=sys.stderr)
            return 1
        print("\nsharpest first:")
        for value, score in ranked:
            print(f"  focus.absolute = {value:<4} sharpness {score:9.1f}")
        best = ranked[0][0]
        print(f"\nPut this in [capture.focus]:  absolute = {best}")
        if best == 0:
            print("0 is infinity on this camera: the subject is beyond its near\n"
                  "focus range, so move the camera closer rather than settling.")
        elif best == max(values):
            print(f"{best} is the closest this camera focuses, so the subject may\n"
                  "be nearer than it can manage. Try moving the camera back.")
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
