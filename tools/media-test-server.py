#!/usr/bin/env python3
"""Тестовый PNG и обработчик изображений/JSON. Только stdlib, без камеры и внешних API."""
import argparse
import json
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    rows = bytearray()
    for y in range(64):
        rows.append(0)
        for x in range(64):
            rows.extend((70, 180, 130) if (x // 8 + y // 8) % 2 else (40, 60, 90))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


PNG = test_png()
LIMIT = 2 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, raw, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/image.png":
            self.reply(200, PNG, "image/png")
        else:
            self.reply(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path != "/process":
            self.reply(404, b'{"error":"not found"}')
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= LIMIT:
                self.reply(413, b'{"error":"body size"}')
                return
            raw = self.rfile.read(size)
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0]
            if ctype == "application/json":
                result = {"ok": True, "value": 42, "echo": json.loads(raw)}
            elif ctype in ("image/png", "image/jpeg"):
                result = {"ok": True, "value": 42, "bytes": len(raw), "content_type": ctype}
            else:
                self.reply(415, b'{"error":"content type"}')
                return
            self.reply(200, json.dumps(result).encode())
        except (ValueError, UnicodeDecodeError):
            self.reply(400, b'{"error":"invalid body"}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(f"Тестовый сервер: http://{args.host}:{server.server_port}/image.png → /process", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
