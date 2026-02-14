#!/usr/bin/env python3
"""Serves the snapshot page. Proxies /snapshot.jpg from ../camera/snapshot.jpg."""

import http.server
import os

PORT = 8080
WWW_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(WWW_DIR, "..", "camera", "snapshot.jpg")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/snapshot.jpg"):
            try:
                with open(SNAPSHOT_PATH, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", len(data))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_error(404, "No snapshot yet")
        else:
            super().do_GET()


if __name__ == "__main__":
    print(f"Serving on http://0.0.0.0:{PORT}")
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
