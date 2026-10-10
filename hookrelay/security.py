import hashlib
import hmac
import ipaddress
import secrets
import time
from urllib.parse import urlsplit


def generate_secret() -> str:
    return secrets.token_hex(32)


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify(secret: str, body: bytes, header: str | None, tolerance: int, now: int | None = None) -> bool:
    if not header or len(header) > 256:
        return False
    parts = header.split(",")
    if len(parts) != 2:
        return False
    values = {}
    for item in parts:
        if "=" not in item:
            return False
        k, v = item.strip().split("=", 1)
        if k in values:
            return False
        values[k] = v
    try:
        ts = int(values["t"])
        digest = values["v1"]
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > tolerance or len(digest) != 64:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(digest, expected)


def validate_destination_url(url: str, allow_private: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Invalid destination URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port  # Forces validation of malformed ports.
    except ValueError as exc:
        raise ValueError("Invalid destination URL") from exc
    if parsed.scheme not in ({"https", "http"} if allow_private else {"https"}):
        raise ValueError("Destination must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Destination URL contains unsupported components")
    if port is not None and port <= 0:
        raise ValueError("Invalid port")
    host = parsed.hostname.rstrip(".").lower()
    if not allow_private:
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Private destinations are not allowed")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not ip.is_global:
                raise ValueError("Private destinations are not allowed")
    return url
