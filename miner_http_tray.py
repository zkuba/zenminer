#!/usr/bin/env python3
"""ZenMiner - CLI runner (updated)

Key fixes:
 - parse_threads_arg: supports "4,1,2", "1-4", "50%,25%,25%", single value -> duplicated
 - Uses user_activity.py (if available) to determine active/idle/locked
 - XMRigController: robust HTTP API usage (POST then GET fallback), retries, and only restarts xmrig if API fails
 - WalletRotator: try set_wallet_via_api before restarting xmrig
 - Web UI: /status and /control endpoints (start/stop/restart/set threads/rotate)
 - CLI: added --xmrig-http-port
"""

from __future__ import annotations
import argparse
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
import urllib.request
import urllib.error
import urllib.parse
import re
import ast
import hashlib
import logging.handlers


# Optional tray imports (only used if installed)
try:
    from PIL import Image, ImageDraw
    import pystray
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

# Optional flask for Web UI. If not installed, fallback to a tiny http server exposing JSON.
USE_FLASK = True
try:
    from flask import Flask, jsonify, request
except Exception:
    USE_FLASK = False

# Try import user_activity helper (if available)
try:
    import user_activity
    HAS_USER_ACTIVITY = True
except Exception:
    HAS_USER_ACTIVITY = False

# ----------------------------- Immutable defaults ---------------------------------
IMMUTABLE_DEV_WALLET = "87QdreCSsmVC3s9SzctnPuMuEfupAivfdHCstLCKvQQiWtkhfoJFWH1fUMxu3am7Lh9eRHfKPVf5Gi5aKemLqBNpTEEc6oj"
DEFAULT_POOL = "pool.supportxmr.com:3333"
DEFAULT_ROTATOR_CYCLE_MINUTES = 60.0
DEFAULT_ROTATOR_DONATE_PERCENT = 2.0
DEFAULT_HTTP_PORT = 5515
DEFAULT_HTTP_TOKEN = "zenminer"
DEFAULT_XMRIG_HTTP_PORT = 18080
LOG_DIR = Path(os.getcwd()) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------- Logging ------------------------------------------------

py_log_file = LOG_DIR / "zenminer.log"
handler = logging.handlers.RotatingFileHandler(
    str(py_log_file), maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    # will write to file; console/stderr not necessary for exe
    handlers=[handler],
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zenminer")

# ----------------------------- Utilities ---------------------------------------------


# -----------------------------
# HTTP helpers (robust)
# -----------------------------
def try_urlopen_json(url: str, data: Optional[bytes] = None, timeout: float = 2.0,
                     headers: Optional[Dict[str, str]] = None) -> Optional[bytes]:
    """Simple helper to call xmrig API or local endpoints. Returns response bytes on success.
       Supports passing headers (e.g. Authorization).
    """
    try:
        req = urllib.request.Request(url, data=data, headers=(headers or {}))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("URL open failed %s -> %s", url, e)
        return None


def try_urlopen_with_retry(url: str, data: Optional[bytes] = None, timeout: float = 2.0,
                           headers: Optional[Dict[str, str]] = None,
                           retries: int = 2, backoff: float = 1.0) -> Optional[bytes]:
    """Try urlopen multiple times with simple backoff."""
    last = None
    for i in range(retries):
        r = try_urlopen_json(url, data=data, timeout=timeout, headers=headers)
        if r is not None:
            return r
        last = r
        time.sleep(backoff * (i + 1))
    return None


def _build_xmrig_config_url(cfg: dict) -> str:
    """Return /1/config URL with token as query param if token exists (fallback)."""
    host = cfg.get("xmrig_api_host", cfg.get("http_host", "127.0.0.1"))
    port = int(cfg.get("xmrig_api_port", cfg.get(
        "http_port", DEFAULT_XMRIG_HTTP_PORT)))
    base = f"http://{host}:{port}/1/config"
    token = (cfg.get("xmrig_access_token") or cfg.get("api_token") or None)
    if token:
        return base + "?token=" + urllib.parse.quote_plus(str(token))
    return base


# ----------------------------- Thread hybrid parser ----------------------------------


def parse_threads_arg(raw: str) -> dict:
    """
    Interpret threads spec where order is: active,idle,logged_out
    Examples:
      "1,2,4" -> active=1, idle=2, logged_out=4
      "50%,25%,10%" -> floats as fractions
      "1" -> duplicates -> all three = 1
      "1-4" -> single int representative (use lower bound)
    Returns dict keys: 'active','idle','logged_out' with int or float (fraction)
    """
    if raw is None:
        raise ValueError("threads argument empty")
    raw = raw.strip()
    if not raw:
        raise ValueError("threads argument empty")
    if "-" in raw and "," not in raw and "%" not in raw:
        try:
            a, _ = raw.split("-", 1)
            v = int(a.strip())
            parts = [str(v)]
        except Exception:
            raise ValueError(f"Invalid range threads spec: {raw}")
    else:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    parsed = []
    for p in parts:
        if p.endswith('%'):
            try:
                pct = float(p[:-1])
                if pct < 0 or pct > 100:
                    raise ValueError
                parsed.append(pct / 100.0)
            except Exception:
                raise ValueError(f"Invalid percentage threads value: {p}")
        else:
            try:
                parsed.append(int(p))
            except Exception:
                raise ValueError(f"Invalid int threads value: {p}")
    while len(parsed) < 3:
        parsed.append(parsed[-1])
    # NOTE: mapping changed to active, idle, logged_out (user expectation)
    return {'active': parsed[0], 'idle': parsed[1], 'logged_out': parsed[2]}


# ----------------------------- XMRig process controller --------------------------------


class XMRigController:
    def __init__(self, xmrig_path: str, pool: str, wallet: str, worker: str,
                 xmrig_http_port: int = DEFAULT_XMRIG_HTTP_PORT, http_token: str = DEFAULT_HTTP_TOKEN,
                 donate_level: float = 0.0):
        self.api_failures = 0
        self.api_failure_threshold = 3
        self.xmrig_path = xmrig_path
        self.pool = pool
        self.wallet = wallet
        self.worker = worker
        self.xmrig_http_port = int(xmrig_http_port)
        self.http_token = http_token
        self.donate_level = donate_level
        self.proc: Optional[subprocess.Popen] = None
        self.proc_lock = threading.Lock()
        # runtime hints for UI
        self.threads_spec = None
        self.last_threads_target: Optional[int] = None
        self._xmrig_stdout = None
        self._xmrig_stderr = None

    def _build_cmd(self, threads: Optional[int] = None, extra_args: Optional[List[str]] = None) -> List[str]:
        cmd = [self.xmrig_path]
        cmd += ["-o", self.pool]
        cmd += ["-u", self.wallet]
        cmd += ["-p", self.worker]
        if threads is not None:
            cmd += [f"--threads={threads}"]
        cmd += ["--http-enabled", "--http-host=127.0.0.1", f"--http-port={self.xmrig_http_port}",
                f"--http-access-token={self.http_token}", "--http-no-restricted"]
        # set xmrig donate-level to 0 because we rotate wallets to include dev share
        cmd += ["--donate-level=0", "--no-huge-pages"]
        if extra_args:
            cmd += extra_args
        return cmd

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if getattr(self, 'http_token', None):
            headers['Authorization'] = f'Bearer {self.http_token}'
        return headers

        # --- add these helpers for interacting with XMRig HTTP API ---
    def query_summary(self, timeout: float = 1.0) -> Optional[dict]:
        """Return parsed JSON summary from XMRig HTTP API or None on failure."""
        url = f"http://127.0.0.1:{self.xmrig_http_port}/1/summary"
        try:
            raw = try_urlopen_json(url, timeout=timeout,
                                   headers=self._auth_headers())
            if raw is None:
                return None
            # try_urlopen_json may return bytes or already-parsed dict depending on file version
            if isinstance(raw, (bytes, bytearray)):
                return json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(raw, str):
                return json.loads(raw)
            if isinstance(raw, dict):
                return raw
            return None
        except Exception:
            return None

    def query_config(self, timeout: float = 2.0) -> Optional[dict]:
        """Return parsed JSON config from XMRig HTTP API or None on failure."""
        url = f"http://127.0.0.1:{self.xmrig_http_port}/1/config"
        try:
            raw = try_urlopen_json(url, timeout=timeout,
                                   headers=self._auth_headers())
            if raw is None:
                return None
            if isinstance(raw, (bytes, bytearray)):
                return json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(raw, str):
                return json.loads(raw)
            if isinstance(raw, dict):
                return raw
            return None
        except Exception:
            return None

    def set_wallet_via_api(self, wallet: str, worker: Optional[str] = None, timeout: float = 3.0) -> Tuple[bool, str]:
        """
        Update pools[0].user (wallet) and optionally pools[0].pass (worker) via PUT /1/config.
        Returns (ok, message).
        """
        cfg = self.query_config(timeout=timeout)
        if not cfg:
            return False, "failed to fetch xmrig config"
        try:
            if 'pools' not in cfg or not isinstance(cfg['pools'], list) or len(cfg['pools']) == 0:
                return False, "xmrig config has no pools[0]"
            if wallet:
                cfg['pools'][0]['user'] = wallet
            # --- after cfg['pools'][0]['user'] = wallet -- build worker name automatically ---
            try:
                # compute short hash of provided wallet (8 hex chars)
                wallet_hash = hashlib.sha256(
                    wallet.encode('utf-8')).hexdigest()[:8]

                # get version string (prefer controller attribute, fallback to global VERSION or 'unknown')
                version = getattr(self, 'version', None) or globals().get(
                    'VERSION', None) or 'unknown'
                # normalize version to safe characters and remove spaces
                version_s = re.sub(r'[^0-9A-Za-z._-]', '_',
                                   str(version)).strip('_')

                # form worker name: "<wallethash>_<version>"
                worker_auto = f"{wallet_hash}_{version_s}"

                # sanitize worker (allow only safe subset)
                # cap length to 64 chars
                worker_auto = re.sub(r'[^0-9A-Za-z._-]', '', worker_auto)[:64]

                # If a worker was explicitly passed to the method, prefer it (but still combine if desired).
                if worker is not None:
                    # explicit worker provided (user mode)
                    cfg['pools'][0]['pass'] = worker
                else:
                    # dev mode -> auto-generate worker
                    try:
                        wallet_hash = hashlib.sha256(
                            wallet.encode('utf-8')
                        ).hexdigest()[:8]

                        version = getattr(self, 'version', None) or globals().get(
                            'VERSION', 'dev')
                        version_s = re.sub(
                            r'[^0-9A-Za-z._-]', '_', str(version))[:16]

                        worker_auto = f"dev_{wallet_hash}_{version_s}"
                        cfg['pools'][0]['pass'] = worker_auto

                    except Exception:
                        cfg['pools'][0]['pass'] = "dev"
            except Exception as _e:
                # don't fail setting wallet for non-critical worker formatting errors
                cfg['pools'][0]['pass'] = worker if worker is not None else ''

            if worker is not None:
                # map worker to pool pass (common pattern in this app)
                cfg['pools'][0]['pass'] = worker

            data = json.dumps(cfg).encode('utf-8')
            headers = self._auth_headers()
            headers['Content-Type'] = 'application/json'
            # PUT full config
            r = try_urlopen_json(
                f"http://127.0.0.1:{self.xmrig_http_port}/1/config", data=data, timeout=timeout, headers=headers, )
            # try_urlopen_json returns parsed JSON on success; treat non-None as ok
            if r is None:
                return False, "PUT /1/config returned no response"

                # persist applied worker into controller state so future restarts use correct worker
            try:
                applied = cfg['pools'][0].get('pass', '')
                if applied:
                    self.worker = applied
            except Exception:
                pass

            return True, "ok"
        except Exception as e:
            return False, f"exception: {e}"

    def set_threads_via_api(self, threads: int, timeout: float = 3.0) -> Tuple[bool, str]:
        """
        For XMRig >=6: change threads by modifying returned /1/config CPU fields then PUT /1/config.
        Returns (ok, message).
        """
        cfg = self.query_config(timeout=timeout)
        if not cfg:
            return False, "failed to fetch xmrig config"
        try:
            changed = False
            cpu = cfg.get('cpu') or {}
            if isinstance(cpu, dict):
                if 'threads' in cpu:
                    cpu['threads'] = int(threads)
                    changed = True
                # also support the '*' / profile fields
                if isinstance(cpu.get('*'), dict):
                    cpu['*']['threads'] = int(threads)
                    changed = True
                if 'profile' in cpu and isinstance(cpu['profile'], dict):
                    cpu['profile']['threads'] = int(threads)
                    changed = True
            if not changed:
                # try fallback: top-level 'cpu' replacement
                cfg['cpu'] = cfg.get('cpu', {})
                cfg['cpu']['*'] = cfg['cpu'].get('*', {})
                cfg['cpu']['*']['threads'] = int(threads)
            data = json.dumps(cfg).encode('utf-8')
            headers = self._auth_headers()
            headers['Content-Type'] = 'application/json'
            r = try_urlopen_json(
                f"http://127.0.0.1:{self.xmrig_http_port}/1/config", data=data, timeout=timeout, headers=headers)
            if r is None:
                return False, "PUT /1/config returned no response"
            return True, "ok"
        except Exception as e:
            return False, f"exception: {e}"

    def start(self, threads: Optional[int] = None) -> Tuple[bool, Optional[int]]:
        with self.proc_lock:
            if self.proc and self.proc.poll() is None:
                log.info("xmrig already running pid=%s", self.proc.pid)
                return True, self.proc.pid

            # ensure threads fallback
            if threads is None:
                threads = getattr(self, 'last_threads_target', None)
            if threads is None:
                threads = 1

            cmd = self._build_cmd(threads=threads)
            log.info("Starting xmrig: %s", " ".join(cmd))

            # prepare logs dir and file handles BEFORE starting process
            logs_dir = Path(os.getcwd()) / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = logs_dir / "xmrig_stdout.log"
            stderr_path = logs_dir / "xmrig_stderr.log"

            # close any previous handles if present
            try:
                if getattr(self, "_xmrig_stdout", None):
                    try:
                        self._xmrig_stdout.close()
                    except Exception:
                        pass
                    self._xmrig_stdout = None
                if getattr(self, "_xmrig_stderr", None):
                    try:
                        self._xmrig_stderr.close()
                    except Exception:
                        pass
                    self._xmrig_stderr = None
            except Exception:
                # defensive: ignore cleanup errors
                pass

            # open new handles and keep them on self so stop() can close them later
            try:
                self._xmrig_stdout = open(str(stdout_path), "ab")
                self._xmrig_stderr = open(str(stderr_path), "ab")
            except Exception as e:
                log.exception("Failed to open xmrig log files: %s", e)
                # ensure no partial handles remain
                try:
                    if getattr(self, "_xmrig_stdout", None):
                        self._xmrig_stdout.close()
                        self._xmrig_stdout = None
                except Exception:
                    pass
                try:
                    if getattr(self, "_xmrig_stderr", None):
                        self._xmrig_stderr.close()
                        self._xmrig_stderr = None
                except Exception:
                    pass
                return False, None

            try:
                popen_kwargs = {
                    "stdout": self._xmrig_stdout,
                    "stderr": self._xmrig_stderr,
                    "close_fds": False,
                }

                # --- Windows: hide console window (KROK 3) ---
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = subprocess.SW_HIDE

                    popen_kwargs["startupinfo"] = startupinfo
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

                self.proc = subprocess.Popen(
                    cmd,
                    **popen_kwargs
                )

                log.info("Started xmrig (pid=%s)",
                         getattr(self.proc, "pid", None))
                # remember target
                self.last_threads_target = threads

                # wait briefly for HTTP API readiness (non-fatal)
                max_wait = 8.0
                start_ts = time.time()
                while time.time() - start_ts < max_wait:
                    try:
                        s = try_urlopen_json(
                            f"http://127.0.0.1:{self.xmrig_http_port}/1/summary",
                            timeout=1.0,
                            headers=self._auth_headers())
                        if s:
                            log.info("xmrig HTTP API is ready")
                            break
                    except Exception:
                        pass
                    time.sleep(0.4)
                else:
                    log.warning(
                        "xmrig HTTP API not ready after %.1fs (port=%d). Will continue but API ops may fail.",
                        max_wait, self.xmrig_http_port)

                return True, self.proc.pid
            except Exception as e:
                log.exception("Failed to start xmrig: %s", e)
                # ensure no stale proc ref and cleanup file handles
                try:
                    if self.proc:
                        try:
                            self.proc.kill()
                        except Exception:
                            pass
                except Exception:
                    pass
                self.proc = None
                try:
                    if getattr(self, "_xmrig_stdout", None):
                        try:
                            self._xmrig_stdout.close()
                        except Exception:
                            pass
                        self._xmrig_stdout = None
                    if getattr(self, "_xmrig_stderr", None):
                        try:
                            self._xmrig_stderr.close()
                        except Exception:
                            pass
                        self._xmrig_stderr = None
                except Exception:
                    pass
                return False, None

    def stop(self, graceful: bool = True) -> bool:
        with self.proc_lock:
            if not self.proc:
                log.info("xmrig not running")
                return True
            try:
                if graceful:
                    try:
                        url = f"http://127.0.0.1:{self.xmrig_http_port}/1/stop"
                        try_urlopen_with_retry(
                            url, timeout=1.0, retries=1, headers=self._auth_headers())

                    except Exception:
                        pass
                if self.proc.poll() is None:
                    try:
                        self.proc.terminate()
                        self.proc.wait(timeout=3)
                    except Exception:
                        try:
                            self.proc.kill()
                        except Exception:
                            pass
                log.info("xmrig stopped")
                self.proc = None
                try:
                    if getattr(self, "_xmrig_stdout", None):
                        self._xmrig_stdout.close()
                        self._xmrig_stdout = None
                    if getattr(self, "_xmrig_stderr", None):
                        self._xmrig_stderr.close()
                        self._xmrig_stderr = None
                except Exception:
                    pass
                return True
            except Exception as e:
                log.exception("Error stopping xmrig: %s", e)
                try:
                    if self.proc:
                        self.proc.kill()
                except Exception:
                    pass
                self.proc = None
                return False

    def restart(self, threads: Optional[int] = None) -> dict:
        log.info("Restart requested (threads=%s)", threads)
        ok = self.stop(graceful=True)
        if not ok:
            log.warning("Stop failed, attempting continue start")
        start_ok, pid = self.start(threads=threads)
        return {'status': 'started' if start_ok else 'failed', 'pid': pid, 'threads': threads}

    def _http_post_then_get(self, path: str, params: Dict[str, str], timeout: float = 1.0) -> Optional[bytes]:
        """
        Try POST first (with proper headers), then fallback to GET.
        Ensure Authorization header or token query param is passed.
        """
        base = f"http://127.0.0.1:{self.xmrig_http_port}{path}"
        headers = self._auth_headers()

        # Ensure token param if not in headers
        if 'token' not in params and self.http_token and 'Authorization' not in headers:
            params['token'] = self.http_token

        data = urllib.parse.urlencode(params).encode(
            'utf-8') if params else None

        # try POST with headers
        try:
            resp = try_urlopen_with_retry(
                base, data=data, timeout=timeout, retries=2, headers=headers)
            if resp:
                return resp
        except Exception:
            pass

        # fallback to GET (params in query)
        try:
            url2 = base + ("?" + urllib.parse.urlencode(params)
                           if params else "")
            resp2 = try_urlopen_with_retry(
                url2, timeout=timeout, retries=2, headers=headers)
            if resp2:
                return resp2
        except Exception:
            pass
        return None

    def _http_put_json(self, url, obj, timeout=3.0):
        """
        Put JSON to xmrig API, making sure Authorization / token headers are sent.
        Replaces previous implementation which lost headers by calling try_urlopen_with_retry(req.full_url,...).
        """
        try:
            import json
            data = json.dumps(obj).encode('utf-8')

            # build headers: Content-Type plus Authorization (if http_token configured)
            headers = {'Content-Type': 'application/json'}
            if getattr(self, "http_token", None):
                headers['Authorization'] = f"Bearer {self.http_token}"

            # try sending with the helper that accepts headers
            from urllib.parse import urlparse, parse_qs
            # if url already contains ?token=... we still send header — header preferred
            r = try_urlopen_json(
                url, data=data, timeout=timeout, headers=headers)
            return r is not None
        except Exception as e:
            log.debug("HTTP PUT JSON failed %s -> %s", url, e)
            return False

    # ------------------------------------------------------------------
    #  NOWE FUNKCJE API – stabilne jak w wersji TRAY (GET+PUT CONFIG)
    # ------------------------------------------------------------------

    def _build_config_url(self):
        base = f"http://127.0.0.1:{self.xmrig_http_port}/1/config"
        if getattr(self, "http_token", None):
            import urllib.parse
            return base + f"?token={urllib.parse.quote(str(self.http_token))}"
        return base

    def _http_get_json(self, url, timeout=2.0):
        """
        GET JSON from xmrig API — prefer header-based Authorization but fall back to no-query URL.
        Returns parsed JSON (dict/list) or None.
        """
        try:
            # Try with header (preferred)
            b = try_urlopen_with_retry(
                url, timeout=timeout, retries=2, headers=self._auth_headers())
            if not b:
                # Fallback: sometimes XMRig expects header-only (no ?token) or vice-versa — try removing ?token=... and retry
                try:
                    import re
                    base_no_q = re.sub(r"\?token=.*$", "", url)
                    b = try_urlopen_with_retry(
                        base_no_q, timeout=timeout, retries=2, headers=self._auth_headers())
                except Exception:
                    b = None

            if not b:
                return None

            import json
            return json.loads(b.decode('utf-8', errors='replace'))
        except Exception as e:
            log.debug("HTTP GET JSON failed %s -> %s", url, e)
            return None


# ----------------------------- Wallet rotator ---------------------------------------


class WalletRotator(threading.Thread):
    def __init__(self, controller, user_wallet, cycle_minutes, dev_percent, worker,
                 dev_slice_minutes: int = 15, max_cycle_hours: float = 6.0, min_user_seconds: int = 60):
        super().__init__(daemon=True)
        self.controller = controller
        self.user_wallet = user_wallet
        self.dev_wallet = IMMUTABLE_DEV_WALLET  # or passed override later
        # legacy; still kept but _compute_periods uses dev_slice+percent
        self.cycle_minutes = float(cycle_minutes)
        self.dev_percent = float(dev_percent)
        self.worker = worker
        self.dev_slice_minutes = float(dev_slice_minutes)
        self.max_cycle_hours = float(max_cycle_hours)
        self.min_user_seconds = int(min_user_seconds)
        self._stop = threading.Event()
        self._current_owner = None
        self._next_switch_ts = None
        # compute initial periods
        self._compute_periods()

    def _compute_periods(self):
        """
        Compute dev_seconds and user_seconds with automatic reduction of dev slice
        so that full cycle <= max_cycle_hours (in hours).

        Rules:
        - self.dev_percent is donate percent D (0..100)
        - self.dev_slice_minutes is the preferred dev slice S_base (minutes)
        - self.max_cycle_hours is maximum allowed total cycle (hours)
        - final S = min(S_base, max(1, floor(max_cycle_minutes * D / 100)))
        - total_minutes = S * 100 / D  (if D > 0)
        - dev_seconds = S * 60, user_seconds = total_seconds - dev_seconds (clamped >= min_user_seconds)
        - If D == 0 => dev_seconds = 0, user_seconds = min(max_total_seconds, some fallback)
        """
        # normalize inputs / defaults
        raw_D = float(max(0.0, min(100.0, getattr(self, 'dev_percent', 0.0))))
        # enforce minimum 1% (CLI clamps earlier, but guard here too)
        D = raw_D if raw_D >= 1.0 else 1.0
        if raw_D < 1.0:
            log.warning(
                "WalletRotator: dev_percent %.6f%% < 1%% -> using 1%% to respect minimum", raw_D)

        # preferred dev slice minutes
        S_base = float(max(1.0, getattr(self, 'dev_slice_minutes', 15.0)))
        min_user_seconds = int(getattr(self, 'min_user_seconds', 60))
        # NOTE: default 6 hours as requested
        max_cycle_hours = float(getattr(self, 'max_cycle_hours', 6.0))

        # convert caps
        max_cycle_minutes = max_cycle_hours * 60.0

        if D <= 0.0:
            # dev disabled -> no switching; user gets entire cycle (set large but limited by max_cycle_minutes)
            dev_seconds = 0
            # user can have up to cap
            total_minutes = max(1.0, max_cycle_minutes)
            total_seconds = int(round(total_minutes * 60.0))
            user_seconds = max(min_user_seconds, total_seconds)
            self.dev_seconds = int(dev_seconds)
            self.user_seconds = int(user_seconds)
            self.cycle_total_seconds = int(total_seconds)
            return

        # compute S_allowed such that total_minutes = S * 100 / D <= max_cycle_minutes
        # => S <= max_cycle_minutes * D / 100
        S_allowed = (max_cycle_minutes * D) / 100.0
        # ensure at least 1 minute
        S_allowed_int = max(1, int(S_allowed))
        S = min(S_base, S_allowed_int)

        # final total & slices
        total_minutes = (S * 100.0) / D if D > 0 else max_cycle_minutes
        # safety cap: if computed total > max_cycle_minutes, clamp and adjust S downwards if possible
        if total_minutes > max_cycle_minutes:
            # recompute S from cap
            S = max(1, int((max_cycle_minutes * D) / 100.0))
            total_minutes = (S * 100.0) / D if D > 0 else max_cycle_minutes
        # compute seconds
        dev_seconds = int(round(S * 60.0))
        total_seconds = int(round(total_minutes * 60.0))
        user_seconds = total_seconds - dev_seconds

        # ensure user_seconds respects minimum
        if user_seconds < min_user_seconds:
            # reduce dev_seconds first
            deficit = min_user_seconds - user_seconds
            dev_seconds = max(0, dev_seconds - deficit)
            user_seconds = total_seconds - dev_seconds
            # if still too small, expand total_seconds up to cap
            if user_seconds < min_user_seconds:
                needed = min_user_seconds - user_seconds
                # try to increase total_seconds but not exceed cap
                new_total_seconds = min(
                    int(max_cycle_minutes * 60.0), total_seconds + needed)
                total_seconds = new_total_seconds
                user_seconds = total_seconds - dev_seconds

        # final safety checks
        if total_seconds <= 0:
            total_seconds = dev_seconds + max(min_user_seconds, 60)
            user_seconds = total_seconds - dev_seconds

        self.dev_seconds = int(dev_seconds)
        self.user_seconds = int(user_seconds)
        self.cycle_total_seconds = int(total_seconds)

    def run(self):
        log.info("Wallet rotator running: cycle_minutes=%.2f dev_fee_percent=%.3f",
                 self.cycle_minutes, self.dev_percent)
        if not self.user_wallet:
            self._current_owner = 'dev'
            self.controller.wallet = self.dev_wallet
            log.info("No user wallet provided; holding dev wallet continuously.")
            while not self._stop.is_set():
                time.sleep(1.0)
            log.info("Wallet rotator stopping (dev-only mode)")
            return

        # compute periods already done in __init__
        # first user slice: max 15 min
        first_user_short_seconds = min(self.user_seconds, 15 * 60)
        first_cycle = True

        while not self._stop.is_set():
            # user slice (first shortened, then normal)
            self._set_owner('user')
            use_seconds = first_user_short_seconds if first_cycle else self.user_seconds
            self._set_next_switch(use_seconds)
            if self._wait_or_stop(use_seconds):
                break
            first_cycle = False

            # dev slice (normal)
            self._set_owner('dev')
            self._set_next_switch(self.dev_seconds)
            if self._wait_or_stop(self.dev_seconds):
                break

        log.info("Wallet rotator stopping")

    def _set_next_switch(self, seconds: float):
        self._next_switch_ts = time.time() + seconds

    def get_status(self):
        """Return dict with current owner and seconds remaining (or None)."""
        now = time.time()
        remaining = None
        if self._next_switch_ts:
            remaining = max(0, int(self._next_switch_ts - now))
        return {'current_owner': self._current_owner, 'seconds_to_next': remaining}

    def _set_owner(self, owner: str):
        if owner == self._current_owner:
            return
        wallet = self.dev_wallet if owner == 'dev' else self.user_wallet
        # first try API
        if owner == 'dev':
            ok, msg = self.controller.set_wallet_via_api(wallet, worker=None)
        else:
            ok, msg = self.controller.set_wallet_via_api(
                wallet, worker=self.worker)
        if ok:
            log.info("Rotator: switched to %s wallet via API", owner)
            self.controller.wallet = wallet
        else:
            log.info(
                "Rotator: API wallet switch failed, falling back to restart to apply %s wallet", owner)
            self.controller.wallet = wallet
            self.controller.restart()
        self._current_owner = owner

    def _wait_or_stop(self, seconds: float) -> bool:
        start = time.time()
        while time.time() - start < seconds:
            if self._stop.is_set():
                return True
            time.sleep(0.5)
        return False

    def stop(self):
        self._stop.set()


# ----------------------------- Adaptive thread controller (light) --------------------


class AdaptiveThreadController(threading.Thread):
    def __init__(self, controller: XMRigController, threads_map: dict, idle_after_min: float, logout_after_min: float, debounce_sec: float):
        super().__init__(daemon=True)
        self.controller = controller
        self.threads_map = threads_map
        self.state = None
        self._candidate_state = None
        self._candidate_since = None
        self._stop = threading.Event()
        self.idle_after_sec = float(idle_after_min) * 60.0
        self.logout_after_sec = float(logout_after_min) * 60.0
        self.state_debounce_sec = float(debounce_sec)
        self.last_input = time.time()
        self.idle_threshold = 60.0  # seconds to consider idle
        self._state_candidates = {}
        self._debounce_required = 5  # wymagaj 2 kolejnych próbek, zmień na 3 jeśli trzeba

    def set_user_input(self):
        self.last_input = time.time()

    def get_state(self) -> str:
        """
        State decision based on:
        - Windows lock (highest priority)
        - idle time thresholds
        No debounce here — debounce handled in run()
        """
        try:
            # 1) Windows lock detection (highest priority)
            locked = False
            if HAS_USER_ACTIVITY:
                try:
                    locked = user_activity.is_workstation_locked()
                except Exception as e:
                    log.debug("is_workstation_locked() failed: %s", e)

            # 2) Idle time (seconds since last input)
            try:
                idle_sec = (
                    user_activity.get_last_input_seconds()
                    if HAS_USER_ACTIVITY
                    else (time.time() - self.last_input)
                )
            except Exception as e:
                log.debug("get_last_input_seconds() failed: %s", e)
                idle_sec = time.time() - self.last_input

            log.debug(
                "state eval: locked=%s idle_sec=%.1f idle_after=%.1f logout_after=%.1f",
                locked,
                idle_sec,
                self.idle_after_sec,
                self.logout_after_sec
            )

            # 3) Decision tree
            if locked:
                return 'logged_out'

            if idle_sec >= self.logout_after_sec:
                return 'logged_out'

            if idle_sec >= self.idle_after_sec:
                return 'idle'

            return 'active'

        except Exception as e:
            log.debug("get_state exception fallback: %s", e)
            return 'active'

    def run(self):
        log.info("AdaptiveThreadController started")

        # ensure debounce structures exist (safe if __init__ wasn't changed)
        if not hasattr(self, "_state_candidates"):
            self._state_candidates = {}
        if not hasattr(self, "_debounce_required"):
            self._debounce_required = 2
        if not hasattr(self, "_logged_out_debounce"):
            self._logged_out_debounce = 3

        try:
            while not self._stop.is_set():
                st = self.get_state()

                now = time.time()
                st = self.get_state()

                if st != self.state:
                    # start or continue debounce window
                    if self._candidate_state != st:
                        self._candidate_state = st
                        self._candidate_since = now
                        log.debug(
                            "Adaptive candidate state: %s (starting debounce)", st
                        )
                    else:
                        elapsed = now - (self._candidate_since or now)
                        if elapsed >= self.state_debounce_sec:
                            log.info(
                                "Adaptive state change: %s -> %s (debounced %.1fs)",
                                self.state,
                                st,
                                elapsed
                            )
                            self.state = st
                            self._candidate_state = None
                            self._candidate_since = None

                            # determine target threads
                            target = self.threads_map.get(st)
                            if isinstance(target, float):
                                import multiprocessing
                                cpu = multiprocessing.cpu_count()
                                threads = max(1, int(round(target * cpu)))
                            else:
                                threads = int(target)

                            # apply threads
                            if self.controller.set_threads_via_api(threads):
                                log.info(
                                    "Adaptive threads set via API -> %d", threads
                                )
                            else:
                                log.info(
                                    "Adaptive threads API failed, restarting xmrig to apply %d threads",
                                    threads
                                )
                                self.controller.restart(threads=threads)
                else:
                    # state stable — reset candidate
                    if self._candidate_state is not None:
                        self._candidate_state = None
                        self._candidate_since = None

                # faster loop to react to lock/unlock quickly
                time.sleep(1.0)
        finally:
            log.info("AdaptiveThreadController stopping")

    def stop(self):
        self._stop.set()

# ----------------------------- Web UI (Flask or basic) --------------------------------


class WebUI:
    def __init__(self, controller: XMRigController, adaptive: AdaptiveThreadController, rotator: Optional[WalletRotator], port: int, http_token: str, http_debug: bool = False):
        self.controller = controller
        self.adaptive = adaptive
        self.rotator = rotator
        self.port = port
        # http_debug comes from CLI flag --http-debug; default False
        self.http_debug = bool(http_debug)
        self.http_token = http_token
        self._server_thread = None

        if USE_FLASK:
            self._setup_flask()
        else:
            self._setup_basic()

    def _setup_flask(self):
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        WEB_DIR = os.path.join(BASE_DIR, "web")

        app = Flask(
            __name__,
            static_folder=WEB_DIR,
            static_url_path=""
        )
        self.app = app

        # --- debug helpers: log incoming request headers + body (temporary, controlled by flag) ---
       # after creating self.app
        if getattr(self, 'http_debug', False):
            import logging
            logger = logging.getLogger("zenminer.http_dbg")
            logger.setLevel(logging.ERROR)
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter(
                "%(asctime)s [DEBUG] %(message)s"))
            logger.addHandler(ch)

            @self.app.before_request
            def log_request_info():
                try:
                    body = request.get_data(cache=True, as_text=True)[:16384]
                    logger.debug("INCOMING %s %s token=%s Headers=%s Body=%s",
                                 request.method, request.path,
                                 bool(_get_request_token(request)),
                                 dict(request.headers), body)
                except Exception as e:
                    logger.debug("log_request_info error: %s", e)

        # --- robust auth helper: accept X-Auth-Token, Authorization: Bearer <token>, or ?http_token=... ---
        def _get_request_token(request):
            # 1) header X-Auth-Token
            token = request.headers.get('X-Auth-Token')
            if token:
                return token

            # 2) Authorization: Bearer <token>  (or plain token)
            auth = request.headers.get('Authorization')
            if auth:
                parts = auth.split()
                if len(parts) == 1:
                    return parts[0]
                return parts[-1]

            # 3) query param fallback: ?http_token=xyz or ?token=xyz
            q = request.args.get('http_token') or request.args.get('token')
            if q:
                return q

            return None

        def auth_required(request):
            # returns True if request has a token matching self.http_token (or if no token configured -> True)
            if not getattr(self, 'http_token', None):
                return True   # no token configured => open API
            token = _get_request_token(request)
            return token == self.http_token

        from flask import send_file, jsonify, request

        # --- Log lines endpoint: returns last N lines of zenminer.log ---
        @app.route("/log_lines")
        def log_lines():
            # optional ?lines=N
            try:
                nl = int(request.args.get("lines") or 200)
            except Exception:
                nl = 200
            path = Path(os.getcwd()) / "logs" / "zenminer.log"
            if not path.exists():
                return jsonify({"ok": False, "error": "log not found"}), 404
            try:
                # read safely last nl lines (efficient-ish)
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    filesize = f.tell()
                    blocksize = 1024
                    data = bytearray()
                    lines_found = 0
                    pos = filesize
                    while pos > 0 and lines_found <= nl:
                        read_size = blocksize if pos - blocksize > 0 else pos
                        pos -= read_size
                        f.seek(pos)
                        chunk = f.read(read_size)
                        data = chunk + data
                        lines_found = data.count(b"\n")
                        if pos == 0:
                            break
                    text = data.decode(
                        "utf-8", errors="replace").splitlines()[-nl:]
                return jsonify({"ok": True, "lines": text})
            except Exception as e:
                log.exception("log_lines read failed: %s", e)
                return jsonify({"ok": False, "error": str(e)}), 500

        # --- Download the main app log (attachment) ---
        @app.route("/download_log")
        def download_log():
            path = Path(os.getcwd()) / "logs" / "zenminer.log"
            if not path.exists():
                return ("Log not found", 404)
            try:
                return send_file(str(path), mimetype="text/plain", as_attachment=True,
                                 download_name="zenminer.log")
            except Exception as e:
                log.exception("download_log failed: %s", e)
                return ("Internal error", 500)

        # --- Download xmrig stdout (first available) ---
        @app.route("/download_xmrig")
        def download_xmrig():
            base = Path(os.getcwd()) / "logs"
            candidates = [base / "xmrig_stdout.log", base / "xmrig_stderr.log"]
            for p in candidates:
                if p.exists():
                    try:
                        return send_file(str(p), mimetype="text/plain", as_attachment=True,
                                         download_name=p.name)
                    except Exception as e:
                        log.exception("download_xmrig failed: %s", e)
                        return ("Internal error", 500)
            return ("XMRig logs not found", 404)

        @app.route('/status')
        def status():
            # running state
            running = self.controller.proc is not None and self.controller.proc.poll() is None

            # defaults (safe for UI)
            runtime_summary = None
            hashrate_info = {'total': 0}
            shares_info = {}

            try:
                s = self.controller.query_summary()
                runtime_summary = None
                hashrate_info = {'total': 0}
                shares_info = {}

                if s:
                    try:
                        # s may be bytes or str. normalize to str:
                        if isinstance(s, (bytes, bytearray)):
                            txt = s.decode('utf-8', errors='replace')
                        else:
                            txt = str(s)

                        parsed = None
                        # Try JSON first (most correct)
                        try:
                            parsed = json.loads(txt)
                        except Exception:
                            # If it fails, maybe it's Python repr (single quotes, True/None)
                            try:
                                parsed = ast.literal_eval(txt)
                            except Exception:
                                parsed = None

                        if parsed and isinstance(parsed, dict):
                            runtime_summary = parsed

                            # helper: pick last numeric from nested structures/lists
                            def pick_last_numeric(x):
                                if x is None:
                                    return None
                                if isinstance(x, (int, float)):
                                    return x
                                if isinstance(x, list):
                                    for v in reversed(x):
                                        if isinstance(v, (int, float)):
                                            return v
                                    return None
                                if isinstance(x, dict):
                                    if 'total' in x:
                                        return pick_last_numeric(x['total'])
                                    for k in ('hashrate', 'hs', 'h', 'speed'):
                                        if k in x:
                                            nv = pick_last_numeric(x[k])
                                            if nv is not None:
                                                return nv
                                    for v in x.values():
                                        nv = pick_last_numeric(v)
                                        if nv is not None:
                                            return nv
                                    return None
                                return None

                            # extract hashrate candidate
                            hr_candidate = None
                            if 'hashrate' in parsed:
                                hr_candidate = parsed.get('hashrate')
                            elif 'speed' in parsed:
                                hr_candidate = parsed.get('speed')
                            elif 'results' in parsed and isinstance(parsed['results'], dict):
                                hr_candidate = parsed['results'].get(
                                    'hashrate', None)

                            num = pick_last_numeric(
                                hr_candidate if hr_candidate is not None else parsed)
                            hashrate_info = {
                                'total': num if num is not None else 0}

                            # SHARES
                            res = parsed.get('results') or {}
                            if isinstance(res, dict) and ('shares_good' in res or 'shares_total' in res):
                                shares_info = {
                                    'accepted': res.get('shares_good', res.get('accepted', 0)),
                                    'total': res.get('shares_total', res.get('total', 0)),
                                    'rejected': res.get('shares_rejected', res.get('rejected', 0))
                                }
                            elif 'connection' in parsed and isinstance(parsed['connection'], dict):
                                conn = parsed['connection']
                                shares_info = {
                                    'accepted': conn.get('accepted', 0),
                                    'rejected': conn.get('rejected', 0)
                                }

                        else:
                            # parsed failed -> keep safe defaults
                            runtime_summary = None
                            hashrate_info = {'total': 0}
                            shares_info = {}

                    except Exception as e:
                        log.debug(
                            "status: parsing summary general exception: %s", e)
                        runtime_summary = None
                        hashrate_info = {'total': 0}
                        shares_info = {}

            except Exception as e:
                log.debug("status: query_summary failed: %s", e)
                runtime_summary = None

            rot_info = (self.rotator.get_status() if self.rotator else {
                        'current_owner': None, 'seconds_to_next': None})
            # --- try to extract uptime (seconds) from runtime_summary if present ---
            uptime_val = None
            try:
                if runtime_summary and isinstance(runtime_summary, dict):
                    # common places for uptime in various xmrig summary variants
                    # try top-level keys first
                    for key in ('uptime', 'time', 'seconds', 'uptime_seconds', 'running_time'):
                        if key in runtime_summary:
                            uptime_val = runtime_summary.get(key)
                            break
                    # try nested 'results' or other nested dicts
                    if uptime_val is None:
                        res = runtime_summary.get('results') if isinstance(
                            runtime_summary.get('results'), dict) else None
                        if res:
                            for key in ('uptime', 'time', 'seconds', 'uptime_seconds'):
                                if key in res:
                                    uptime_val = res.get(key)
                                    break
                    # normalize simple string like "123" or "123s" -> int seconds
                    if isinstance(uptime_val, str):
                        try:
                            uptime_val = int(uptime_val.rstrip('s'))
                        except Exception:
                            # leave string as-is (frontend will handle non-numeric)
                            pass
                    # if it's float, cast to int seconds
                    if isinstance(uptime_val, float):
                        uptime_val = int(uptime_val)
            except Exception:
                uptime_val = None

            runtime_obj = {
                'pid': self.controller.proc.pid if self.controller.proc else None,
                'summary': runtime_summary,
                'uptime': uptime_val
            }

            return jsonify({
                'xmrig': {'status': 'started' if running else 'stopped', 'threads': self.controller.last_threads_target},
                'runtime_wallet': self.controller.wallet,
                'config': {'pool': self.controller.pool, 'threads': self.controller.threads_spec},
                'runtime_worker': self.controller.worker,
                'runtime': runtime_obj,
                'hashrate': hashrate_info,
                'shares': shares_info,
                'rotator': rot_info
            })

        @app.route('/control', methods=['POST'])
        def control():
            if not auth_required(request):
                return jsonify({'ok': False, 'error': 'unauthorized'}), 401
            try:
                payload = request.get_json(force=True)
                action = payload.get('action')
                # START
                if action == 'start':
                    # allow overrides
                    worker = payload.get('worker')
                    pool = payload.get('pool')
                    threads_spec = payload.get('threads')
                    if worker:
                        self.controller.worker = worker
                    if pool:
                        self.controller.pool = pool
                    if threads_spec:
                        self.controller.threads_spec = threads_spec
                    # decide initial threads (active)
                    threads_val = None
                    if threads_spec:
                        tm = parse_threads_arg(threads_spec)
                        target = tm.get('active')
                        if isinstance(target, float):
                            import multiprocessing
                            threads_val = max(
                                1, int(round(target * multiprocessing.cpu_count())))
                        else:
                            threads_val = int(target)
                    ok, pid = self.controller.start(threads=threads_val)
                    return jsonify({'ok': ok, 'pid': pid})
                # STOP
                elif action == 'stop':
                    ok = self.controller.stop()
                    return jsonify({'ok': ok})
                # RESTART
                elif action == 'restart':
                    threads_spec = payload.get(
                        'threads') or self.controller.threads_spec
                    threads_val = None
                    if threads_spec:
                        tm = parse_threads_arg(threads_spec)
                        target = tm.get('active')
                        if isinstance(target, float):
                            import multiprocessing
                            threads_val = max(
                                1, int(round(target * multiprocessing.cpu_count())))
                        else:
                            threads_val = int(target)
                    res = self.controller.restart(threads=threads_val)
                    return jsonify({'ok': True, 'result': res})
                # set threads directly (manual)
                elif action == 'set_threads':
                    t = payload.get('threads')
                    if t is None:
                        return jsonify({'ok': False, 'error': 'no threads provided'})
                    try:
                        threads_int = int(t)
                    except Exception:
                        return jsonify({'ok': False, 'error': 'invalid threads'})
                    if self.controller.set_threads_via_api(threads_int):
                        return jsonify({'ok': True, 'method': 'api', 'threads': threads_int})
                    else:
                        res = self.controller.restart(threads=threads_int)
                        return jsonify({'ok': True, 'method': 'restart', 'result': res})
                # rotate now
                elif action == 'rotate_now':
                    if not self.rotator:
                        return jsonify({'status': 'no_rotator'})
                    current = getattr(self.rotator, '_current_owner', 'user')
                    new = 'dev' if current == 'user' else 'user'
                    threading.Thread(target=self.rotator._set_owner, args=(
                        new,), daemon=True).start()
                    return jsonify({'ok': True, 'new': new})
                else:
                    return jsonify({'ok': False, 'error': 'unknown action'})
            except Exception as e:
                # Log full exception with stacktrace so we can debug the 500.
                log.exception("Unhandled exception in /control: %s", e)
                return jsonify({'ok': False, 'error': 'internal server error', 'detail': str(e)}), 500

        # serve the static UI file if present (optional)
        from flask import send_file

        @app.route('/ui/')
        def ui_root():
            ui_path = Path(os.getcwd()) / "zenminer_web_ui.html"
            if ui_path.exists():
                try:
                    return send_file(str(ui_path))
                except Exception as e:
                    log.exception("Failed to send UI file: %s", e)
                    return ("Internal server error serving UI file.", 500)
            return ("ZenMiner Web UI not found. Place zenminer_web_ui.html next to this script.", 404)

        @app.route("/")
        def index():
            return app.send_static_file("index.html")

        self._flask_app = app

    def _run_flask(self):
        # Use threaded to allow concurrent handling
        self._flask_app.run(host='127.0.0.1', port=self.port, threaded=True)

    def _setup_basic(self):
        # minimal HTTP server (not production) exposing /status only
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/status'):
                    running = self.server.controller.proc is not None and self.server.controller.proc.poll() is None
                    payload = {"running": running, "pid": self.server.controller.proc.pid if self.server.controller.proc else None,
                               "worker": self.server.controller.worker}
                    body = json.dumps(payload).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
        self._basic_handler = Handler

    def start(self):
        if USE_FLASK:
            t = threading.Thread(target=self._run_flask, daemon=True)
            t.start()
            self._server_thread = t
            log.info("Web UI running on http://127.0.0.1:%d", self.port)
        else:
            def run_basic():
                server = HTTPServer(
                    ('127.0.0.1', self.port), self._basic_handler)
                server.controller = self.controller
                log.info("Basic Web UI running on http://127.0.0.1:%d", self.port)
                server.serve_forever()
            t = threading.Thread(target=run_basic, daemon=True)
            t.start()
            self._server_thread = t

# ----------------------------- Tray (optional) ----------------------------------------


class TrayIcon:
    def __init__(self, controller: XMRigController, web_port: int, shutdown_fn=None):
        """
        shutdown_fn: callable bez argumentów, wywoływany przy kliknięciu Exit z tray.
        Jeśli podasz shutdown_fn, zostanie wywołany przed zatrzymaniem ikony.
        """
        self.controller = controller
        self.web_port = web_port
        self.shutdown_fn = shutdown_fn
        self.icon = None
        if not HAS_TRAY:
            log.info("pystray or Pillow not installed; tray disabled")
            return
        image = Image.new('RGB', (64, 64), color=(0, 0, 0))
        d = ImageDraw.Draw(image)
        d.ellipse((8, 8, 56, 56), fill=(255, 255, 255))
        self.image = image

    def _open_webui(self, icon, item):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{self.web_port}/")

    def _start(self, icon, item):
        self.controller.start()

    def _stop(self, icon, item):
        self.controller.stop()

    def _exit(self, icon, item):
        log.info("Tray Exit clicked — shutting down services")
        try:
            if callable(self.shutdown_fn):
                # graceful shutdown of other services (rotator, adaptive, webui) done by caller
                self.shutdown_fn()
        except Exception as e:
            log.exception("Error during shutdown_fn: %s", e)
        try:
            # ensure xmrig stopped
            try:
                self.controller.stop()
            except Exception:
                pass
            # stop the icon UI
            icon.stop()
        finally:
            log.info("Tray Exit complete — exiting process")
            # hard exit to ensure no background threads remain
            os._exit(0)

    def run(self):
        if not HAS_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem('Start', self._start),
            pystray.MenuItem('Stop', self._stop),
            pystray.MenuItem('Open Web UI', self._open_webui),
            pystray.MenuItem('Exit', self._exit),
        )
        icon = pystray.Icon('zenminer', self.image, 'ZenMiner', menu)
        self.icon = icon
        icon.run()

# ----------------------------- CLI / Main --------------------------------------------


def build_arg_parser():
    p = argparse.ArgumentParser(description='ZenMiner CLI-only runner')
    p.add_argument('-o', '--pool', default=DEFAULT_POOL,
                   help='pool (host:port)')
    p.add_argument('-u', '--wallet', required=True,
                   help='user wallet (address)')
    p.add_argument(
        '-p', '--worker', default=os.environ.get('COMPUTERNAME', 'worker'), help='worker name')
    p.add_argument('-t', '--threads', default='1',
                   help='threads hybrid spec, e.g. "1,2,4" or "50%,25%,25%" (active,idle,logged_out)')
    p.add_argument('--tp', type=float,
                   default=DEFAULT_ROTATOR_CYCLE_MINUTES, help='rotator cycle minutes')
    p.add_argument('--xmrig-path', required=True,
                   help='path to xmrig executable')
    p.add_argument('--xmrig-http-port', '--http-port-xmrig', dest='xmrig_http_port', type=int,
                   default=DEFAULT_XMRIG_HTTP_PORT,
                   help=f'XMRig HTTP API port (default {DEFAULT_XMRIG_HTTP_PORT})')
    p.add_argument('--donate-level', type=float, default=2.0,
                   help='dev fee percent used by rotator (1-100). Specifies fraction of cycle allocated to dev wallet. Minimum 1%%.')
    p.add_argument('--http-port', type=int,
                   default=DEFAULT_HTTP_PORT, help='web ui port')
    p.add_argument('--http-token', default=DEFAULT_HTTP_TOKEN,
                   help='token used for XMRig HTTP API and Web UI (if set, add header X-Auth-Token)')
    p.add_argument('--no-tray', action='store_true',
                   help='do not show tray even if available')
    p.add_argument('--http-debug', action='store_true',
                   help='enable verbose HTTP request logging (debug)')
    p.add_argument(
        '--idle-after-min',
        type=float,
        default=2.0,
        help='Minutes of no user input after which state becomes IDLE (default: 2.0)'
    )

    p.add_argument(
        '--logout-after-min',
        type=float,
        default=15.0,
        help='Minutes of no user input after which state becomes LOGGED_OUT even if Windows does not report lock (default: 15.0)'
    )

    p.add_argument(
        '--state-debounce-sec',
        type=float,
        default=10.0,
        help='Debounce time in seconds required to confirm state change (default: 10.0)'
    )
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # Validate and normalize donate_level (enforce minimum 1% and max 100%)
    try:
        donate_pct = float(args.donate_level)
    except Exception:
        log.warning("Invalid donate-level provided, defaulting to 2%%")
        donate_pct = 2.0

    # Enforce allowed range: minimum 1%, maximum 100%
    if donate_pct < 1.0:
        log.warning(
            "donate-level %.6f%% is below minimum 1%% — clamping to 1%%", donate_pct)
        donate_pct = 1.0
    if donate_pct > 100.0:
        log.warning("donate-level > 100%%; clamping to 100%%")
        donate_pct = 100.0

# Decide dev wallet: prefer CLI override if given, else default constant
    dev_wallet = args.dev_wallet if getattr(
        args, 'dev_wallet', None) else IMMUTABLE_DEV_WALLET

    log.info("Donate-level (rotator) = %.3f%% ; dev wallet = %s", donate_pct,
             (dev_wallet[:8] + '...' + dev_wallet[-6:]) if dev_wallet else 'None')

    # parse threads
    try:
        threads_map = parse_threads_arg(args.threads)
    except Exception as e:
        log.error("Error parsing threads: %s", e)
        sys.exit(2)

    log.info("ZenMiner starting...")
    log.info("Adaptive timing config: idle_after=%.1f min, logout_after=%.1f min, debounce=%.1f sec",
             args.idle_after_min, args.logout_after_min, args.state_debounce_sec)

    log.info("Detected VERSION: v0.0.1-CLI (modified)")
    log.info("Config loaded (dev_wallet immutable at runtime)")
    log.info("BASE_DIR (runtime): %s", os.getcwd())

    # Create controller
    controller = XMRigController(
        xmrig_path=args.xmrig_path,
        pool=args.pool,
        wallet=args.wallet,
        worker=args.worker,
        xmrig_http_port=args.xmrig_http_port,
        http_token=args.http_token,
        donate_level=args.donate_level,
    )
    controller.threads_spec = args.threads

    # Adaptive controller
    adaptive = AdaptiveThreadController(
        controller,
        threads_map,
        idle_after_min=args.idle_after_min,
        logout_after_min=args.logout_after_min,
        debounce_sec=args.state_debounce_sec
    )
    adaptive.start()

    # Wallet rotator
    rotator = WalletRotator(controller,
                            user_wallet=args.wallet,
                            cycle_minutes=args.tp,            # kept for compatibility
                            dev_percent=donate_pct,
                            worker=args.worker,
                            # fixed, non-overridable defaults (CLI options removed)
                            dev_slice_minutes=15.0,
                            max_cycle_hours=6.0,
                            min_user_seconds=60)
    rotator.start()

    # Start xmrig initially using active threads target
    initial_target = threads_map.get('active')
    if isinstance(initial_target, float):
        import multiprocessing
        cpu = multiprocessing.cpu_count()
        initial_threads = max(1, int(round(initial_target * cpu)))
    else:
        initial_threads = int(initial_target)
    controller.start(threads=initial_threads)

    # Web UI
    webui = WebUI(controller, adaptive, rotator,
                  port=args.http_port, http_token=args.http_token, http_debug=args.http_debug)

    webui.start()

    # Tray
    if not args.no_tray and HAS_TRAY:
        def shutdown_all():
            try:
                log.info("Shutdown_all called from tray")
                # stop wallet rotator and adaptive if exist (use try/except because scope here)
                try:
                    rotator.stop()
                except Exception:
                    pass
                try:
                    adaptive.stop()
                except Exception:
                    pass
                try:
                    webui_stop = getattr(webui, 'shutdown', None)
                    if callable(webui_stop):
                        webui_stop()
                except Exception:
                    pass
                try:
                    controller.stop()
                except Exception:
                    pass
            except Exception as e:
                log.exception("Error in shutdown_all: %s", e)

        tray = TrayIcon(controller, args.http_port, shutdown_fn=shutdown_all)
        t = threading.Thread(target=tray.run, daemon=True)
        t.start()

    # Unified wait + graceful shutdown (no background flag)
    import signal

    shutdown_requested = {"stop": False}

    def _signal_handler(signum, frame):
        log.info("Received signal %s — shutting down...", signum)
        shutdown_requested["stop"] = True

    # register signals (SIGINT = Ctrl+C; SIGTERM = TaskKill/etc.)
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        pass  # Windows may not support SIGTERM depending on environment

    log.info("Press CTRL+C to quit")

    try:
        while not shutdown_requested["stop"]:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutdown requested by user (Ctrl+C)")
    except Exception as e:
        log.exception("Unexpected exception in main loop: %s", e)
    finally:
        log.info("Stopping services...")

        # stop rotator
        try:
            rotator.stop()
        except Exception:
            log.exception("rotator.stop() failed")

        # stop adaptive controller
        try:
            adaptive.stop()
        except Exception:
            log.exception("adaptive.stop() failed")

        # stop xmrig controller
        try:
            controller.stop()
        except Exception:
            log.exception("controller.stop() failed")

        log.info("Shutdown complete.")


if __name__ == '__main__':
    main()
