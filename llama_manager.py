"""llama.cpp lifecycle management for the dot app.

Attaches to a healthy server when one is already running, otherwise spawns
llama-server in its own process group and watches /health until the model is
ready. The manager is intentionally stdlib-only so it can be tested without the
web stack.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

ROOT = Path(__file__).resolve().parent

# Localhost health checks must never go through an HTTP proxy from the
# environment (some machines export HTTP_PROXY/ALL_PROXY globally).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

State = Literal["starting", "ready", "error", "stopped"]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class LlamaConfig:
    bin: Path = field(
        default_factory=lambda: Path(
            _env("LLAMA_BIN", str(Path.home() / "Projects/llama.cpp/build/bin/llama-server"))
        ).expanduser()
    )
    model: Path = field(
        default_factory=lambda: Path(
            _env("MODEL", str(ROOT / "models/Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf"))
        ).expanduser()
    )
    drafter: Optional[Path] = field(
        default_factory=lambda: (
            Path(
                _env(
                    "DRAFTER",
                    str(ROOT / "models/Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf"),
                )
            ).expanduser()
            if os.environ.get("DFLASH", "0") == "1"
            else None
        )
    )
    host: str = field(default_factory=lambda: _env("LLAMA_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("LLAMA_PORT", "1234")))
    ctx: int = field(default_factory=lambda: int(_env("CTX", "32768")))
    ngl: int = field(default_factory=lambda: int(_env("NGL", "99")))
    alias: str = field(default_factory=lambda: _env("ALIAS", "nemotron-bolt"))
    no_spawn: bool = field(default_factory=lambda: _env("LLAMA_NO_SPAWN", "0") == "1")
    keep: bool = field(default_factory=lambda: _env("KEEP_LLAMA", "0") == "1")
    timeout_s: float = field(default_factory=lambda: float(_env("LLAMA_TIMEOUT", "180")))
    log_path: Path = field(default_factory=lambda: ROOT / "logs/llama.log")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class LlamaManager:
    def __init__(self, config: Optional[LlamaConfig] = None) -> None:
        self.config = config or LlamaConfig()
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._owned = False
        self._attached = False
        self._stop_event = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._log_file = None
        self._state: State = "stopped"
        self._detail = "not started"
        self._error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._ready_at: Optional[float] = None
        self._down_since: Optional[float] = None

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        with self._lifecycle:
            if self._proc is not None or self._attached:
                return
            self._stop_event.clear()
            self._error = None
            self._ready_at = None
            self._started_at = time.time()
            self._set("starting", "checking llama.cpp")

            status, detail = self._probe()
            if status == "ready":
                self._attached = True
                self._set("ready", "attached to running server")
                self._ready_at = time.time()
                self._start_watcher()
                return
            if status == "loading":
                self._attached = True
                self._set("starting", "attached, model loading")
                self._start_watcher()
                return
            if self._port_open():
                self._fail(
                    f"port {self.config.port} is in use by another process "
                    f"(health check said: {detail})"
                )
                return
            if not self.config.bin.is_file():
                self._fail(f"llama-server not found at {self.config.bin}")
                return
            if not self.config.model.is_file():
                self._fail(f"model not found at {self.config.model}")
                return
            if self.config.drafter is not None and not self.config.drafter.is_file():
                self._fail(f"drafter not found at {self.config.drafter}")
                return
            if self.config.no_spawn:
                self._fail("LLAMA_NO_SPAWN=1 and no healthy server is running")
                return
            self._spawn()
            if self._state != "error":
                self._start_watcher()

    def stop(self) -> None:
        with self._lifecycle:
            self._stop_event.set()
            watcher = self._watcher
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=3)
        with self._lifecycle:
            self._proc_backup()
            self._state = "stopped"
            self._detail = "stopped"
            self._attached = False

    def restart(self) -> None:
        self.stop()
        with self._lifecycle:
            self._proc = None
            self._owned = False
            self._attached = False
            self._error = None
        time.sleep(0.25)
        self.start()

    def snapshot(self) -> dict:
        with self._lock:
            proc = self._proc
            return {
                "state": self._state,
                "detail": self._detail,
                "error": self._error,
                "pid": proc.pid if proc is not None and proc.poll() is None else None,
                "attached": self._attached,
                "model": self.config.model.name,
                "llama_url": self.config.url,
                "elapsed_s": round(
                    (self._ready_at or time.time()) - (self._started_at or time.time()), 2
                ),
            }

    @property
    def base_url(self) -> str:
        return self.config.url

    # ----------------------------------------------------------------- process
    def _spawn(self) -> None:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.config.log_path, "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S").encode()
        self._log_file.write(b"\n===== " + stamp + b" starting llama-server =====\n")

        args = [
            str(self.config.bin),
            "-m",
            str(self.config.model),
            "--alias",
            self.config.alias,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "-c",
            str(self.config.ctx),
            "-ngl",
            str(self.config.ngl),
            "-fa",
            "on",
            "--jinja",
            "-ctk",
            "q8_0",
            "-ctv",
            "q8_0",
            "-np",
            "1",
            "--no-mmproj",
            "--temp",
            "1.0",
            "--top-p",
            "0.95",
        ]
        if self.config.drafter is not None:
            args += [
                "--spec-draft-model",
                str(self.config.drafter),
                "--spec-type",
                "draft-dflash",
                "--spec-draft-n-max",
                "7",
            ]

        try:
            self._proc = subprocess.Popen(
                args,
                cwd=str(ROOT),
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group, so stop() kills children too
            )
        except OSError as exc:
            self._close_log()
            self._fail(f"failed to spawn llama-server: {exc}")
            return
        self._owned = True
        self._attached = False
        self._set("starting", "spawning llama-server")

    def _proc_backup(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            self._close_log()
            return
        if self.config.keep or not self._owned:
            self._close_log()
            return
        self._set("stopped", "stopping llama-server")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._close_log()

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    # -------------------------------------------------------------------- watch
    def _start_watcher(self) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._watcher = threading.Thread(target=self._watch, name="llama-health", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._fail(f"llama-server exited with code {code}", log_tail=True)
                return

            status, detail = self._probe()
            if status == "ready":
                self._down_since = None
                if self._state != "ready":
                    self._ready_at = time.time()
                    self._set("ready", "model loaded")
            elif status == "loading":
                self._down_since = None
                self._set("starting", detail or "loading model")
            else:
                if self._down_since is None:
                    self._down_since = time.time()
                down_for = time.time() - self._down_since
                if (self._state == "ready" or self._attached) and down_for > 5:
                    self._fail("lost connection to llama-server")
                    return
                if (
                    self._started_at is not None
                    and self._state != "ready"
                    and time.time() - self._started_at > self.config.timeout_s
                ):
                    self._fail(
                        f"timed out after {int(self.config.timeout_s)}s waiting for /health",
                        log_tail=True,
                    )
                    return
            time.sleep(0.25)

    def _fail(self, message: str, log_tail: bool = False) -> None:
        if log_tail:
            tail = self._tail()
            if tail:
                message = f"{message}\n{tail}"
        self._error = message
        self._set("error", message.splitlines()[0])
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
            self._close_log()

    # --------------------------------------------------------------------- util
    def _set(self, state: State, detail: str) -> None:
        with self._lock:
            self._state = state
            self._detail = detail

    def _probe(self) -> tuple[str, str]:
        url = f"{self.config.url}/health"
        try:
            with _OPENER.open(url, timeout=1.5) as resp:
                raw = resp.read(4096)
                if resp.status == 200:
                    try:
                        body = json.loads(raw or b"{}")
                        return "ready", str(body.get("status", "ok"))
                    except json.JSONDecodeError:
                        return "ready", "ok"
        except urllib.error.HTTPError as exc:
            body = exc.read(4096)
            try:
                parsed = json.loads(body or b"{}")
                msg = (
                    parsed.get("error", {}).get("message")
                    if isinstance(parsed, dict)
                    else None
                ) or exc.reason
            except (json.JSONDecodeError, AttributeError):
                msg = exc.reason
            if exc.code == 503:
                return "loading", str(msg)
            return "error", f"HTTP {exc.code}: {msg}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return "down", str(getattr(exc, "reason", exc))
        return "error", "unexpected health response"

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((self.config.host, self.config.port)) == 0

    def _tail(self, limit: int = 12) -> str:
        path = self.config.log_path
        if not path.is_file():
            return ""
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                data = handle.read()
        except OSError:
            return ""
        lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
        return "\n".join(lines[-limit:])
