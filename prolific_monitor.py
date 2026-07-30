#!/usr/bin/env python3
"""
Lightweight Prolific study monitor with Telegram notifications.

The monitor is intentionally configured from a browser-captured cURL request so
it can mirror Prolific's current frontend request without hardcoding internals.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import gzip
import random
import re
import queue
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG = "prolific-monitor.json"
DEFAULT_STATE = "state/prolific-state.json"
DEFAULT_MANUAL_ONLY_NOTIFICATION_STATE = "state/manual-only-notifications.json"
ENV_FILE = ".env"
DEFAULT_BROWSER_PORT = 9222
DEFAULT_BROWSER_PROFILE = ".browser-profile"
PROLIFIC_STUDIES_URL_PART = "internal-api.prolific.com/api/v1/participant/studies"

# To disable this rule, comment the True line and uncomment the False line.
MANUAL_ONLY_STUDY_RULE_ENABLED = True
# MANUAL_ONLY_STUDY_RULE_ENABLED = False

STATE_LOCK = threading.RLock()


def resolve_log_timezone() -> datetime.tzinfo:
    """Use the requested IANA zone when available, otherwise fall back safely."""
    timezone_name = os.environ.get("PROLIFIC_LOG_TIMEZONE", "Asia/Riyadh")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        # Some Windows Python installs do not ship tzdata. Fall back to the
        # machine's local timezone so the monitor can still run.
        fallback = datetime.now().astimezone().tzinfo
        if fallback is None:
            return ZoneInfo("UTC")
        return fallback


LOCAL_TIMEZONE = resolve_log_timezone()


class MonitorError(Exception):
    """Expected runtime error that can be shown without a traceback."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def utc_now() -> str:
    return datetime.now(LOCAL_TIMEZONE).replace(microsecond=0).isoformat()


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


def load_dotenv(path: Path = Path(ENV_FILE)) -> None:
    """Load simple KEY=VALUE entries, allowing .env token rotation at runtime."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def update_env_var(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            updated.append(f"{key}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.environ[key] = value


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            replacement = os.environ.get(name)
            if replacement is None:
                raise MonitorError(f"Missing required environment variable: {name}")
            return replacement

        return pattern.sub(replace, value)
    if isinstance(value, dict):
        return {key: expand_env(val) for key, val in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MonitorError(f"Config file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    last_error: PermissionError | None = None

    for attempt in range(8):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(min(1.0, 0.05 * (2**attempt)) + random.uniform(0, 0.05))

    raise MonitorError(f"Could not write {path}; another process is still locking it: {last_error}")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_ids": [], "initialized": False, "last_checked_at": None, "last_error": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        path.replace(backup)
        log(f"State file was corrupt; moved it to {backup}")
        return {"seen_ids": [], "last_checked_at": None, "last_error": None}
    state.setdefault("seen_ids", [])
    state.setdefault("initialized", False)
    state.setdefault("last_checked_at", None)
    state.setdefault("last_error", None)
    return state


def normalize_curl(curl_text: str) -> str:
    normalized = curl_text.replace("\\\r\n", " ").replace("\\\n", " ")
    normalized = normalized.replace("^\r\n", " ").replace("^\n", " ")
    normalized = normalized.replace("`\r\n", " ").replace("`\n", " ")
    normalized = normalized.replace("^", "")
    return normalized.strip()


def parse_curl(curl_text: str) -> dict[str, Any]:
    tokens = shlex.split(normalize_curl(curl_text), posix=True)
    if not tokens or Path(tokens[0]).name.lower() not in {"curl", "curl.exe"}:
        raise MonitorError("The captured request must start with curl or curl.exe")

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    body: str | None = None
    dropped_headers: list[str] = []

    i = 1
    while i < len(tokens):
        token = tokens[i]
        next_value = tokens[i + 1] if i + 1 < len(tokens) else None

        if token in {"-H", "--header"} and next_value is not None:
            if ":" not in next_value:
                raise MonitorError(f"Header is missing ':' separator: {next_value}")
            name, value = next_value.split(":", 1)
            name = name.strip()
            value = value.strip()
            lower_name = name.lower()
            if lower_name == "authorization":
                headers[name] = "Bearer ${PROLIFIC_BEARER_TOKEN}"
            elif lower_name in {"cookie", "accept-encoding"}:
                dropped_headers.append(name)
            else:
                headers[name] = value
            i += 2
            continue

        if token in {"-X", "--request"} and next_value is not None:
            method = next_value.upper()
            i += 2
            continue

        if token == "--url" and next_value is not None:
            url = next_value
            i += 2
            continue

        if token in {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii"} and next_value is not None:
            body = next_value
            i += 2
            continue

        if not token.startswith("-") and url is None:
            url = token

        if token in {"--compressed", "--location", "-L", "--globoff", "-g"}:
            i += 1
        elif token.startswith("-") and next_value is not None and not next_value.startswith("-"):
            i += 2
        else:
            i += 1

    if url is None:
        raise MonitorError("Could not find a URL in the captured cURL request.")
    if not any(name.lower() == "authorization" for name in headers):
        headers["authorization"] = "Bearer ${PROLIFIC_BEARER_TOKEN}"
    if method is None:
        method = "POST" if body is not None else "GET"

    return {
        "poll_seconds": 20,
        "request_timeout_seconds": 20,
        "studies_path": None,
        "state_file": DEFAULT_STATE,
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
        },
        "request": {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
        },
        "notes": {
            "created_at": utc_now(),
            "dropped_headers": dropped_headers,
            "token_source": "Set PROLIFIC_BEARER_TOKEN in your environment or .env file.",
        },
    }


def load_config(path: Path) -> dict[str, Any]:
    load_dotenv()
    raw = read_json(path)
    config = expand_env(raw)
    request = config.get("request")
    if not isinstance(request, dict):
        raise MonitorError("Config must contain a request object.")
    if not request.get("url"):
        raise MonitorError("Config request.url is required.")
    config.setdefault("poll_seconds", 30)
    config.setdefault("request_timeout_seconds", 20)
    config.setdefault("state_file", DEFAULT_STATE)
    config.setdefault("telegram", {})
    browser = config.setdefault("browser", {})
    if isinstance(browser, dict):
        browser.setdefault("auto_take_part", True)
        browser.setdefault("take_part_timeout_seconds", 15)
        browser.setdefault("target_study_timeout_seconds", 20)
    return config


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 20,
) -> HttpResponse:
    req = Request(url=url, data=body, headers=headers or {}, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=decode_body(response.read(), response.headers.get("Content-Encoding", "")),
            )
    except HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=decode_body(exc.read(), exc.headers.get("Content-Encoding", "")),
        )
    except URLError as exc:
        raise MonitorError(f"Network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MonitorError("Request timed out.") from exc


def decode_body(body: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.lower().strip()
    if not encoding or encoding == "identity":
        return body
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


class DevToolsWebSocket:
    """Tiny dependency-free WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, websocket_url: str, timeout: int = 30):
        self.websocket_url = websocket_url
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self) -> "DevToolsWebSocket":
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise MonitorError(f"Unsupported DevTools WebSocket URL: {self.websocket_url}")

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path
        if parsed.query:
            path += f"?{parsed.query}"

        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=self.timeout)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise MonitorError("Chrome DevTools WebSocket upgrade failed.")

        # A short idle timeout lets the watcher notice when Prolific moves to a new tab.
        sock.settimeout(2)
        self.sock = sock

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_text(json.dumps(payload, separators=(",", ":")))

    def command(self, method: str, params: dict[str, Any] | None = None) -> int:
        message_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_json(payload)
        return message_id

    def send_text(self, text: str) -> None:
        if self.sock is None:
            raise MonitorError("DevTools WebSocket is not connected.")

        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_json(self) -> dict[str, Any]:
        text = self.recv_text()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MonitorError("Chrome DevTools sent invalid JSON.") from exc

    def recv_text(self) -> str:
        if self.sock is None:
            raise MonitorError("DevTools WebSocket is not connected.")

        while True:
            first_two = self.recv_exact(2)
            opcode = first_two[0] & 0x0F
            masked = bool(first_two[1] & 0x80)
            length = first_two[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self.recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.recv_exact(8))[0]

            mask = self.recv_exact(4) if masked else b""
            payload = self.recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:
                raise MonitorError("Chrome DevTools WebSocket closed.")
            if opcode == 0x9:
                self.send_pong(payload)

    def recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise MonitorError("DevTools WebSocket is not connected.")
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise MonitorError("Chrome DevTools WebSocket disconnected.")
            chunks.extend(chunk)
        return bytes(chunks)

    def send_pong(self, payload: bytes) -> None:
        if self.sock is None:
            return
        header = bytearray([0x8A, 0x80 | len(payload)])
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)


def find_browser_executable(explicit_path: str | None = None) -> str:
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return str(path)
        raise MonitorError(f"Browser executable not found: {explicit_path}")

    candidates = [
        os.environ.get("PROLIFIC_BROWSER_PATH", ""),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise MonitorError("Could not find Brave, Chrome, Chromium, or Edge. Set PROLIFIC_BROWSER_PATH.")


def browser_extra_args(cli_args: list[str] | None = None) -> list[str]:
    extra_args: list[str] = []
    env_args = os.environ.get("PROLIFIC_BROWSER_ARGS", "").strip()
    if env_args:
        extra_args.extend(shlex.split(env_args, posix=os.name != "nt"))
    if cli_args:
        extra_args.extend(cli_args)
    return extra_args


def start_browser(
    browser_path: str | None,
    port: int,
    profile_dir: Path,
    url: str = "https://app.prolific.com/studies",
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> subprocess.Popen[Any]:
    executable = find_browser_executable(browser_path)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        executable,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
    ]
    if headless:
        args.append("--headless=new")
    args.extend(browser_extra_args(extra_args))
    args.append(url)
    return subprocess.Popen(args)


def devtools_json(port: int, path: str, timeout: int = 10) -> Any:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_devtools(port: int, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            devtools_json(port, "/json/version", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise MonitorError("Timed out waiting for browser DevTools. Is the browser blocked?")


def get_or_create_page_target(port: int) -> str:
    targets = devtools_json(port, "/json/list")
    for target in targets:
        target_url = target.get("url", "")
        ws_url = target.get("webSocketDebuggerUrl")
        if target.get("type") == "page" and ws_url and "app.prolific.com" in target_url:
            return str(ws_url)

    new_url = f"/json/new?{quote('https://app.prolific.com/studies', safe='')}"
    try:
        req = Request(f"http://127.0.0.1:{port}{new_url}", method="PUT")
        with urlopen(req, timeout=10) as response:
            target = json.loads(response.read().decode("utf-8"))
        return str(target["webSocketDebuggerUrl"])
    except Exception as exc:
        raise MonitorError("Could not create a Prolific browser tab.") from exc


def prolific_target_changed(port: int, current_websocket_url: str) -> bool:
    try:
        targets = devtools_json(port, "/json/list", timeout=2)
    except Exception:
        return False

    current_target_is_prolific = False
    for target in targets:
        if target.get("type") != "page":
            continue
        target_url = str(target.get("url", ""))
        websocket_url = str(target.get("webSocketDebuggerUrl", ""))
        if websocket_url == current_websocket_url:
            current_target_is_prolific = "app.prolific.com" in target_url
    return not current_target_is_prolific


def close_duplicate_prolific_targets(port: int, keep_websocket_url: str) -> int:
    try:
        targets = devtools_json(port, "/json/list", timeout=2)
    except Exception:
        return 0

    closed = 0
    for target in targets:
        if target.get("type") != "page":
            continue
        target_url = str(target.get("url", ""))
        websocket_url = str(target.get("webSocketDebuggerUrl", ""))
        target_id = str(target.get("id", ""))
        if (
            target_id
            and websocket_url != keep_websocket_url
            and "app.prolific.com" in target_url
        ):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/close/{quote(target_id, safe='')}", timeout=2):
                    closed += 1
            except Exception as exc:
                log(f"Could not close a duplicate Prolific tab: {exc}")
    return closed


def extract_bearer_from_headers(headers: dict[str, Any]) -> str | None:
    for key, value in headers.items():
        if key.lower() == "authorization":
            match = re.match(r"Bearer\s+(.+)", str(value), re.I)
            if match:
                return match.group(1).strip()
    return None


def token_expiry_seconds(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = int(data["exp"])
    except Exception:
        return None
    return exp - int(time.time())


def current_bearer_token(env_path: Path = Path(ENV_FILE)) -> str:
    load_dotenv(env_path)
    return os.environ.get("PROLIFIC_BEARER_TOKEN", "").strip()


def current_token_is_fresh(env_path: Path = Path(ENV_FILE), min_seconds: int = 300) -> bool:
    token = current_bearer_token(env_path)
    if not token:
        return False
    remaining = token_expiry_seconds(token)
    return remaining is None or remaining > min_seconds


def save_bearer_token(token: str, env_path: Path = Path(ENV_FILE)) -> None:
    update_env_var(env_path, "PROLIFIC_BEARER_TOKEN", token)
    remaining = token_expiry_seconds(token)
    if remaining is None:
        log("Saved fresh Prolific bearer token.")
    else:
        log(f"Saved fresh Prolific bearer token; expires in about {max(0, remaining // 60)} minute(s).")


def wait_for_fresh_token(env_path: Path, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if current_token_is_fresh(env_path):
            return
        time.sleep(1)
    raise MonitorError(
        "Timed out waiting for a fresh Prolific bearer token. "
        "Make sure the browser window is logged in and on the Prolific studies page."
    )


def watch_browser_tokens(
    port: int = DEFAULT_BROWSER_PORT,
    env_path: Path = Path(ENV_FILE),
    config_path: Path | None = None,
    request_url_part: str = PROLIFIC_STUDIES_URL_PART,
    stop_event: threading.Event | None = None,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
) -> None:
    wait_for_devtools(port)
    websocket_url = get_or_create_page_target(port)
    seen_tokens: set[str] = set()
    request_urls: dict[str, str] = {}
    pending_bodies: dict[int, str] = {}
    pending_clicks: dict[int, str] = {}
    pending_studies: deque[dict[str, Any]] = deque()
    active_target = False
    active_target_deadline: float | None = None
    target_timeout_seconds = 20
    if config_path is not None:
        try:
            browser_config = load_config(config_path).get("browser")
            if isinstance(browser_config, dict):
                target_timeout_seconds = int(
                    browser_config.get("target_study_timeout_seconds", 20)
                )
        except (MonitorError, TypeError, ValueError) as exc:
            log(f"Could not read browser target timeout; using 20 seconds: {exc}")
    click_guard_marker = "PROLIFIC_AUTO_TAKE_PART_CLICKED"
    reserved_marker = "PROLIFIC_SEAT_RESERVED"
    full_marker = "PROLIFIC_STUDY_FULL"
    high_demand_marker = "PROLIFIC_HIGH_DEMAND_RETRY"
    manual_only_marker = "PROLIFIC_MANUAL_ONLY_SKIPPED"
    target_open_marker = "PROLIFIC_TARGET_STUDY_OPEN_ATTEMPT"
    target_missing_marker = "PROLIFIC_TARGET_STUDY_NOT_FOUND"
    target_expired_marker = "PROLIFIC_TARGET_STUDY_EXPIRED"

    duplicates_closed = close_duplicate_prolific_targets(port, websocket_url)
    if duplicates_closed:
        log(f"Closed {duplicates_closed} duplicate Prolific tab(s).")

    with DevToolsWebSocket(websocket_url) as devtools:
        devtools.command("Network.enable")
        devtools.command("Page.enable")
        devtools.command("Runtime.enable")
        devtools.command("Page.bringToFront")
        devtools.command("Page.navigate", {"url": "https://app.prolific.com/studies"})
        log("Watching browser network requests for fresh Prolific tokens.")

        while stop_event is None or not stop_event.is_set():
            if (
                active_target
                and active_target_deadline is not None
                and time.monotonic() >= active_target_deadline
            ):
                log(
                    f"Browser target timed out after {target_timeout_seconds} seconds; "
                    "advancing to the next queued study."
                )
                active_target = False
                active_target_deadline = None
                devtools.command(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "sessionStorage.removeItem('prolificAutoTargetStudyTitle');"
                            "sessionStorage.removeItem('prolificAutoTargetStudyId');"
                            "sessionStorage.removeItem('prolificAutoTargetStudyDetectedAt')"
                        ),
                    },
                )

            if study_queue is not None:
                while True:
                    try:
                        pending_studies.extend(study_queue.get_nowait())
                    except queue.Empty:
                        break

            if not active_target and pending_studies:
                if prolific_target_changed(port, websocket_url):
                    raise MonitorError("Watcher is no longer on Prolific; reconnecting the auto-click guard.")
                target_study = pending_studies.popleft()
                target_title = first_value(
                    target_study,
                    ("title", "name", "internal_name"),
                    "",
                )
                target_id = study_id(target_study) or ""
                if target_title or target_id:
                    devtools.command(
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "sessionStorage.setItem("
                                "'prolificAutoTargetStudyTitle', "
                                f"{json.dumps(target_title)}"
                                ");sessionStorage.setItem("
                                "'prolificAutoTargetStudyId', "
                                f"{json.dumps(target_id)}"
                                ");sessionStorage.setItem("
                                "'prolificAutoTargetStudyDetectedAt', "
                                "String(Date.now())"
                                ")"
                            ),
                        },
                    )
                    active_target = True
                    active_target_deadline = (
                        time.monotonic() + target_timeout_seconds
                    )
                    target_label = target_title or target_id
                    log(f"Queued study detected; opening it in browser: {target_label}")
                    devtools.command("Page.bringToFront")
                    devtools.command("Page.navigate", {"url": "https://app.prolific.com/studies"})
                else:
                    log("Skipped queued study without a title or ID.")

            try:
                message = devtools.recv_json()
            except socket.timeout:
                if prolific_target_changed(port, websocket_url):
                    raise MonitorError("Watcher is no longer on Prolific; reconnecting the auto-click guard.")
                duplicates_closed = close_duplicate_prolific_targets(port, websocket_url)
                if duplicates_closed:
                    log(f"Closed {duplicates_closed} duplicate Prolific tab(s).")
                install_take_part_guard(devtools, config_path, announce=False)
                continue
            method = message.get("method")
            params = message.get("params") or {}

            if method == "Page.loadEventFired":
                install_take_part_guard(devtools, config_path)

            if method == "Runtime.executionContextCreated":
                install_take_part_guard(devtools, config_path, announce=False)

            if method == "Runtime.consoleAPICalled":
                values = [
                    str((argument or {}).get("value", ""))
                    for argument in params.get("args", [])
                    if isinstance(argument, dict)
                ]
                if click_guard_marker in values:
                    log("Take part button clicked; waiting for Prolific to confirm the reservation.")
                    active_target_deadline = None
                if reserved_marker in values:
                    handle_reservation_result(config_path, reserved=True)
                    active_target = False
                    active_target_deadline = None
                    pending_studies.clear()
                if full_marker in values:
                    handle_reservation_result(config_path, reserved=False)
                    active_target = False
                    active_target_deadline = None
                if high_demand_marker in values:
                    log("High demand detected; automatically retrying the Take part button.")
                if manual_only_marker in values:
                    handle_manual_only_study(config_path, values)
                    active_target = False
                    active_target_deadline = None
                if target_open_marker in values:
                    log("Target study card clicked; waiting for the Take part button.")
                if target_missing_marker in values:
                    log("Target study card not found yet; continuing to retry.")
                if target_expired_marker in values:
                    log(
                        f"Target study expired after {target_timeout_seconds} seconds; "
                        "it is no longer available."
                    )
                    active_target = False
                    active_target_deadline = None

            if "id" in message:
                command_id = int(message["id"])
                source_url = pending_bodies.pop(command_id, "")
                if source_url and "result" in message:
                    pending_clicks.update(
                        handle_browser_studies_body_with_devtools(
                            message["result"],
                            config_path,
                            source_url,
                            devtools=devtools,
                            study_queue=study_queue,
                        )
                    )
                clicked_title = pending_clicks.pop(command_id, "")
                if clicked_title:
                    handle_take_part_result(message, config_path, clicked_title)
                continue

            if method == "Network.requestWillBeSent":
                request_id = str(params.get("requestId", ""))
                request = params.get("request") or {}
                request_url = str(request.get("url", ""))
                if request_id:
                    request_urls[request_id] = request_url
                if request_url_part not in request_url:
                    continue
                headers = request.get("headers") or {}
                token = extract_bearer_from_headers(headers)
                if token and token not in seen_tokens:
                    seen_tokens.add(token)
                    save_bearer_token(token, env_path)

            if method == "Network.loadingFinished":
                request_id = str(params.get("requestId", ""))
                request_url = request_urls.get(request_id, "")
                if request_url_part in request_url:
                    command_id = devtools.command("Network.getResponseBody", {"requestId": request_id})
                    pending_bodies[command_id] = request_url

            if method == "Network.requestWillBeSentExtraInfo":
                headers = params.get("headers") or {}
                token = extract_bearer_from_headers(headers)
                if token and token not in seen_tokens:
                    seen_tokens.add(token)
                    save_bearer_token(token, env_path)


def handle_browser_studies_body(
    result: dict[str, Any],
    config_path: Path | None,
    source_url: str,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
) -> None:
    handle_browser_studies_body_with_devtools(
        result,
        config_path,
        source_url,
        devtools=None,
        study_queue=study_queue,
    )


def handle_browser_studies_body_with_devtools(
    result: dict[str, Any],
    config_path: Path | None,
    source_url: str,
    devtools: DevToolsWebSocket | None,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
) -> dict[int, str]:
    if config_path is None:
        return {}
    body = result.get("body")
    if not isinstance(body, str) or not body:
        return {}
    if result.get("base64Encoded"):
        try:
            body = base64.b64decode(body.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            return {}
    try:
        payload = json.loads(body)
        config = load_config(config_path)
        state_path = Path(str(config.get("state_file", DEFAULT_STATE)))
        studies = extract_studies(payload, config.get("studies_path"))
        new_studies = process_studies(
            studies,
            config,
            state_path,
            notify=True,
            source="browser",
        )
        if study_queue is not None and new_studies:
            study_queue.put(new_studies)
            log(
                f"Queued {len(new_studies)} browser-detected study/studies "
                "for automatic opening."
            )
        # The persistent page guard owns clicking and reservation-result tracking.
        return {}
    except MonitorError as exc:
        log(f"Browser response parse error: {exc}")
    except json.JSONDecodeError:
        return {}
    except Exception as exc:
        log(f"Browser response alert error for {source_url}: {exc}")
    return {}


def maybe_click_take_part(
    devtools: DevToolsWebSocket | None,
    config: dict[str, Any],
    new_studies: list[dict[str, Any]],
) -> dict[int, str]:
    pending_clicks: dict[int, str] = {}
    if devtools is None or not new_studies:
        return pending_clicks

    browser_config = config.get("browser")
    if not isinstance(browser_config, dict) or not browser_config.get("auto_take_part", True):
        return pending_clicks

    timeout_seconds = int(browser_config.get("take_part_timeout_seconds", 15))
    for study in new_studies:
        title = first_value(study, ("title", "name", "internal_name"), "Untitled study")
        devtools.command("Page.bringToFront")
        command_id = devtools.command(
            "Runtime.evaluate",
            {
                "expression": build_take_part_script(timeout_seconds),
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        pending_clicks[command_id] = title
        log(f"Triggered browser auto-click attempt for study: {title}")
    return pending_clicks


def handle_take_part_result(message: dict[str, Any], config_path: Path | None, title: str) -> None:
    result = ((message.get("result") or {}).get("result") or {}).get("value")
    if result != "clicked":
        return
    log(f"Take part button clicked for {title}; waiting for Prolific to confirm the reservation.")


def handle_reservation_result(config_path: Path | None, reserved: bool) -> None:
    if config_path is None:
        return
    try:
        config = load_config(config_path)
        if reserved:
            message = "Seat reserved successfully."
        else:
            message = "Study is full. Seat was not reserved."
        send_telegram(config, f"<b>{message}</b>")
        log("========================================")
        log(message)
        log("========================================")
    except MonitorError as exc:
        log(f"Telegram reservation result message failed: {exc}")


def handle_manual_only_study(config_path: Path | None, console_values: list[str]) -> None:
    if config_path is None:
        return
    try:
        config = load_config(config_path)
        marker_index = console_values.index("PROLIFIC_MANUAL_ONLY_SKIPPED")
        identity_parts = console_values[marker_index + 1 : marker_index + 3]
        display_title = (
            identity_parts[1].strip()
            if len(identity_parts) > 1 and identity_parts[1].strip()
            else "Saudi Sexism Detection"
        )
        study_identity = " | ".join(
            part.strip().casefold() for part in identity_parts if part.strip()
        )
        if not study_identity:
            study_identity = "saudi sexism detection | qmul.ac.uk"

        notification_state_path = Path(DEFAULT_MANUAL_ONLY_NOTIFICATION_STATE)
        notification_state: dict[str, Any] = {}
        if notification_state_path.exists():
            try:
                notification_state = json.loads(
                    notification_state_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                notification_state = {}
        notified_studies = set(
            str(item) for item in notification_state.get("notified_studies", [])
        )
        if study_identity in notified_studies:
            log("Manual-only Telegram message already sent for this study; duplicate suppressed.")
            return

        send_telegram(
            config,
            "<b>Manual-only study detected.</b>\n"
            f"{html.escape(display_title)}\n"
            "Auto-reservation skipped.",
        )
        notified_studies.add(study_identity)
        write_json(
            notification_state_path,
            {"notified_studies": sorted(notified_studies)},
        )
        log("Manual-only study detected; auto-reservation skipped.")
    except (MonitorError, ValueError) as exc:
        log(f"Telegram manual-only message failed: {exc}")


def install_take_part_guard(
    devtools: DevToolsWebSocket,
    config_path: Path | None,
    announce: bool = True,
) -> None:
    if config_path is None:
        return
    try:
        config = load_config(config_path)
        browser_config = config.get("browser")
        if not isinstance(browser_config, dict) or not browser_config.get("auto_take_part", True):
            return
        target_study_timeout_seconds = int(
            browser_config.get("target_study_timeout_seconds", 60)
        )
        devtools.command(
            "Runtime.evaluate",
            {
                "expression": build_persistent_take_part_script(
                    target_study_timeout_seconds
                ),
                "userGesture": True,
            },
        )
        if announce:
            log("Browser auto-click guard is active.")
    except (MonitorError, TypeError, ValueError) as exc:
        log(f"Could not install browser auto-click guard: {exc}")


def build_persistent_take_part_script(
    target_study_timeout_seconds: int = 20,
) -> str:
    script = """
(() => {
  if (window.__prolificTakePartGuardActive) return "already-active";
  window.__prolificTakePartGuardActive = true;

  const marker = "PROLIFIC_AUTO_TAKE_PART_CLICKED";
  const reservedMarker = "PROLIFIC_SEAT_RESERVED";
  const fullMarker = "PROLIFIC_STUDY_FULL";
  const highDemandMarker = "PROLIFIC_HIGH_DEMAND_RETRY";
  const manualOnlyMarker = "PROLIFIC_MANUAL_ONLY_SKIPPED";
  const targetOpenMarker = "PROLIFIC_TARGET_STUDY_OPEN_ATTEMPT";
  const targetMissingMarker = "PROLIFIC_TARGET_STUDY_NOT_FOUND";
  const targetExpiredMarker = "PROLIFIC_TARGET_STUDY_EXPIRED";
  const targetText = "take part in this study";
  const targetStudyMaxAgeMs = __TARGET_STUDY_MAX_AGE_MS__;
  const manualOnlyEnabled = __MANUAL_ONLY_ENABLED__;
  const manualOnlyTitle = "saudi sexism detection";
  const manualOnlyResearcher = "qmul.ac.uk";
  const manualOnlyReportedKey = "prolificManualOnlyReportedUrl";
  const clickedControls = new WeakSet();
  const pendingKey = "prolificAutoTakePartPendingAt";
  const targetStudyKey = "prolificAutoTargetStudyTitle";
  const targetStudyIdKey = "prolificAutoTargetStudyId";
  const targetStudyDetectedAtKey = "prolificAutoTargetStudyDetectedAt";
  const readPendingAt = () => {
    try {
      return Number(sessionStorage.getItem(pendingKey) || 0);
    } catch (_) {
      return 0;
    }
  };
  const writePendingAt = (value) => {
    try {
      if (value) sessionStorage.setItem(pendingKey, String(value));
      else sessionStorage.removeItem(pendingKey);
    } catch (_) {}
  };
  let pendingAt = readPendingAt();
  let waitingForResult = pendingAt > 0 && Date.now() - pendingAt < 120000;
  let reportedResult = false;
  let retryMode = false;
  let nextRetryAt = 0;
  let lastHighDemandNoticeAt = 0;
  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
  let targetStudyTitleRaw = "";
  let targetStudyTitle = "";
  let targetStudyId = "";
  let targetStudyDetectedAt = 0;
  try {
    targetStudyTitleRaw = sessionStorage.getItem(targetStudyKey) || "";
    targetStudyTitle = normalize(targetStudyTitleRaw);
    targetStudyId = normalize(sessionStorage.getItem(targetStudyIdKey) || "");
    targetStudyDetectedAt = Number(
      sessionStorage.getItem(targetStudyDetectedAtKey) || 0
    );
  } catch (_) {}
  let targetStudySelected = !targetStudyTitle && !targetStudyId;
  let targetStudyExpired = false;
  let targetReadyAt = 0;
  let targetSelectionAttemptedAt = 0;
  let lastTargetMissingNoticeAt = 0;
  const clearTargetStudy = () => {
    try {
      [targetStudyKey, targetStudyIdKey, targetStudyDetectedAtKey]
        .forEach((key) => sessionStorage.removeItem(key));
    } catch (_) {}
    targetStudyTitleRaw = "";
    targetStudyTitle = "";
    targetStudyId = "";
    targetStudyDetectedAt = 0;
  };
  const expireTargetStudyIfNeeded = () => {
    if (targetStudyExpired) return true;
    if (!targetStudyTitle && !targetStudyId) return false;
    if (
      targetStudyDetectedAt &&
      Date.now() - targetStudyDetectedAt < targetStudyMaxAgeMs
    ) return false;
    const expiredStudyId = targetStudyId;
    const expiredStudyTitle = targetStudyTitleRaw;
    clearTargetStudy();
    targetStudyExpired = true;
    targetStudySelected = false;
    console.log(targetExpiredMarker, expiredStudyId, expiredStudyTitle);
    return true;
  };
  const reservedTexts = [
    "your place has been reserved",
    "your place is reserved",
    "place has been reserved",
    "place is reserved for you",
    "you have a reserved place",
    "start the study",
    "open study in new window"
  ];
  const fullTexts = [
    "study is full",
    "study is currently full",
    "this study is full",
    "no places available",
    "no places are available",
    "all places have been taken",
    "study has filled up"
  ];
  const highDemandTexts = [
    "high demand",
    "in high demand",
    "many participants are trying",
    "a lot of participants are trying",
    "please try again"
  ];
  const roots = () => {
    const found = [document];
    for (let index = 0; index < found.length; index += 1) {
      const root = found[index];
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) found.push(element.shadowRoot);
      }
    }
    return found;
  };
  const resultTexts = () => {
    const selectors = [
      "[role='alert']",
      "[role='dialog']",
      "[aria-live='assertive']",
      "[aria-live='polite']",
      "mat-snack-bar-container",
      ".toast",
      ".snackbar",
      ".notification",
      ".modal",
      "main"
    ].join(",");
    const texts = [];
    for (const root of roots()) {
      for (const element of root.querySelectorAll(selectors)) {
        const rect = element.getBoundingClientRect();
        if (!rect.width || !rect.height) continue;
        const text = normalize(element.innerText || element.textContent || "");
        if (
          text &&
          !text.includes("essential maintenance") &&
          !text.includes("you may see more study full")
        ) {
          texts.push(text);
        }
      }
    }
    return texts;
  };
  const visibleTakePartControl = () => {
    for (const root of roots()) {
      for (const control of root.querySelectorAll(
        "button, a, [role='button'], input[type='button'], input[type='submit']"
      )) {
        const label = control.value || control.getAttribute("aria-label") ||
          control.innerText || control.textContent || "";
        const rect = control.getBoundingClientRect();
        if (
          normalize(label).includes(targetText) &&
          !control.disabled &&
          rect.width &&
          rect.height
        ) {
          return control;
        }
      }
    }
    return null;
  };
  const simplifyTitle = (value) => normalize(value).replace(/[^a-z0-9]+/g, " ").trim();
  const targetTitleTokens = simplifyTitle(targetStudyTitle)
    .split(" ")
    .filter((token) => token.length > 2);
  const titleSimilarity = (value) => {
    const simplified = simplifyTitle(value);
    if (!simplified || !targetTitleTokens.length) return 0;
    const targetSimplified = simplifyTitle(targetStudyTitle);
    if (
      simplified.includes(targetSimplified) ||
      targetSimplified.includes(simplified)
    ) return 1;
    const availableTokens = new Set(simplified.split(" ").filter(Boolean));
    const matches = targetTitleTokens.filter((token) => availableTokens.has(token)).length;
    return matches / targetTitleTokens.length;
  };
  const identityValues = (element) => {
    const values = [];
    let current = element;
    for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
      for (const attribute of Array.from(current.attributes || [])) {
        values.push(normalize(attribute.value));
      }
    }
    return values;
  };
  const activeDetailContext = () => {
    const takePartControl = visibleTakePartControl();
    if (!takePartControl) return {headingTexts: [], text: ""};
    let current = takePartControl;
    for (let depth = 0; current && depth < 10; depth += 1, current = current.parentElement) {
      const headingTexts = Array.from(current.querySelectorAll("h1"))
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          return rect.width && rect.height;
        })
        .map((element) => (element.innerText || element.textContent || "").trim())
        .filter(Boolean);
      if (headingTexts.length) {
        return {
          headingTexts,
          text: normalize(current.innerText || current.textContent || "")
        };
      }
    }
    return {headingTexts: [], text: ""};
  };
  const activeStudyMatchesTarget = () => {
    if (!targetStudyTitle && !targetStudyId) return false;
    const takePartControl = visibleTakePartControl();
    if (!takePartControl) return false;
    if (
      targetStudyId &&
      (
        normalize(location.href).includes(targetStudyId) ||
        identityValues(takePartControl).some((value) => value.includes(targetStudyId))
      )
    ) return true;
    const detail = activeDetailContext();
    return Boolean(
      targetStudyTitle &&
      detail.headingTexts.some((heading) => titleSimilarity(heading) >= 0.75)
    );
  };
  const selectTargetStudy = () => {
    if (expireTargetStudyIfNeeded()) return false;
    // No stored target means there is no study card to search for. Continue
    // normally so the guard can still click a visible Take part button.
    if (!targetStudyTitle && !targetStudyId) return true;
    if (activeStudyMatchesTarget()) {
      targetStudySelected = true;
      return Date.now() >= targetReadyAt;
    }
    if (targetStudySelected) {
      if (Date.now() < targetReadyAt) return false;
      if (targetSelectionAttemptedAt && Date.now() - targetSelectionAttemptedAt < 2000) {
        return false;
      }
      targetStudySelected = false;
    }
    const matches = [];
    for (const root of roots()) {
      for (const element of root.querySelectorAll(
        "button, a, [role='button'], [tabindex='0']"
      )) {
        const text = normalize(element.innerText || element.textContent || "");
        const rect = element.getBoundingClientRect();
        if (!rect.width || !rect.height || element.disabled) continue;
        const idMatch = Boolean(
          targetStudyId &&
          identityValues(element).some((value) => value.includes(targetStudyId))
        );
        const similarity = titleSimilarity(text);
        if (idMatch || similarity >= 0.75) {
          matches.push({element, idMatch, similarity, textLength: text.length});
        }
      }
    }
    matches.sort((left, right) =>
      Number(right.idMatch) - Number(left.idMatch) ||
      right.similarity - left.similarity ||
      left.textLength - right.textLength
    );
    const match = matches[0];
    if (!match) {
      if (Date.now() - lastTargetMissingNoticeAt >= 5000) {
        lastTargetMissingNoticeAt = Date.now();
        console.log(targetMissingMarker, targetStudyId, targetStudyTitleRaw);
      }
      return false;
    }
    match.element.scrollIntoView({block: "center", inline: "nearest"});
    match.element.click();
    console.log(targetOpenMarker, targetStudyId, targetStudyTitleRaw);
    targetStudySelected = true;
    targetSelectionAttemptedAt = Date.now();
    targetReadyAt = Date.now() + 500;
    return false;
  };
  const isManualOnlyStudy = () => {
    if (!manualOnlyEnabled || !document.body) return false;
    const detail = activeDetailContext();
    const selectedTitle = targetStudyTitle || normalize(detail.headingTexts.join(" "));
    const selectedTitleDisplay = targetStudyTitleRaw || detail.headingTexts.join(" ");
    if (
      !selectedTitle.includes(manualOnlyTitle) ||
      !detail.text.includes(manualOnlyResearcher)
    ) {
      return false;
    }
    const hadQueuedTarget = Boolean(targetStudyTitle || targetStudyId);
    let alreadyReported = false;
    try {
      alreadyReported = sessionStorage.getItem(manualOnlyReportedKey) === location.href;
      sessionStorage.setItem(manualOnlyReportedKey, location.href);
    } catch (_) {}
    clearTargetStudy();
    if (hadQueuedTarget || !alreadyReported) {
      console.log(manualOnlyMarker, location.href, selectedTitleDisplay);
    }
    return true;
  };
  const detectReservationResult = () => {
    if (!waitingForResult || reportedResult || !document.body) return false;
    const pageText = normalize(document.body.innerText || document.body.textContent || "");
    const statusTexts = resultTexts();
    const isReserved =
      reservedTexts.some((text) => pageText.includes(text)) ||
      (
        pageText.includes("time remaining") &&
        pageText.includes("cancel participation")
      );
    if (isReserved) {
      reportedResult = true;
      waitingForResult = false;
      writePendingAt(0);
      console.log(reservedMarker);
      return true;
    }
    if (fullTexts.some((text) => statusTexts.some((status) => status.includes(text)))) {
      reportedResult = true;
      waitingForResult = false;
      writePendingAt(0);
      console.log(fullMarker);
      return true;
    }
    if (highDemandTexts.some((text) => statusTexts.some((status) => status.includes(text)))) {
      waitingForResult = false;
      retryMode = true;
      if (Date.now() - lastHighDemandNoticeAt >= 5000) {
        lastHighDemandNoticeAt = Date.now();
        console.log(highDemandMarker);
      }
      return true;
    }
    if (pendingAt && Date.now() - pendingAt >= 120000) {
      waitingForResult = false;
      writePendingAt(0);
    }
    return false;
  };
  const clickCandidate = () => {
    if (!selectTargetStudy()) return false;
    if (isManualOnlyStudy()) return false;
    for (const root of roots()) {
      const controls = root.querySelectorAll(
        "button, a, [role='button'], input[type='button'], input[type='submit']"
      );
      for (const control of controls) {
        const label = control.value || control.getAttribute("aria-label") ||
          control.innerText || control.textContent || "";
        const normalizedLabel = normalize(label);
        const isTakePart = normalizedLabel.includes(targetText);
        const isRetry = retryMode && (
          normalizedLabel.includes("try again") ||
          normalizedLabel.includes("retry")
        );
        if (
          (!isTakePart && !isRetry) ||
          control.disabled ||
          (clickedControls.has(control) && !retryMode) ||
          (retryMode && Date.now() < nextRetryAt)
        ) continue;
        const rect = control.getBoundingClientRect();
        if (!rect.width || !rect.height) continue;
        clickedControls.add(control);
        control.scrollIntoView({block: "center", inline: "center"});
        pendingAt = Date.now();
        writePendingAt(pendingAt);
        waitingForResult = true;
        reportedResult = false;
        retryMode = false;
        nextRetryAt = Date.now() + 500;
        control.click();
        clearTargetStudy();
        console.log(marker);
        return true;
      }
    }
    return false;
  };

  if (clickCandidate()) return "clicked";
  const observer = new MutationObserver(() => {
    detectReservationResult();
    if (!waitingForResult) clickCandidate();
  });
  observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  setInterval(() => {
    detectReservationResult();
    if (!waitingForResult) clickCandidate();
  }, 100);
  return "watching";
})()
""".strip()
    return (
        script.replace(
            "__MANUAL_ONLY_ENABLED__",
            "true" if MANUAL_ONLY_STUDY_RULE_ENABLED else "false",
        ).replace(
            "__TARGET_STUDY_MAX_AGE_MS__",
            str(max(5, target_study_timeout_seconds) * 1000),
        )
    )


def build_take_part_script(timeout_seconds: int) -> str:
    timeout_ms = max(1000, timeout_seconds * 1000)
    return f"""
(() => {{
  const targetTexts = ["Take part in this study", "Take part in this study when"];
  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
  const matches = (element) => {{
    const text = normalize(element.innerText || element.textContent || "");
    return targetTexts.some((target) => text.includes(normalize(target)));
  }};
  const clickCandidate = () => {{
    const elements = Array.from(document.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']"));
    const candidate = elements.find(matches);
    if (!candidate) {{
      return false;
    }}
    candidate.scrollIntoView({{block: "center", inline: "center"}});
    candidate.click();
    return true;
  }};

  return new Promise((resolve) => {{
    const deadline = Date.now() + {timeout_ms};
    if (clickCandidate()) {{
      resolve("clicked");
      return;
    }}
    const timer = setInterval(() => {{
      if (clickCandidate()) {{
        clearInterval(timer);
        resolve("clicked");
        return;
      }}
      if (Date.now() >= deadline) {{
        clearInterval(timer);
        resolve("not-found");
      }}
    }}, 500);
  }});
}})()
""".strip()


def run_browser_token_watcher(
    browser_path: str | None,
    port: int,
    profile_dir: Path,
    env_path: Path,
    config_path: Path | None,
    stop_event: threading.Event,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> None:
    try:
        wait_for_devtools(port, timeout_seconds=2)
        log(f"Connected to existing browser DevTools on port {port}.")
    except MonitorError:
        log("Starting browser with DevTools enabled. Log in to Prolific there if prompted.")
        start_browser(browser_path, port, profile_dir, headless=headless, extra_args=extra_args)

    while not stop_event.is_set():
        try:
            watch_browser_tokens(
                port=port,
                env_path=env_path,
                config_path=config_path,
                stop_event=stop_event,
                study_queue=study_queue,
            )
        except (MonitorError, OSError, TimeoutError) as exc:
            if stop_event.is_set():
                break
            if str(exc).startswith("Watcher is no longer on Prolific"):
                log(str(exc))
                time.sleep(0.5)
            else:
                log(f"Browser token watcher error: {exc}")
                time.sleep(5)


def fetch_studies(config: dict[str, Any]) -> list[dict[str, Any]]:
    request_config = config["request"]
    method = str(request_config.get("method", "GET")).upper()
    url = str(request_config["url"])
    headers = {str(key): str(value) for key, value in request_config.get("headers", {}).items()}
    body_config = request_config.get("body")
    body = None if body_config is None else str(body_config).encode("utf-8")

    response = http_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout=int(config.get("request_timeout_seconds", 20)),
    )

    if response.status in {401, 403, 404}:
        raise MonitorError(
            f"Prolific returned HTTP {response.status}. The bearer token may have expired."
        )
    if response.status >= 400:
        snippet = response.body[:300].decode("utf-8", errors="replace")
        raise MonitorError(f"Prolific returned HTTP {response.status}: {snippet}")

    try:
        payload = json.loads(response.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError("Prolific response was not valid JSON.") from exc

    return extract_studies(payload, config.get("studies_path"))


def extract_studies(payload: Any, studies_path: str | None = None) -> list[dict[str, Any]]:
    if studies_path:
        payload = get_path(payload, studies_path)
        if not isinstance(payload, list):
            raise MonitorError(f"studies_path did not point to a list: {studies_path}")
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("results", "studies", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    candidates: list[list[dict[str, Any]]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items:
                score = sum(1 for item in dict_items if study_id(item) is not None)
                if score:
                    candidates.append(dict_items)
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(payload)
    if not candidates:
        raise MonitorError(
            "Could not find a study list in the response. Set studies_path in the config."
        )
    return max(candidates, key=lambda items: sum(1 for item in items if study_id(item) is not None))


def get_path(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise MonitorError(f"Invalid studies_path segment: {part}")
    return current


def study_id(study: dict[str, Any]) -> str | None:
    for key in ("id", "_id", "study_id", "studyId", "external_id", "externalId"):
        value = study.get(key)
        if value:
            return str(value)
    return None


def first_value(study: dict[str, Any], keys: tuple[str, ...], fallback: str = "") -> str:
    for key in keys:
        value = study.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def format_prolific_reward(study: dict[str, Any], *keys: str) -> str:
    """Format Prolific's minor-unit reward value like the participant website."""
    if not keys:
        keys = ("reward", "pay", "payment", "reward_per_hour")

    for key in keys:
        value = study.get(key)
        if isinstance(value, dict):
            amount = value.get("amount")
            if amount is None:
                amount = value.get("value")
            currency = value.get("currency") or value.get("currency_code")
        else:
            amount = value
            currency = None

        if amount in (None, ""):
            continue

        # Numeric rewards returned by Prolific are in minor currency units
        # (for example, 398 means GBP 3.98).
        try:
            major_amount = Decimal(str(amount)) / Decimal("100")
        except (InvalidOperation, ValueError):
            return str(amount)

        currency = str(
            currency
            or study.get("currency")
            or study.get("currency_code")
            or study.get("currencyCode")
            or "GBP"
        ).upper()
        symbols = {"GBP": "£", "USD": "$", "EUR": "€"}
        formatted_amount = f"{major_amount:.2f}"
        symbol = symbols.get(currency)
        return f"{symbol}{formatted_amount}" if symbol else f"{formatted_amount} {currency}"
    return ""


def format_study(study: dict[str, Any]) -> str:
    title = html.escape(first_value(study, ("title", "name", "internal_name"), "Untitled study"))
    sid = html.escape(study_id(study) or "unknown")
    reward = html.escape(
        format_prolific_reward(study, "reward", "pay", "payment", "reward_per_hour")
    )
    duration = html.escape(first_value(study, ("estimated_completion_time", "duration", "time"), ""))
    places = html.escape(first_value(study, ("places", "places_remaining", "available_places"), ""))
    url = html.escape(first_value(study, ("url", "link"), "https://app.prolific.com/studies"))

    lines = [f"<b>New Prolific study</b>", f"<b>{title}</b>", f"ID: <code>{sid}</code>"]
    if reward:
        lines.append(f"Reward: {reward}")
    if duration:
        lines.append(f"Duration: {duration}")
    if places:
        lines.append(f"Places: {places}")
    lines.append(f'<a href="{url}">Open Prolific</a>')
    return "\n".join(lines)


def send_telegram(config: dict[str, Any], text: str) -> None:
    telegram = config.get("telegram") or {}
    bot_token = telegram.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = telegram.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise MonitorError("Telegram bot token or chat ID is missing.")

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    response = http_request(
        method="POST",
        url=f"https://api.telegram.org/bot{bot_token}/sendMessage",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=payload,
        timeout=int(config.get("request_timeout_seconds", 20)),
    )
    if response.status >= 400:
        snippet = response.body[:300].decode("utf-8", errors="replace")
        raise MonitorError(f"Telegram returned HTTP {response.status}: {snippet}")


def check_once(
    config: dict[str, Any],
    state_path: Path,
    notify: bool = True,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
) -> int:
    studies = fetch_studies(config)
    new_studies = process_studies(studies, config, state_path, notify=notify, source="poll")
    if study_queue is not None and new_studies:
        study_queue.put(new_studies)
        log(
            f"Queued {len(new_studies)} poll-detected study/studies "
            "for automatic opening."
        )
    return len(new_studies)


def process_studies(
    studies: list[dict[str, Any]],
    config: dict[str, Any],
    state_path: Path,
    notify: bool = True,
    source: str = "poll",
) -> list[dict[str, Any]]:
    with STATE_LOCK:
        return process_studies_locked(
            studies,
            config,
            state_path,
            notify=notify,
            source=source,
        )


def process_studies_locked(
    studies: list[dict[str, Any]],
    config: dict[str, Any],
    state_path: Path,
    notify: bool = True,
    source: str = "poll",
) -> list[dict[str, Any]]:
    state = load_state(state_path)
    initialized = bool(state.get("initialized", False))
    seen_ids = set(str(item) for item in state.get("seen_ids", []))

    current_ids: list[str] = []
    new_studies: list[dict[str, Any]] = []
    for study in studies:
        sid = study_id(study)
        if sid is None:
            continue
        current_ids.append(sid)
        if sid not in seen_ids:
            new_studies.append(study)

    if not initialized:
        log(f"Initialized with {len(current_ids)} current studies from {source}; no alerts sent on first run.")
        new_studies = []
    elif new_studies:
        log(f"Found {len(new_studies)} new study/studies from {source}.")
    else:
        log(f"No new studies from {source}. Seen total: {len(seen_ids)}.")

    if notify:
        for study in new_studies:
            send_telegram(config, format_study(study))
            time.sleep(0.5)

    updated_seen = sorted(seen_ids.union(current_ids))
    state.update(
        {
            "seen_ids": updated_seen,
            "initialized": True,
            "last_checked_at": utc_now(),
            "last_error": None,
            "last_count": len(current_ids),
        }
    )
    write_json(state_path, state)
    return new_studies


def run_forever(
    config_path: Path,
    study_queue: queue.Queue[list[dict[str, Any]]] | None = None,
) -> None:
    stop = False

    def handle_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    consecutive_failures = 0
    token_error_notified = False

    while not stop:
        load_dotenv()
        try:
            config = load_config(config_path)
            state_path = Path(str(config.get("state_file", DEFAULT_STATE)))
            check_once(config, state_path, study_queue=study_queue)
            consecutive_failures = 0
            token_error_notified = False
            sleep_seconds = int(config.get("poll_seconds", 30))
        except MonitorError as exc:
            consecutive_failures += 1
            message = str(exc)
            log(f"Error: {message}")
            sleep_seconds = min(300, 10 * (2 ** min(consecutive_failures, 5)))
            try:
                config = load_config(config_path)
                state_path = Path(str(config.get("state_file", DEFAULT_STATE)))
                state = load_state(state_path)
                state["last_error"] = {"at": utc_now(), "message": message}
                write_json(state_path, state)
                if "token may have expired" in message.lower() and not token_error_notified:
                    send_telegram(config, f"<b>Prolific monitor needs attention</b>\n{html.escape(message)}")
                    token_error_notified = True
            except MonitorError:
                pass

        jitter = random.uniform(0, max(1, min(5, sleep_seconds * 0.1)))
        for _ in range(max(1, int(sleep_seconds + jitter))):
            if stop:
                break
            time.sleep(1)

    log("Stopped.")


def command_init_from_curl(args: argparse.Namespace) -> int:
    if args.curl_file:
        curl_text = Path(args.curl_file).read_text(encoding="utf-8")
    else:
        log("Paste the cURL request, then press Ctrl+Z and Enter on Windows or Ctrl+D on Unix.")
        curl_text = sys.stdin.read()

    config = parse_curl(curl_text)
    output = Path(args.output)
    if output.exists() and not args.force:
        raise MonitorError(f"{output} already exists. Re-run with --force to overwrite it.")
    write_json(output, config)
    log(f"Wrote {output}")
    if config.get("notes", {}).get("dropped_headers"):
        log("Dropped Cookie header from the config. Re-add only if the API truly requires it.")
    return 0


def command_once(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    state_path = Path(str(config.get("state_file", DEFAULT_STATE)))
    count = check_once(config, state_path, notify=not args.no_notify)
    log(f"Completed one check; new studies: {count}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    run_forever(Path(args.config))
    return 0


def command_refresh_token(args: argparse.Namespace) -> int:
    stop_event = threading.Event()

    def handle_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    run_browser_token_watcher(
        browser_path=args.browser_path,
        port=args.port,
        profile_dir=Path(args.profile_dir),
        env_path=Path(args.env),
        config_path=None,
        stop_event=stop_event,
        headless=args.headless,
        extra_args=args.browser_extra_arg,
    )
    return 0


def command_run_with_browser(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    detected_studies: queue.Queue[list[dict[str, Any]]] = queue.Queue()
    env_path = Path(args.env)
    config_path = Path(args.config)
    watcher = threading.Thread(
        target=run_browser_token_watcher,
        kwargs={
            "browser_path": args.browser_path,
            "port": args.port,
            "profile_dir": Path(args.profile_dir),
            "env_path": env_path,
            "config_path": config_path,
            "stop_event": stop_event,
            "study_queue": detected_studies,
            "headless": args.headless,
            "extra_args": args.browser_extra_arg,
        },
        daemon=True,
    )
    watcher.start()
    try:
        if not current_token_is_fresh(env_path):
            log("Waiting for browser to provide a fresh Prolific bearer token.")
            wait_for_fresh_token(env_path)
        run_forever(config_path, study_queue=detected_studies)
    finally:
        stop_event.set()
        watcher.join(timeout=5)
    return 0


def command_test_telegram(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    send_telegram(config, "<b>Prolific monitor test</b>\nTelegram notifications are working.")
    log("Sent Telegram test message.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor Prolific studies and notify Telegram.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-from-curl", help="Create config from browser-copied cURL.")
    init_parser.add_argument("--curl-file", help="Text file containing the copied cURL request.")
    init_parser.add_argument("--output", default=DEFAULT_CONFIG, help="Config file to write.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file.")
    init_parser.set_defaults(func=command_init_from_curl)

    once_parser = subparsers.add_parser("once", help="Run a single poll cycle.")
    once_parser.add_argument("--config", default=DEFAULT_CONFIG, help="Config file to use.")
    once_parser.add_argument("--no-notify", action="store_true", help="Detect without sending Telegram alerts.")
    once_parser.set_defaults(func=command_once)

    run_parser = subparsers.add_parser("run", help="Run continuously.")
    run_parser.add_argument("--config", default=DEFAULT_CONFIG, help="Config file to use.")
    run_parser.set_defaults(func=command_run)

    refresh_parser = subparsers.add_parser(
        "refresh-token",
        help="Open/connect to a browser and keep PROLIFIC_BEARER_TOKEN fresh in .env.",
    )
    refresh_parser.add_argument("--browser-path", help="Path to Brave, Chrome, or Edge.")
    refresh_parser.add_argument("--port", type=int, default=DEFAULT_BROWSER_PORT, help="DevTools port.")
    refresh_parser.add_argument("--profile-dir", default=DEFAULT_BROWSER_PROFILE, help="Browser profile directory.")
    refresh_parser.add_argument("--env", default=ENV_FILE, help=".env file to update.")
    refresh_parser.add_argument("--headless", action="store_true", help="Start Chrome/Chromium in headless mode.")
    refresh_parser.add_argument(
        "--browser-extra-arg",
        action="append",
        default=[],
        help="Extra argument to pass to the browser. Can be used more than once.",
    )
    refresh_parser.set_defaults(func=command_refresh_token)

    browser_run_parser = subparsers.add_parser(
        "run-with-browser",
        help="Run the monitor and a browser token refresher together.",
    )
    browser_run_parser.add_argument("--config", default=DEFAULT_CONFIG, help="Config file to use.")
    browser_run_parser.add_argument("--browser-path", help="Path to Brave, Chrome, or Edge.")
    browser_run_parser.add_argument("--port", type=int, default=DEFAULT_BROWSER_PORT, help="DevTools port.")
    browser_run_parser.add_argument("--profile-dir", default=DEFAULT_BROWSER_PROFILE, help="Browser profile directory.")
    browser_run_parser.add_argument("--env", default=ENV_FILE, help=".env file to update.")
    browser_run_parser.add_argument("--headless", action="store_true", help="Start Chrome/Chromium in headless mode.")
    browser_run_parser.add_argument(
        "--browser-extra-arg",
        action="append",
        default=[],
        help="Extra argument to pass to the browser. Can be used more than once.",
    )
    browser_run_parser.set_defaults(func=command_run_with_browser)

    telegram_parser = subparsers.add_parser("test-telegram", help="Send a Telegram test message.")
    telegram_parser.add_argument("--config", default=DEFAULT_CONFIG, help="Config file to use.")
    telegram_parser.set_defaults(func=command_test_telegram)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
