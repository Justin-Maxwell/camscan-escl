"""Entry point: `camscan-escl` / `python -m camscan_escl`."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from . import config as config_mod
from . import capture, devices, discovery, preview
from .server import serve


def diagnose(cfg) -> int:
    """Say what is on this machine and what the config points at.

    Exists because the failure it was written for was invisible from every
    other angle: the unit was active, the journal said "preview streaming",
    and the only clue was a viewer reconnecting on a 31-second cycle. The
    device numbers had moved across a boot and the config still named the old
    ones, so the daemon was reading from its own loopback.
    """
    import urllib.error
    import urllib.request

    print("V4L2 devices present:")
    inventory = devices.inventory()
    if not inventory:
        print("  (none -- no /dev/video* nodes at all)")
    for entry in inventory:
        print(f"  {entry['path']:<14} {entry['card']!r} "
              f"[{entry['kind']}] driver={entry['driver']}")

    print("\nConfigured devices:")
    problems = 0
    for entry in config_mod.device_report(cfg):
        mark = "ok  " if entry["ok"] else "FAIL"
        target = entry["resolved"] or "unresolved"
        print(f"  {mark} {entry['name']:<9} {entry['spec']!r} -> {target}")
        if not entry["ok"]:
            problems += 1
            print(f"       {entry['detail']}")

    url = f"http://127.0.0.1:{cfg.server.port}/preview/status"
    print(f"\nDaemon at {url}:")
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = json.load(response)
    except urllib.error.URLError as exc:
        print(f"  not reachable: {exc.reason}")
        print("  start it with: systemctl --user start camscan-escl")
        return 1
    except (OSError, ValueError) as exc:
        print(f"  reachable but did not answer usefully: {exc}")
        return 1

    print(f"  {status['summary']}")
    print(f"  running={status['running']} healthy={status['healthy']} "
          f"frames={status['frames_seen']} pid={status['pid']}")
    for line in status.get("stderr_tail", []):
        print(f"  ffmpeg: {line}")
    return 1 if problems or not status["healthy"] else 0


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
    parser.add_argument("--diagnose", action="store_true",
                        help="report every V4L2 device, what each configured "
                             "device resolves to, and whether the daemon is "
                             "reachable; then exit")
    parser.add_argument("--print-camera", action="store_true",
                        help="print the resolved camera device path and exit, "
                             "for use in v4l2-ctl command lines")
    parser.add_argument("--print-loopback", action="store_true",
                        help="print the resolved loopback device path and "
                             "exit, for pointing a player at the preview")
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
        cfg = config_mod.load_adjustments(config_mod.load(args.config))
        config_mod.validate(cfg)
        # After the adjustments, so it judges the configuration that will
        # actually run rather than the file it was loaded from.
        config_mod.warn_about_geometry(cfg)
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

    if args.print_camera:
        try:
            print(capture.camera_path(cfg.capture))
        except devices.DeviceError as exc:
            print(f"camscan-escl: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.print_loopback:
        if not cfg.preview.loopback_device:
            print("camscan-escl: no preview.loopback_device configured",
                  file=sys.stderr)
            return 1
        try:
            print(devices.resolve(cfg.preview.loopback_device, "output"))
        except devices.DeviceError as exc:
            print(f"camscan-escl: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.diagnose:
        return diagnose(cfg)

    if args.focus_sweep:
        try:
            values = [int(v) for v in args.focus_sweep.replace(",", " ").split()]
        except ValueError:
            print("camscan-escl: --focus-sweep takes numbers, e.g. 0,20,40",
                  file=sys.stderr)
            return 2
        try:
            camera = capture.camera_path(cfg.capture)
        except devices.DeviceError as exc:
            print(f"camscan-escl: {exc}", file=sys.stderr)
            return 1
        ranked = capture.focus_sweep(cfg.capture, values)
        if not ranked:
            print("camscan-escl: no capture succeeded", file=sys.stderr)
            return 1
        print("\nsharpest first:")
        for value, score in ranked:
            print(f"  focus.absolute = {value:<4} sharpness {score:9.1f}")
        best = ranked[0][0]
        print(f"\nSharpest: absolute = {best}")
        print(
            "\nBefore pinning it, check autofocus is not already better --\n"
            "it is the default for good reason, and it picked the sharpest\n"
            "setting on the rig this was written for:\n"
            "  v4l2-ctl -d %s -c focus_automatic_continuous=1\n"
            "  # capture something, then:\n"
            "  v4l2-ctl -d %s -C focus_absolute\n"
            "Pin it only if autofocus hunts between pages."
            % (camera, camera)
        )
        print(
            "\nNote the scores are variance-of-Laplacian, which rewards noise\n"
            "as well as detail: a flat spread across the range means the\n"
            "differences are not really about focus. Look at the frames."
        )
        return 0

    # Resolved and logged before anything opens a device, so a numbering
    # change across a boot is visible in the journal at startup rather than
    # inferred hours later from a viewer's reconnect interval.
    for entry in config_mod.device_report(cfg):
        if entry["ok"]:
            logging.info("%s: %s -> %s (%s)", entry["name"], entry["spec"],
                         entry["resolved"], entry.get("card", "?"))
        else:
            logging.error("%s: %s could not be resolved -- %s",
                          entry["name"], entry["spec"], entry["detail"])

    # Before the preview opens the camera, not only before a capture. The
    # mains-frequency filter is what stops artificial light banding the
    # picture, and the picture you look at all day is the preview's.
    capture.apply_image(cfg.capture)

    stream = preview.PreviewStream(cfg)
    httpd = serve(cfg, stream)
    if not args.no_preview:
        # Wait for a real frame. A successful fork is not a working preview,
        # and saying so at startup is the difference between a journal that
        # records the fault and one that records a reassuring lie.
        if not stream.start(wait=8.0):
            logging.error("preview is not running: %s",
                          stream.status()["summary"])
            logging.error("the daemon will serve scans anyway; "
                          "run `camscan-escl --diagnose` for detail")
        else:
            logging.info("preview: %s", stream.status()["summary"])
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
