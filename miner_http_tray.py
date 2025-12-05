#!/usr/bin/env python3
# miner_http_tray.py - ZenMiner (PRO) with restart-lock & orphan cleanup
# - restart_lock to serialize restarts
# - kill_orphans to remove sibling xmrig processes
# - watchdog to ensure single xmrig instance
# - all features from previous version (rotator, adaptive threads, immutable dev wallet, web UI)

import os
import sys
import time
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory
from logging.handlers import RotatingFileHandler
import urllib.request
import urllib.error
import hashlib
import webbrowser

# psutil is optional but strongly recommended for process management
try:
    import psutil
except Exception:
    psutil = None

# Optional tray and image libs
try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None
    Image = None

# user activity helper (Windows)
try:
    from user_activity import get_last_input_seconds, is_workstation_locked
except Exception:
    def get_last_input_seconds():
        return 0.0

    def is_workstation_locked():
        return False

# -------------------------
# BASE_DIR / config resolution
# -------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "miner_config.json"
WEB_DIR = BASE_DIR / "web"

# -------------------------
# Version detection
# -------------------------
FALLBACK_VERSION = "0.0.1"


def detect_version() -> str:
    repo_dir = Path(__file__).resolve().parent
    try:
        if (repo_dir / ".git").exists():
            out = subprocess.check_output(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=str(repo_dir),
                stderr=subprocess.DEVNULL
            )
            v = out.decode("utf-8", errors="ignore").strip()
            if v:
                return v
    except Exception:
        pass
    try:
        p = repo_dir / "VERSION"
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
    except Exception:
        pass
    return FALLBACK_VERSION


VERSION = detect_version()


def sanitize_version_for_id(v: str) -> str:
    if not v:
        return "0_0_0"
    if v.startswith(("v", "V")):
        v = v[1:]
    safe = "".join(ch if (ch.isalnum() or ch in ".-") else "_" for ch in v)
    return safe.replace(".", "_").replace("-", "_")


# -------------------------
# Immutable dev wallet (constant)
# -------------------------
IMMUTABLE_DEV_WALLET = "87QdreCSsmVC3s9SzctnPuMuEfupAivfdHCstLCKvQQiWtkhfoJFWH1fUMxu3am7Lh9eRHfKPVf5Gi5aKemLqBNpTEEc6oj"

# -------------------------
# INTERNAL dev fee -> cycle mapping (not user-editable)
# -------------------------
INTERNAL_DEV_FEE_CYCLE_MAP = [
    {"max_percent": 2.0, "cycle_minutes": 120},
    {"max_percent": 5.0, "cycle_minutes": 60},
    {"max_percent": 10.0, "cycle_minutes": 30},
    {"max_percent": 9999.0, "cycle_minutes": 15}
]

# -------------------------
# DEFAULT CONFIG
# -------------------------
DEFAULT_CONFIG = {
    "xmrig_path": "xmrig",
    "pool": "pool.supportxmr.com:3333",
    "wallet": "YOUR_WALLET",
    "dev_wallet": IMMUTABLE_DEV_WALLET,
    "xmrig_extra_args": "--donate-level=0 --no-huge-pages --http-enabled --http-host=127.0.0.1 --http-port=3333",
    "active_threads": 1,
    "idle_threads": 2,
    "logged_out_threads": 0,
    "idle_threshold_seconds": 300,
    "logged_out_threshold_seconds": 0,
    "debounce_seconds": 120,
    "check_interval": 5,
    "start_minimized": False,
    "log_file": "zenminer.log",
    "log_max_bytes": 5 * 1024 * 1024,
    "log_backup_count": 3,
    "graceful_kill_wait": 6,
    "xmrig_api_host": "127.0.0.1",
    "xmrig_api_port": 3333,
    "api_token": "",
    "wallet_rotation": {
        "enabled": True,
        "dev_fee_percent": 2.0,
        "min_dev_fee_percent": 1.0,
        "min_dev_minutes": 0.5,
        "apply_method": "restart"
    },
    "worker_name": "",
    "debug": False
}

# -------------------------
# helpers
# -------------------------


def deep_merge(a: dict, b: dict):
    for k, v in b.items():
        if k in a and isinstance(a[k], dict) and isinstance(v, dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            deep_merge(cfg, data)
        except Exception as e:
            logging.warning("Failed to parse miner_config.json: %s", e)
    wr = cfg.get("wallet_rotation", {}) or {}
    wr.setdefault("dev_fee_percent", float(wr.get("dev_fee_percent", 2.0)))
    wr.setdefault("min_dev_fee_percent", float(
        wr.get("min_dev_fee_percent", 1.0)))
    wr.setdefault("min_dev_minutes", float(wr.get("min_dev_minutes", 0.5)))
    wr.setdefault("enabled", bool(wr.get("enabled", True)))
    wr.setdefault("apply_method", wr.get("apply_method", "restart"))
    cfg["wallet_rotation"] = wr
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(
            cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        logging.info("Config saved to %s", CONFIG_PATH)
        return True
    except Exception as e:
        logging.error("Failed to save config: %s", e)
        return False

# -------------------------
# logging
# -------------------------


def setup_logging(path, max_bytes, backup_count, debug=False):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    ch.setFormatter(fmt)
    logger.handlers = []  # reset existing handlers
    logger.addHandler(ch)
    try:
        fh = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setLevel(logging.DEBUG if debug else logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning("Could not create RotatingFileHandler: %s", e)

# -------------------------
# user hash id for analytics
# -------------------------


def user_hash_id(wallet: str) -> str:
    version = VERSION or FALLBACK_VERSION
    safe_version = sanitize_version_for_id(version)
    key = (wallet or "") + version
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    short = h[:8]
    return f"user_{short}_v{safe_version}"


def determine_worker_name(cfg: dict, rotation_state: dict) -> str:
    state = rotation_state.get("current")
    wallet = cfg.get("wallet") or ""
    if state == "dev":
        safe_v = sanitize_version_for_id(VERSION or FALLBACK_VERSION)
        hid = hashlib.sha256(
            (wallet + (VERSION or FALLBACK_VERSION)).encode("utf-8")).hexdigest()[:8]
        return f"user_{hid}_v{safe_v}"
    cworker = (cfg.get("worker_name") or "").strip()
    if cworker:
        return cworker
    return "zenminner"

# -------------------------
# MinerManager with restart_lock & orphan cleanup
# -------------------------


class MinerManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.current_threads = None
        self.runtime_wallet = cfg.get("wallet")
        self.start_time = time.time()
        self._wallet_rotator_thread = None
        self._wallet_rotator_stop = threading.Event()
        self._rotation_state = {"current": None, "next_switch_in_seconds": None,
                                "dev_minutes": None, "user_minutes": None, "cycle_minutes": None}
        self._xmrig_api_lock = threading.Lock()
        self.xmrig_api = None
        self.xmrig_api_available = False
        self._stop_polling = threading.Event()
        self._last_thread_change = 0
        self._last_requested_worker: Optional[str] = None

        # PRO additions:
        self.restart_lock = threading.Lock()   # serialize restarts
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None

    def _xmrig_api_base(self):
        host = self.cfg.get("xmrig_api_host", "127.0.0.1")
        port = int(self.cfg.get("xmrig_api_port", 3333))
        return f"http://{host}:{port}"

    def build_cmd(self, threads: int, worker_name: str = ""):
        xmrig = self.cfg.get("xmrig_path", "xmrig")
        if not os.path.isabs(xmrig):
            xmrig = str(Path(BASE_DIR) / xmrig)
        wallet_to_use = self.runtime_wallet or self.cfg.get("wallet")
        args = [
            str(xmrig),
            "-o", self.cfg["pool"],
            "-u", wallet_to_use,
            "-p", worker_name,
            f"--threads={threads}"
        ]
        if self.cfg.get("xmrig_extra_args"):
            args.extend(self.cfg["xmrig_extra_args"].split())
        return args

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _collect_running_xmrigs(self):
        procs = []
        if psutil:
            for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if "xmrig" in name:
                        procs.append(p)
                except Exception:
                    continue
        else:
            # fallback: try using tasklist (Windows)
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq xmrig.exe"], stderr=subprocess.DEVNULL, shell=False)
                # not parsing further in fallback - leave procs empty
            except Exception:
                pass
        return procs

    def kill_orphan_xmrigs(self, keep_pid: Optional[int] = None):
        """Kill other xmrig processes except keep_pid (if provided). Requires psutil."""
        if not psutil:
            logging.debug(
                "psutil not available - cannot kill orphan xmrig processes")
            return {"status": "skipped", "reason": "no_psutil"}
        killed = []
        for p in self._collect_running_xmrigs():
            try:
                if keep_pid and p.pid == keep_pid:
                    continue
                # avoid killing unrelated processes with xmrig in name? we assume xmrig.exe is the miner
                logging.info(
                    "Killing orphan xmrig process pid=%s cmdline=%s", p.pid, p.cmdline())
                p.kill()
                killed.append(p.pid)
            except Exception as e:
                logging.warning("Failed to kill process %s: %s",
                                getattr(p, "pid", "?"), e)
        return {"status": "ok", "killed": killed}

    def start(self, threads: Optional[int] = None, worker_name: Optional[str] = None):
        with self.lock:
            if threads is None:
                threads = int(self.cfg.get("active_threads", 1))

            worker_to_use = worker_name if worker_name is not None else self._last_requested_worker

            xmrig_path = str(self.cfg.get("xmrig_path", "xmrig"))
            if not os.path.isabs(xmrig_path):
                xmrig_path = str(Path(BASE_DIR) / xmrig_path)
            logging.info("Resolved xmrig_path -> %s", xmrig_path)
            if not os.path.exists(xmrig_path):
                logging.error(
                    "Configured xmrig_path does not exist: %s", xmrig_path)
                return {"status": "error", "error": f"xmrig binary not found: {xmrig_path}"}

            worker = "" if not worker_to_use else worker_to_use
            cmd = [
                xmrig_path,
                "-o", self.cfg["pool"],
                "-u", self.runtime_wallet or self.cfg.get("wallet"),
                "-p", worker,
                f"--threads={threads}"
            ]
            if self.cfg.get("xmrig_extra_args"):
                cmd.extend(self.cfg["xmrig_extra_args"].split())

            # ensure no other xmrig is running (best-effort)
            try:
                self.kill_orphan_xmrigs(
                    keep_pid=getattr(self.proc, "pid", None))
            except Exception as e:
                logging.debug("kill_orphan_xmrigs error (ignored): %s", e)

            logging.info("Starting xmrig: %s", " ".join(map(str, cmd)))
            logfile = open(
                str(Path(BASE_DIR) / "xmrig_stdout.log"), "a", encoding="utf-8")
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=logfile, stderr=logfile)
                self.current_threads = threads
                logging.info("xmrig started pid=%s threads=%s worker=%s", getattr(
                    self.proc, "pid", None), threads, worker)
                return {"status": "started", "threads": threads, "worker": worker, "pid": getattr(self.proc, "pid", None)}
            except FileNotFoundError as e:
                logging.error("xmrig binary not found: %s", e)
                self.proc = None
                return {"status": "error", "error": str(e)}
            except Exception as e:
                logging.exception("Failed to start xmrig: %s", e)
                self.proc = None
                return {"status": "error", "error": str(e)}

    def stop(self, force_kill_after: Optional[int] = None):
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                logging.info("xmrig not running")
                self.proc = None
                self.current_threads = None
                return {"status": "not_running"}
            logging.info("Stopping xmrig (graceful)...")
            try:
                self.proc.terminate()
                try:
                    timeout = int(self.cfg.get("graceful_kill_wait", 6))
                    if force_kill_after is not None:
                        timeout = force_kill_after
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logging.warning("xmrig did not exit after %s s, killing...", self.cfg.get(
                        "graceful_kill_wait", 6))
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logging.warning("xmrig kill timeout")
                for _ in range(20):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
                return {"status": "stopped"}
            except Exception as e:
                logging.exception("Error stopping xmrig: %s", e)
                return {"status": "error", "error": str(e)}
            finally:
                self.proc = None
                self.current_threads = None

    def restart(self, threads: Optional[int] = None, worker_name: Optional[str] = None, reason: str = "manual", wait_for_exit_ms: int = 8000):
        """
        PRO restart: serialized by restart_lock. Ensures previous process is dead before start.
        reason: textual reason for logging (e.g., "adaptive", "rotator", "apply_config")
        wait_for_exit_ms: total wait time for previous process to exit
        """
        logging.info("Restart requested (reason=%s threads=%s worker=%s)",
                     reason, threads, worker_name)
        with self.restart_lock:
            logging.info("Restart lock acquired (reason=%s)", reason)
            # attempt graceful stop
            try:
                if self.is_running():
                    logging.info(
                        "Stopping existing xmrig before restart (reason=%s)...", reason)
                    self.stop()
            except Exception as e:
                logging.warning("Error during stop before restart: %s", e)

            # wait until no xmrig processes remain (or until timeout)
            start_wait = time.time()
            timeout = float(wait_for_exit_ms) / 1000.0
            while True:
                still = False
                procs = self._collect_running_xmrigs()
                # consider xmrig running if any xmrig process exists (best-effort)
                if procs:
                    still = True
                if not still:
                    break
                if (time.time() - start_wait) > timeout:
                    logging.warning(
                        "Timeout waiting for xmrig to exit (after %.2fs). Proceeding to start and attempt cleanup.", timeout)
                    # attempt to kill orphans aggressively
                    try:
                        self.kill_orphan_xmrigs()
                    except Exception as e:
                        logging.warning("kill_orphan_xmrigs failed: %s", e)
                    break
                time.sleep(0.15)

            res = self.start(threads=threads, worker_name=worker_name)
            logging.info("Restart completed (reason=%s) -> %s", reason, res)
            return res

    def status(self):
        running = self.proc is not None and self.proc.poll() is None
        username = self.get_current_username()
        return {
            "running": running,
            "pid": self.proc.pid if self.proc else None,
            "threads": self.current_threads,
            "runtime_wallet": self.runtime_wallet,
            "username": username
        }

    def fetch_xmrig_summary_once(self, timeout=2.0):
        url = self._xmrig_api_base() + "/1/summary"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                return json.loads(text)
        except Exception:
            raise

    def xmrig_api_poll_loop(self):
        interval = max(1, int(self.cfg.get("check_interval", 5)))
        logging.info("Starting XMRig API poller (every %s s) to %s",
                     interval, self._xmrig_api_base() + "/1/summary")
        while not self._stop_polling.is_set():
            try:
                data = self.fetch_xmrig_summary_once(timeout=2.0)
                with self._xmrig_api_lock:
                    self.xmrig_api = data
                    self.xmrig_api_available = True
            except Exception:
                with self._xmrig_api_lock:
                    self.xmrig_api = None
                    self.xmrig_api_available = False
            for _ in range(interval):
                if self._stop_polling.is_set():
                    break
                time.sleep(1)

    def start_xmrig_api_poller(self):
        self._stop_polling.clear()
        t = threading.Thread(target=self.xmrig_api_poll_loop, daemon=True)
        t.start()

    def stop_xmrig_api_poller(self):
        self._stop_polling.set()

    def try_set_threads_via_xmrig_api(self, threads: int, worker_name: Optional[str] = None, timeout=2.0):
        base = self._xmrig_api_base()
        cfg_url = base + "/1/config"
        try:
            req = urllib.request.Request(
                cfg_url, headers={"User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                cur = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"Failed to GET xmrig config: {e}")

        changed = False
        cpu = cur.get("cpu") or {}
        if isinstance(cpu, dict):
            if "threads" in cpu:
                cpu["threads"] = threads
                changed = True
            if "profile" in cpu and isinstance(cpu["profile"], dict):
                cpu["profile"]["threads"] = threads
                changed = True

        if worker_name is not None:
            if "connection" in cur and isinstance(cur["connection"], dict):
                cur["connection"]["pass"] = worker_name
                changed = True
            else:
                cur["pass"] = worker_name
                changed = True

        if not changed:
            raise RuntimeError(
                "Couldn't find safe fields to update threads/worker in XMRig config")

        try:
            data = json.dumps(cur).encode("utf-8")
            req2 = urllib.request.Request(cfg_url, data=data, method="PUT",
                                          headers={"Content-Type": "application/json", "User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                _ = resp2.read()
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to PUT xmrig config: {e}")

    def set_threads(self, threads: int, worker_name: Optional[str] = None, source: str = "manual"):
        """
        Set threads: prefer XMRig API, fallback to restart.
        'source' is for logging (adaptive, rotator, apply_config, manual).
        """
        now = time.time()
        last_change = getattr(self, "_last_thread_change", 0)
        if now - last_change < float(self.cfg.get("debounce_seconds", 120)):
            logging.info(
                "Threads change suppressed by debounce. (requested=%s source=%s)", threads, source)
            return {"status": "debounced", "threads": threads}

        cur_threads = getattr(self, "current_threads", None)
        cur_worker = getattr(self, "_last_requested_worker", None)
        if cur_threads is not None:
            if threads == cur_threads and (worker_name is None or worker_name == cur_worker):
                logging.info(
                    "Requested threads == current_threads and worker unchanged -> no-op (source=%s)", source)
                return {"status": "ok", "method": "noop", "threads": threads, "worker": cur_worker}

        try:
            if worker_name:
                self._last_requested_worker = worker_name

            if self.xmrig_api_available:
                try:
                    self.try_set_threads_via_xmrig_api(
                        threads, worker_name=worker_name)
                    logging.info(
                        "Changed threads via XMRig API: threads=%s worker=%s (source=%s)", threads, worker_name, source)
                    self.current_threads = threads
                    self._last_thread_change = time.time()
                    return {"status": "ok", "method": "api", "threads": threads, "worker": worker_name}
                except Exception as e:
                    logging.warning(
                        "XMRig API threads/worker change failed: %s (source=%s)", e, source)

            logging.info(
                "Fallback to restart to apply threads/worker (source=%s)", source)
            res = self.restart(threads, worker_name=worker_name, reason=source)
            self.current_threads = threads
            self._last_thread_change = time.time()
            return {"status": "ok", "method": "restart", "threads": threads, "worker": worker_name, "result": res}
        except Exception as e:
            logging.exception("set_threads failed: %s", e)
            return {"status": "error", "error": str(e)}

    # -------------------------
    # Wallet rotation
    # -------------------------
    def _compute_rotation_times(self):
        wr = self.cfg.get("wallet_rotation", {}) or {}
        P = float(wr.get("dev_fee_percent", 2.0))
        minP = float(wr.get("min_dev_fee_percent", 1.0))
        min_minutes = float(wr.get("min_dev_minutes", 0.5))
        if P < minP:
            logging.warning(
                "dev_fee_percent < min_dev_fee_percent, clamping to min")
            P = minP

        cycle_minutes = None
        try:
            for entry in INTERNAL_DEV_FEE_CYCLE_MAP:
                if P <= float(entry.get("max_percent", 9999.0)):
                    cycle_minutes = float(entry.get("cycle_minutes"))
                    break
        except Exception as e:
            logging.warning("Invalid INTERNAL_DEV_FEE_CYCLE_MAP: %s", e)
        if cycle_minutes is None:
            cycle_minutes = 60.0

        dev_minutes = max(cycle_minutes * (P / 100.0), min_minutes)
        user_minutes = max(cycle_minutes - dev_minutes, 0.0)
        if user_minutes > 24 * 60:
            user_minutes = 24 * 60
        return cycle_minutes, P, dev_minutes, user_minutes

    def wallet_rotator_loop(self):
        wr = self.cfg.get("wallet_rotation", {}) or {}
        if not wr.get("enabled"):
            logging.info("Wallet rotator disabled")
            return
        try:
            C, P, dev_minutes, user_minutes = self._compute_rotation_times()
        except Exception as e:
            logging.error("Wallet rotation config invalid: %s", e)
            return

        logging.info("Wallet rotator running: cycle_minutes=%.2f dev_fee_percent=%.3f => dev_minutes=%.2f user_minutes=%.2f",
                     C, P, dev_minutes, user_minutes)
        apply_method = wr.get("apply_method", "restart")
        dev_wallet = IMMUTABLE_DEV_WALLET
        user_wallet = self.cfg.get("wallet")

        self._rotation_state.update(
            {"dev_minutes": dev_minutes, "user_minutes": user_minutes, "cycle_minutes": C})

        logging.info("Rotator: initial start with USER wallet")
        self._rotation_state["current"] = "user"
        init_worker = determine_worker_name(self.cfg, self._rotation_state)
        self._last_requested_worker = init_worker
        self._apply_wallet(user_wallet, apply_method, worker_name=init_worker)

        while not self._wallet_rotator_stop.is_set():
            logging.info("Wallet rotator: user period %.2f min", user_minutes)
            self._rotation_state["current"] = "user"
            self._rotation_state["next_switch_in_seconds"] = int(
                user_minutes * 60)
            for _ in range(int(user_minutes * 60)):
                if self._wallet_rotator_stop.is_set():
                    break
                time.sleep(1)
                self._rotation_state["next_switch_in_seconds"] = max(
                    0, self._rotation_state["next_switch_in_seconds"] - 1)
            if self._wallet_rotator_stop.is_set():
                break
            logging.info("Wallet rotator: dev period %.2f min", dev_minutes)
            self._rotation_state["current"] = "dev"
            dev_worker = determine_worker_name(self.cfg, self._rotation_state)
            self._last_requested_worker = dev_worker
            self._apply_wallet(dev_wallet, apply_method,
                               worker_name=dev_worker)
            self._rotation_state["next_switch_in_seconds"] = int(
                dev_minutes * 60)
            for _ in range(int(dev_minutes * 60)):
                if self._wallet_rotator_stop.is_set():
                    break
                time.sleep(1)
                self._rotation_state["next_switch_in_seconds"] = max(
                    0, self._rotation_state["next_switch_in_seconds"] - 1)
        logging.info("Wallet rotator stopped")

    def _apply_wallet(self, wallet_addr, apply_method, worker_name: Optional[str] = None):
        self.runtime_wallet = wallet_addr
        logging.info(
            "Runtime wallet set to %s (not persisted). Requested worker: %s", wallet_addr, worker_name)
        if worker_name:
            self._last_requested_worker = worker_name

        if apply_method == "api" and self.xmrig_api_available:
            try:
                base = self._xmrig_api_base()
                cfg_url = base + "/1/config"
                req = urllib.request.Request(
                    cfg_url, headers={"User-Agent": "ZenMiner/1.0"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    current = json.loads(
                        resp.read().decode("utf-8", errors="replace"))
                changed = False
                if isinstance(current, dict):
                    if "connection" in current and isinstance(current["connection"], dict):
                        if "user" in current["connection"]:
                            current["connection"]["user"] = wallet_addr
                            changed = True
                        elif "wallet" in current["connection"]:
                            current["connection"]["wallet"] = wallet_addr
                            changed = True
                        else:
                            current["connection"]["user"] = wallet_addr
                            changed = True
                    elif "wallet" in current:
                        current["wallet"] = wallet_addr
                        changed = True
                    else:
                        current["user"] = wallet_addr
                        changed = True

                    if worker_name is not None:
                        if "connection" in current and isinstance(current["connection"], dict):
                            current["connection"]["pass"] = worker_name
                            changed = True
                        else:
                            current["pass"] = worker_name
                            changed = True

                if changed:
                    data = json.dumps(current).encode("utf-8")
                    req2 = urllib.request.Request(cfg_url, data=data, method="PUT",
                                                  headers={"Content-Type": "application/json", "User-Agent": "ZenMiner/1.0"})
                    with urllib.request.urlopen(req2, timeout=2) as r2:
                        _ = r2.read()
                    logging.info(
                        "Applied wallet/worker via XMRig API (runtime)")
                    return
                else:
                    logging.warning(
                        "Couldn't apply wallet/worker via API (unknown config shape) - fallback to restart")
                    self.restart(worker_name=worker_name,
                                 reason="apply_wallet_fallback")
            except Exception as e:
                logging.warning(
                    "Applying wallet/worker via API failed (%s) - fallback to restart", e)
                self.restart(worker_name=worker_name,
                             reason="apply_wallet_fallback")
        else:
            logging.info("Applying runtime wallet/worker via restart")
            self.restart(worker_name=worker_name,
                         reason="apply_wallet_restart")

    def start_wallet_rotator(self):
        wr = self.cfg.get("wallet_rotation", {}) or {}
        if not wr.get("enabled", True):
            logging.info("Wallet rotator disabled in config")
            return
        self._wallet_rotator_stop.clear()
        t = threading.Thread(target=self.wallet_rotator_loop, daemon=True)
        t.start()
        self._wallet_rotator_thread = t

    def stop_wallet_rotator(self):
        self._wallet_rotator_stop.set()

    def rotate_now(self):
        state = self._rotation_state.get("current")
        if state == "dev":
            target_wallet = self.cfg.get("wallet")
            target = "user"
            worker = determine_worker_name(self.cfg, {"current": "user"})
        else:
            target_wallet = IMMUTABLE_DEV_WALLET
            target = "dev"
            worker = determine_worker_name(self.cfg, {"current": "dev"})
        self._last_requested_worker = worker
        self._apply_wallet(target_wallet, self.cfg.get("wallet_rotation", {}).get(
            "apply_method", "restart"), worker_name=worker)
        self._rotation_state["current"] = target
        return {"status": "ok", "switched_to": target, "worker": worker}

    def get_current_username(self):
        configured_user_wallet = self.cfg.get("wallet")
        return user_hash_id(configured_user_wallet or "unknown")

    def rotation_status(self):
        st = dict(self._rotation_state)
        st["username"] = self.get_current_username()
        st["app"] = {"version": VERSION}
        st["runtime_seconds"] = int(time.time() - self.start_time)
        st["next_switch_in_seconds"] = int(
            st.get("next_switch_in_seconds") or 0)
        st["cycle_minutes"] = st.get("cycle_minutes")
        return st

    # -------------------------
    # Watchdog - ensures single xmrig instance and logs if anomalies
    # -------------------------
    def watchdog_loop(self):
        if not psutil:
            logging.info("Watchdog disabled (psutil not available)")
            return
        interval = max(5, int(self.cfg.get("check_interval", 5)))
        logging.info(
            "Watchdog started (checking every %s s) - will attempt to remove orphan xmrigs", interval)
        while not self._watchdog_stop.is_set():
            try:
                procs = self._collect_running_xmrigs()
                if len(procs) > 1:
                    pids = [p.pid for p in procs]
                    logging.warning(
                        "Watchdog detected multiple xmrig processes: %s - attempting cleanup", pids)
                    try:
                        # keep our known pid if present
                        keep = getattr(self.proc, "pid", None)
                        self.kill_orphan_xmrigs(keep_pid=keep)
                    except Exception as e:
                        logging.warning(
                            "Watchdog kill_orphan_xmrigs error: %s", e)
            except Exception:
                logging.exception("Watchdog loop error")
            time.sleep(interval)

    def start_watchdog(self):
        if not psutil:
            return
        self._watchdog_stop.clear()
        t = threading.Thread(target=self.watchdog_loop, daemon=True)
        self._watchdog_thread = t
        t.start()

    def stop_watchdog(self):
        self._watchdog_stop.set()

# -------------------------
# AdaptiveThreadController
# -------------------------


class AdaptiveThreadController(threading.Thread):
    def __init__(self, manager: MinerManager, cfg):
        super().__init__(daemon=True)
        self.manager = manager
        self.cfg = cfg
        self.poll_interval = max(1, int(cfg.get("check_interval", 5)))
        self.active_threads = int(cfg.get("active_threads", 1))
        self.idle_threads = int(cfg.get("idle_threads", 2))
        self.logged_out_threads = int(cfg.get("logged_out_threads", 0))
        self.idle_threshold = float(cfg.get("idle_threshold_seconds", 300))
        self.logged_out_threshold = float(
            cfg.get("logged_out_threshold_seconds", 0))
        self.debounce_seconds = float(cfg.get("debounce_seconds", 120))
        self._last_change_ts = 0
        self._last_state = None
        self._stop = threading.Event()

    def _state_from_idle(self, idle_seconds: Optional[float]) -> str:
        if float(self.cfg.get("logged_out_threshold_seconds", 0)) <= 0:
            if idle_seconds is None:
                return "logged_out"
            if idle_seconds < self.idle_threshold:
                return "active"
            return "idle"
        if idle_seconds is None:
            return "active"
        if idle_seconds < self.idle_threshold:
            return "active"
        if idle_seconds >= self.logged_out_threshold:
            return "logged_out"
        return "idle"

    def run(self):
        logging.info("AdaptiveThreadController started")
        while not self._stop.is_set():
            try:
                locked = False
                try:
                    locked = is_workstation_locked()
                except Exception as e:
                    logging.debug("is_workstation_locked error: %s", e)
                    locked = False

                if locked:
                    state = "logged_out"
                    idle = None
                else:
                    try:
                        idle = get_last_input_seconds()
                    except Exception as e:
                        logging.warning("get_last_input_seconds error: %s", e)
                        idle = 0.0
                    state = self._state_from_idle(idle)

                if state != self._last_state:
                    reason = f"locked={locked}" if locked else f"idle_seconds={idle:.1f}"
                    logging.info("Adaptive state change: %s -> %s (%s)",
                                 self._last_state, state, reason)
                    now = time.time()
                    force_immediate = (state == "logged_out") or (
                        self._last_state == "logged_out" and state == "active")

                    if force_immediate or (now - self._last_change_ts >= self.debounce_seconds):
                        if state == "active":
                            target = self.active_threads
                        elif state == "idle":
                            target = self.idle_threads
                        else:
                            target = self.logged_out_threads
                        worker_name = determine_worker_name(
                            self.cfg, self.manager._rotation_state)
                        self.manager._last_requested_worker = worker_name
                        logging.info("Adaptive deciding target threads for state=%s -> %s (config_logged_out=%s active=%s idle=%s)",
                                     state, target, self.logged_out_threads, self.active_threads, self.idle_threads)
                        res = self.manager.set_threads(
                            target, worker_name=worker_name, source="adaptive")
                        logging.info("Adaptive threads set result: %s", res)
                        self.manager._rotation_state.setdefault("activity", {})
                        self.manager._rotation_state["activity"].update(
                            {"state": state, "changed_at": int(time.time()), "reason": reason})
                        self._last_change_ts = now
                        self._last_state = state
                    else:
                        remain = self.debounce_seconds - \
                            (now - self._last_change_ts)
                        logging.info(
                            "Adaptive change suppressed by debounce (%.1fs left)", remain)
            except Exception:
                logging.exception("AdaptiveThreadController loop error")
            time.sleep(self.poll_interval)
        logging.info("AdaptiveThreadController stopped")

    def stop(self):
        self._stop.set()


# -------------------------
# Flask API + UI
# -------------------------
app = Flask("zenminer", static_folder=str(WEB_DIR), static_url_path="/ui")
cfg = load_config()
manager = MinerManager(cfg)


def _get_api_token():
    try:
        return cfg.get("api_token")
    except Exception:
        return None


def require_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = _get_api_token()
        if not token:
            return func(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth.split(" ", 1)[1].strip()
            if provided == token:
                return func(*args, **kwargs)
        return jsonify({"status": "error", "error": "unauthorized"}), 401
    return wrapper


@app.route("/status", methods=["GET"])
def api_status():
    s = manager.status()
    s["config"] = cfg
    with manager._xmrig_api_lock:
        s["xmrig_api_available"] = manager.xmrig_api_available
        s["xmrig_api"] = manager.xmrig_api
    s["rotation_status"] = manager.rotation_status()
    s["app"] = {"version": VERSION}
    return jsonify(s)


@app.route("/rotation_status", methods=["GET"])
def api_rotation_status():
    return jsonify(manager.rotation_status())


@app.route("/config", methods=["GET", "POST"])
def api_config():
    global cfg
    if request.method == "GET":
        view = dict(cfg)
        view["dev_wallet"] = IMMUTABLE_DEV_WALLET
        return jsonify(view)
    else:
        new = request.json
        if not isinstance(new, dict):
            return jsonify({"status": "error", "error": "invalid payload"}), 400
        try:
            if "dev_wallet" in new:
                logging.warning(
                    "Attempt to modify dev_wallet via API ignored.")
                new = dict(new)
                new.pop("dev_wallet", None)
            if "wallet" in new:
                logging.warning(
                    "Attempt to modify wallet via API ignored (edit miner_config.json file).")
                new = dict(new)
                new.pop("wallet", None)
            deep_merge(cfg, new)
            wr = cfg.get("wallet_rotation", {}) or {}
            wr.setdefault("dev_fee_percent", float(
                wr.get("dev_fee_percent", 2.0)))
            wr.setdefault("min_dev_fee_percent", float(
                wr.get("min_dev_fee_percent", 1.0)))
            wr.setdefault("min_dev_minutes", float(
                wr.get("min_dev_minutes", 0.5)))
            wr.setdefault("enabled", bool(wr.get("enabled", True)))
            wr.setdefault("apply_method", wr.get("apply_method", "restart"))
            cfg["wallet_rotation"] = wr
        except Exception as e:
            logging.error("api_config merge error: %s", e)
            return jsonify({"status": "error", "error": "merge_failed"}), 500

        try:
            if float(cfg["wallet_rotation"].get("dev_fee_percent", 0.0)) < float(cfg["wallet_rotation"].get("min_dev_fee_percent", 1.0)):
                return jsonify({"status": "error", "error": "dev_fee_percent lower than min_dev_fee_percent"}), 400
        except Exception:
            pass

        saved = save_config(cfg)
        if not saved:
            return jsonify({"status": "error", "error": "save_failed"}), 500

        manager.stop_wallet_rotator()
        manager.start_wallet_rotator()
        return jsonify({"status": "ok", "config": cfg})


@app.route("/apply_config", methods=["POST"])
@require_token
def api_apply_config():
    body = request.json or {}
    restart_flag = bool(body.get("restart", False))
    saved = save_config(cfg)
    if not saved:
        return jsonify({"status": "error", "error": "save_failed"}), 500
    manager.stop_wallet_rotator()
    manager.start_wallet_rotator()
    if restart_flag:
        manager.restart(worker_name=determine_worker_name(
            cfg, manager._rotation_state), reason="apply_config")
    return jsonify({"status": "ok", "saved": True, "restarted": restart_flag})


@app.route("/probe_xmrig", methods=["POST"])
def api_probe_xmrig():
    host = cfg.get("xmrig_api_host", "127.0.0.1")
    port = int(cfg.get("xmrig_api_port", 3333))
    url = f"http://{host}:{port}/1/summary"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ZenMiner/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            return jsonify({"status": "ok", "xmrig_api": data})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/start", methods=["POST"])
@require_token
def api_start():
    data = request.json or {}
    threads = data.get("threads")
    worker = determine_worker_name(cfg, manager._rotation_state)
    manager._last_requested_worker = worker
    res = manager.start(threads, worker_name=worker)
    return jsonify(res)


@app.route("/stop", methods=["POST"])
@require_token
def api_stop():
    res = manager.stop()
    return jsonify(res)


@app.route("/restart", methods=["POST"])
@require_token
def api_restart():
    data = request.json or {}
    threads = data.get("threads")
    worker = determine_worker_name(cfg, manager._rotation_state)
    manager._last_requested_worker = worker
    res = manager.restart(threads, worker_name=worker, reason="api_restart")
    return jsonify(res)


@app.route("/set_threads", methods=["POST"])
@require_token
def api_set_threads():
    payload = request.json or {}
    threads = payload.get("threads")
    if threads is None:
        return jsonify({"status": "error", "error": "missing threads parameter"}), 400
    worker = determine_worker_name(cfg, manager._rotation_state)
    manager._last_requested_worker = worker
    res = manager.set_threads(
        int(threads), worker_name=worker, source="api_set_threads")
    return jsonify(res)


@app.route("/rotate_now", methods=["POST"])
@require_token
def api_rotate_now():
    res = manager.rotate_now()
    return jsonify(res)

# UI static serve


@app.route("/ui/")
@app.route("/ui/<path:filename>")
def ui_files(filename="index.html"):
    f = WEB_DIR / filename
    if not f.exists():
        return "UI not found. Put web/index.html in the web/ directory.", 404
    return send_from_directory(str(WEB_DIR), filename)

# -------------------------
# Tray helpers (unchanged)
# -------------------------


def create_image(width=64, height=64):
    if Image is None:
        return None
    img = Image.new('RGB', (width, height), (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.text((width*0.22, height*0.12), "Z", fill=(220, 200, 80))
    return img


def open_config_in_editor():
    path = CONFIG_PATH.resolve()
    try:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logging.error("Couldn't open config in editor: %s", e)


def open_web_ui():
    host = cfg.get("http_host", "127.0.0.1")
    port = int(cfg.get("http_port", 5515))
    url = f"http://{host}:{port}/ui/"
    try:
        webbrowser.open(url)
    except Exception as e:
        logging.error("Failed to open web UI: %s", e)


def tray_worker(mm: MinerManager):
    if pystray is None or Image is None:
        logging.info("pystray or pillow not installed - skipping tray")
        return
    icon = pystray.Icon("zenminer")
    icon.icon = create_image()
    icon.title = "ZenMiner"

    def on_start(icon, item):
        logging.info("Tray -> start")
        worker = determine_worker_name(cfg, mm._rotation_state)
        mm._last_requested_worker = worker
        mm.start(worker_name=worker)

    def on_stop(icon, item):
        logging.info("Tray -> stop")
        mm.stop()

    def on_status(icon, item):
        s = mm.status()
        logging.info("Tray -> status: %s", s)

    def on_restart(icon, item):
        logging.info("Tray -> restart")
        worker = determine_worker_name(cfg, mm._rotation_state)
        mm._last_requested_worker = worker
        mm.restart(worker_name=worker, reason="tray_restart")

    def on_apply_config(icon, item):
        logging.info("Tray -> apply config")
        try:
            import requests as _r
            url = f"http://{cfg.get('http_host','127.0.0.1')}:{cfg.get('http_port',5515)}/apply_config"
            _r.post(url, json={"restart": True}, timeout=3)
        except Exception as e:
            logging.warning("Tray apply_config failed: %s", e)

    def on_rotate_now(icon, item):
        logging.info("Tray -> rotate now (force)")
        try:
            import requests as _r
            url = f"http://{cfg.get('http_host','127.0.0.1')}:{cfg.get('http_port',5515)}/rotate_now"
            _r.post(url, timeout=3)
        except Exception as e:
            logging.warning("Tray rotate_now failed: %s", e)

    def on_open_web_ui(icon, item):
        logging.info("Tray -> Open Web UI")
        open_web_ui()

    def on_exit(icon, item):
        logging.info("Tray -> exit")
        try:
            mm.stop()
        except Exception:
            pass
        mm.stop_xmrig_api_poller()
        mm.stop_wallet_rotator()
        mm.stop_watchdog()
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Web UI", on_open_web_ui),
        pystray.MenuItem("Start", on_start),
        pystray.MenuItem("Stop", on_stop),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Edit config", lambda i, it: open_config_in_editor()),
        pystray.MenuItem("Apply config (restart)", on_apply_config),
        pystray.MenuItem("Rotate now", on_rotate_now),
        pystray.MenuItem("Status (log)", on_status),
        pystray.MenuItem("Exit", on_exit)
    )
    icon.menu = menu
    icon.run()


def run_flask(host, port):
    app.run(host=host, port=port, debug=False, use_reloader=False)


# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    setup_logging(
        path=str(Path(BASE_DIR) / cfg.get("log_file", "zenminer.log")),
        max_bytes=int(cfg.get("log_max_bytes", 5 * 1024 * 1024)),
        backup_count=int(cfg.get("log_backup_count", 3)),
        debug=bool(cfg.get("debug", False))
    )

    logging.info("ZenMiner starting...")
    logging.info("Detected VERSION: %s", VERSION)
    logging.info("Config loaded (dev_wallet immutable at runtime)")

    try:
        logging.info("BASE_DIR (runtime): %s", BASE_DIR)
        logging.info("CONFIG_PATH resolved: %s", CONFIG_PATH)
        logging.info("Config file exists at startup: %s", CONFIG_PATH.exists())
    except Exception:
        pass

    manager.start_xmrig_api_poller()
    manager.start_wallet_rotator()
    manager.start_watchdog()

    adaptive = AdaptiveThreadController(manager, cfg)
    adaptive.start()

    flask_thread = threading.Thread(target=run_flask, args=(cfg.get(
        "http_host", "127.0.0.1"), int(cfg.get("http_port", 5515))), daemon=True)
    flask_thread.start()

    try:
        tray_worker(manager)
    except KeyboardInterrupt:
        logging.info("Interrupted")
        manager.stop_xmrig_api_poller()
        manager.stop_wallet_rotator()
        manager.stop_watchdog()
        adaptive.stop()
        manager.stop()
        sys.exit(0)
