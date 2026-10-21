"""FIXTURE: servidor HTTP local que imita las 4 rutas de atomicmemory-core que usan las notas revisadas.

No es Atomic Memory real. Sirve a los tests y a la demo local:
    uv run python -m tests.fake_atomicmemory --port 4716
"""

import argparse
import json
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


class FakeAtomicMemory:
    def __init__(self, port: int = 0):
        self.memories: dict[str, dict[str, dict]] = {}  # user_id -> id -> memoria
        self.fail_status: int | None = None
        self.raw_body: bytes | None = None  # responde 200 con este cuerpo tal cual (respuesta malformada)
        self.delay_s = 0.0
        self.ingest_status = 200
        self.delete_body: dict | None = None
        self.redirect_to: str | None = None
        self.requests: list[tuple[str, str, dict | None]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeAtomicMemory":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _by_external(self, user: str, external_id: str) -> dict | None:
        return next((m for m in self.memories.get(user, {}).values() if m["metadata"].get("externalId") == external_id), None)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                url = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((method, url.path, body or qs))
                if fake.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fake.delay_s:
                    time.sleep(fake.delay_s)
                if fake.fail_status:
                    return self._send(fake.fail_status, {"error": "fixture: falla simulada"})
                if fake.raw_body is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(fake.raw_body)))
                    self.end_headers()
                    return self.wfile.write(fake.raw_body)
                parts = url.path.split("/")
                if method == "POST" and url.path == "/v1/memories/ingest/quick":
                    user, external = body["user_id"], body["metadata"]["externalId"]
                    now = datetime.now(UTC).isoformat()
                    found = fake._by_external(user, external)
                    if found:
                        found.update(content=body["conversation"], updated_at=now)
                        return self._send(fake.ingest_status, {"stored_memory_ids": [], "updated_memory_ids": [found["id"]]})
                    mem = {"id": str(uuid.uuid4()), "content": body["conversation"], "created_at": now,
                           "metadata": body["metadata"]}
                    fake.memories.setdefault(user, {})[mem["id"]] = mem
                    return self._send(fake.ingest_status, {"stored_memory_ids": [mem["id"]], "updated_memory_ids": []})
                if method == "GET" and url.path.startswith("/v1/memories/by-external-id/"):
                    found = fake._by_external(qs.get("user_id", ""), unquote(parts[-1]))
                    return self._send(200, found) if found else self._send(404, {"error": "Memory not found"})
                if method == "DELETE" and fake.delete_body is not None:
                    return self._send(200, fake.delete_body)
                if method == "DELETE" and len(parts) == 4 and parts[2] == "memories":
                    if fake.memories.get(qs.get("user_id", ""), {}).pop(unquote(parts[3]), None):
                        return self._send(200, {"success": True})
                    return self._send(404, {"error": "Memory not found"})
                self._send(404, {"error": "fixture: ruta no implementada"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4716)
    fake = FakeAtomicMemory(parser.parse_args().port)
    print(f"FIXTURE Atomic Memory (no es el servicio real) en {fake.url}", flush=True)
    fake.server.serve_forever()
