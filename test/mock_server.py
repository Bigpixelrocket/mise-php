#!/usr/bin/env python3

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PORT = int(sys.argv[1])
ASSET_DIR = Path(sys.argv[2]).resolve()
VERSION = "8.4.99"
ARCHIVE_NAME = f"php-{VERSION}-cli-macos-aarch64.tar.gz"


def release_payload() -> dict:
    base_url = f"http://127.0.0.1:{PORT}/assets"
    return {
        "tag_name": VERSION,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": ARCHIVE_NAME,
                "browser_download_url": f"{base_url}/{ARCHIVE_NAME}",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": f"{base_url}/SHA256SUMS",
            },
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/health":
            self.send_bytes(b"ok\n", "text/plain")
            return

        if path == "/repos/bigpixelrocket/php-bin/releases":
            self.send_json([release_payload()])
            return

        if path == f"/repos/bigpixelrocket/php-bin/releases/tags/{VERSION}":
            self.send_json(release_payload())
            return

        asset_prefix = "/assets/"
        if path.startswith(asset_prefix):
            name = path[len(asset_prefix) :]
            if name not in {ARCHIVE_NAME, "SHA256SUMS"}:
                self.send_error(404)
                return

            asset = ASSET_DIR / name
            if not asset.is_file():
                self.send_error(404)
                return

            content_type = "application/gzip" if name.endswith(".tar.gz") else "text/plain"
            self.send_bytes(asset.read_bytes(), content_type)
            return

        self.send_error(404)

    def send_json(self, payload: object) -> None:
        self.send_bytes(json.dumps(payload).encode(), "application/json")

    def send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

