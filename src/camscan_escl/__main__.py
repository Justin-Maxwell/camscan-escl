"""Entry point: `camscan-escl` / `python -m camscan_escl`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config as config_mod
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="camscan-escl", description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=None,
                        help=f"TOML config (default: {config_mod.DEFAULT_CONFIG_PATH})")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--bind", help="override server.bind")
    parser.add_argument("-v", "--verbose", action="store_true")
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

    httpd = serve(cfg)
    logging.info(
        "camscan-escl listening on http://%s:%d/eSCL (config: %s)",
        cfg.server.bind, cfg.server.port, cfg.source_path or "built-in defaults",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
