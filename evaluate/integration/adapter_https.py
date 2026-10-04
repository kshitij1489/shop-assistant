"""TLS reverse proxy so evaluation payment workers exercise real HTTPS.

The website stays on plain HTTP inside the compose network. This process
terminates TLS on the development network and forwards only `/commerce/` requests.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_BODY = 1_000_000
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


def ensure_certs(directory: Path) -> tuple[Path, Path, Path]:
    """Create a short-lived local CA and a server cert if they are not already present."""
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = directory / "ca.pem", directory / "ca.key"
    cert, key = directory / "cert.pem", directory / "key.pem"
    if ca_cert.is_file() and cert.is_file() and key.is_file():
        return ca_cert, cert, key
    _openssl([
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
        "-out", str(ca_cert), "-days", "30", "-subj", "/CN=studio-eval-local-ca",
    ])
    csr = directory / "cert.csr"
    _openssl([
        "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
        "-out", str(csr), "-subj", "/CN=localhost",
    ])
    san = directory / "san.cnf"
    san.write_text(
        "subjectAltName=DNS:localhost,DNS:adapter_https,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    _openssl([
        "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(cert), "-days", "30", "-extfile", str(san),
    ])
    csr.unlink(missing_ok=True)
    os.chmod(ca_key, 0o600)
    os.chmod(key, 0o600)
    return ca_cert, cert, key


def _openssl(args: list[str]) -> None:
    try:
        subprocess.run(["openssl", *args], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("openssl could not create the local evaluation certificate") from exc


def validated_upstream(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").casefold()
    allowed = {"127.0.0.1", "localhost", "::1", "web"}
    if (parsed.scheme != "http" or host not in allowed or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise SystemExit("adapter upstream must be an http origin for the evaluation website")
    return url.rstrip("/")


class _CommerceProxy(BaseHTTPRequestHandler):
    upstream = "http://127.0.0.1:8000"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self):  # noqa: N802 - stdlib handler name
        if self.path == "/health":
            payload = b'{"service":"evaluation-adapter"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._forward()

    def do_POST(self):  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/commerce/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > MAX_BODY:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        headers["Host"] = "127.0.0.1"
        request = Request(self.upstream + self.path, data=body, method=self.command, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed evaluation upstream
                payload = response.read(MAX_BODY + 1)
                status = response.status
                forwarded = response.headers
        except HTTPError as exc:
            payload = exc.read(MAX_BODY + 1)
            status = exc.code
            forwarded = exc.headers
        except (URLError, OSError, ValueError):
            self.send_error(502)
            return
        if len(payload) > MAX_BODY:
            self.send_error(502)
            return
        self.send_response(status)
        content_type = forwarded.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(listen: str, port: int, upstream: str, cert_dir: Path) -> None:
    _ca, cert, key = ensure_certs(cert_dir)
    handler = type("BoundProxy", (_CommerceProxy,), {"upstream": validated_upstream(upstream)})
    server = ThreadingHTTPServer((listen, port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluation commerce HTTPS proxy")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--upstream", default="http://127.0.0.1:8000")
    parser.add_argument("--cert-dir", type=Path, default=Path("/tmp/studio-eval-certs"))
    args = parser.parse_args(argv)
    if args.listen not in {"0.0.0.0", "127.0.0.1", "::"}:
        raise SystemExit("adapter proxy listens only on loopback or the compose bridge")
    serve(args.listen, args.port, args.upstream, args.cert_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
