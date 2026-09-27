#!/usr/bin/env python3
"""Serve fixture php-bin releases in the GitHub API shape the plugin reads.

releases.json in the asset folder lists the releases in API order, newest
published first, as [{"tag": "8.4.99", "draft": false}, ...]. Every archive is
served as php-<tag>-cli-macos-aarch64.tar.gz beside one shared SHA256SUMS.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PORT = int(sys.argv[1])
ASSET_DIR = Path(sys.argv[2]).resolve()
RELEASES_PATH = "/repos/bigpixelrocket/php-bin/releases"


def archive_name(tag: str) -> str:
    return f"php-{tag}-cli-macos-aarch64.tar.gz"


def releases() -> list:
    return json.loads((ASSET_DIR / "releases.json").read_text())


def release_payload(release: dict) -> dict:
    base_url = f"http://127.0.0.1:{PORT}/assets"
    tag = release["tag"]
    return {
        "tag_name": tag,
        "draft": release.get("draft", False),
        "prerelease": False,
        "assets": [
            {
                "name": archive_name(tag),
                "browser_download_url": f"{base_url}/{archive_name(tag)}",
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

        if path == RELEASES_PATH:
            self.send_json([release_payload(release) for release in releases()])
            return

        tag_prefix = f"{RELEASES_PATH}/tags/"
        if path.startswith(tag_prefix):
            tag = path[len(tag_prefix) :]
            for release in releases():
                if release["tag"] == tag:
                    self.send_json(release_payload(release))
                    return
            self.send_error(404)
            return

        asset_prefix = "/assets/"
        if path.startswith(asset_prefix):
            name = path[len(asset_prefix) :]
            known = {archive_name(release["tag"]) for release in releases()} | {"SHA256SUMS"}
            asset = ASSET_DIR / name
            if name not in known or not asset.is_file():
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
