from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ArchiveHandler(BaseHTTPRequestHandler):
    body: bytes = b""
    bucket: str = "evidence"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        prefix = f"/{self.bucket}/"
        if not self.path.split("?", 1)[0].startswith(prefix):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {f"/{self.bucket}", f"/{self.bucket}/"}:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        prefix = f"/{self.bucket}/"
        if not path.startswith(prefix):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="prototype-only fake archive")
    parser.add_argument("--file", required=True)
    parser.add_argument("--bucket", default="evidence")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    arguments = parser.parse_args()
    body = Path(arguments.file).read_bytes()
    handler = type(
        "ConfiguredArchiveHandler",
        (ArchiveHandler,),
        {"body": body, "bucket": arguments.bucket},
    )
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
