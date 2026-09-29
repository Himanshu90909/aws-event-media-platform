"""Minimal production container entrypoint for the existing stdlib API."""
from __future__ import annotations

import os
import sys
from http.server import HTTPServer

sys.path.insert(0, "/app")
from api.index import handler


class ContainerHandler(handler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ready","dependencies":"optional-local-or-aws"}')
            return
        super().do_GET()


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), ContainerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
