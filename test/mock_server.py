#!/usr/bin/env python3
"""Serve fixture php-bin releases in the GitHub API shape the plugin reads.

releases.json in the asset folder lists the releases in API order, newest
published first, as [{"tag": "8.4.99", "draft": false}, ...]. A release lists
the archive for each platform that has a file in the asset folder, named
php-<tag>-cli-<platform>.tar.gz, and its own SHA256SUMS when
sums/<tag>/SHA256SUMS exists, served at /assets/<tag>/SHA256SUMS.

The release listing pages like GitHub's: per_page (default 30, at most 100)
and page (default 1). Every request is appended to requests.log in the asset
folder as "<path and query> auth=<yes|no>", so tests can count API calls and prove where
an Authorization header went without the server ever recording its value.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


PORT = int(sys.argv[1])
ASSET_DIR = Path(sys.argv[2]).resolve()
RELEASES_PATH = "/repos/bigpixelrocket/php-bin/releases"
PLATFORMS = ("macos-aarch64", "linux-x86_64", "linux-aarch64")


def archive_name(tag: str, platform: str) -> str:
    return f"php-{tag}-cli-{platform}.tar.gz"


def releases() -> list:
    return json.loads((ASSET_DIR / "releases.json").read_text())


def release_files(tag: str) -> dict:
    """Map each asset a release carries to its file in the asset folder."""
    files = {}
    for platform in PLATFORMS:
        name = archive_name(tag, platform)
        if (ASSET_DIR / name).is_file():
            files[name] = (name, ASSET_DIR / name)
    sums = ASSET_DIR / "sums" / tag / "SHA256SUMS"
    if sums.is_file():
        files["SHA256SUMS"] = (f"{tag}/SHA256SUMS", sums)
    return files


def release_payload(release: dict) -> dict:
    base_url = f"http://127.0.0.1:{PORT}/assets"
    tag = release["tag"]
    return {
        "tag_name": tag,
        "draft": release.get("draft", False),
        "prerelease": False,
        "assets": [
            {"name": name, "browser_download_url": f"{base_url}/{path}"}
            for name, (path, _) in release_files(tag).items()
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        if path != "/health":
            auth = "yes" if self.headers.get("Authorization") else "no"
            with (ASSET_DIR / "requests.log").open("a") as log:
                log.write(f"{self.path} auth={auth}\n")

        if path == "/health":
            self.send_bytes(b"ok\n", "text/plain")
            return

        if path == RELEASES_PATH:
            query = parse_qs(url.query)
            per_page = min(int(query.get("per_page", ["30"])[0]), 100)
            page = int(query.get("page", ["1"])[0])
            listed = releases()[(page - 1) * per_page : page * per_page]
            self.send_json([release_payload(release) for release in listed])
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
            requested = path[len(asset_prefix) :]
            for release in releases():
                for served, file in release_files(release["tag"]).values():
                    if served == requested:
                        content_type = "application/gzip" if served.endswith(".tar.gz") else "text/plain"
                        self.send_bytes(file.read_bytes(), content_type)
                        return
            self.send_error(404)
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
