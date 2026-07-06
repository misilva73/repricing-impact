#!/usr/bin/env python3
"""Local preview server for the static dashboard.

Serves the `site/` directory over `http.server`. No build step, no deps.

Usage:
    python scripts/serve.py            # serves on http://localhost:8000
    python scripts/serve.py --port 8123

Then open:
    http://localhost:8000/index.html
    http://localhost:8000/overview.html?schedule=eip-8038
    http://localhost:8000/transaction-failures.html?schedule=eip-8037
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
DEFAULT_PORT = 8000


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve site/ for local preview.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    if not SITE_DIR.is_dir():
        raise SystemExit(f"site dir not found: {SITE_DIR}")

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR)
    )
    # Allow quick restarts without "address already in use".
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.bind, args.port), handler) as httpd:
        print(f"Serving {SITE_DIR} at http://{args.bind}:{args.port}/")
        print("  http://%s:%d/index.html" % (args.bind, args.port))
        print("  http://%s:%d/overview.html?schedule=eip-8038" % (args.bind, args.port))
        print(
            "  http://%s:%d/transaction-failures.html?schedule=eip-8037"
            % (args.bind, args.port)
        )
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
