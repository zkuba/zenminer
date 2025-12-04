#!/usr/bin/env python3
# miner_http_tray.py
# ZenMiner - REST + Tray controller (full: API poller, set_threads API+fallback, apply_config, wallet rotation, tray menu)

import os
import sys
import time
import json
import logging
import subprocess
import threading
import random
from pathlib import Path
from typing import Optional
from functools import wraps
from flask import Flask, jsonify, request

# --- HTTP helper ---
import urllib.request
import urllib.error

try:
    import psutil
except Exception:
    print("Install requirements: pip install -r requirements.txt")
    sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    print("Install requirements: pip install pystray pillow")
    sys.exit(1)

# -------------------------
# Defaults
# -------------------------
DEFAULT_CONFIG = {
    "xmrig_path": "xmrig",
    "pool": "pool.supportxmr.com:3333",
    "wallet": "YOUR_WALLET",
    "xmrig_extra_args": "--donate-level=0 --no-huge-pages",
    "high_threads": 4,
    "low_threads": 1,
    "cpu_threshold_percent": 40,
    "idle_seconds_threshold": 30,
    "check_interval": 5,
    "http_host": "127.0.0.1",
    "http_port": 5515,
    "log_file": "zenminer.log",
    "graceful_kill_wait": 6,
    "xmrig_api_host": "127.0.0.1",
    "xmrig_api_port": 3333,
    "api_token": "",
    "wallet_rotation": {
        "enabled": False,
        "interval_minutes": 60.0,
        "min_interval_minutes": 0.5,
        "mode": "sequential",
        "wallets": [],
        "apply_method": "restart"
    }
}

CONFIG_PATH = Path("miner_config.json")


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            logging.warning("Failed to parse miner_config.json: %s", e)
    # validate rotation minimal value
    try:
        wr = cfg.get("wallet_rotation", {}) or {}
        min_iv = float(wr.get("min_interval_minutes", 0.5))
        iv = float(wr.get("interval_minutes", 2.0))
        if iv < min_iv:
            logging.warning(
                "wallet_rotation.interval_minutes < min_interval_minutes: adjusting to min")
            wr["interval_minutes"] = min_iv
            cfg["wallet_rotation"] = wr
            save_config(cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(
            cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        logging.error("Failed to save config: %s", e)
        return False


def setup_logging(path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(path),
            logging.StreamHandler(sys.stdout)
        ]
    )


def create_image(width=64, height=64):
    img = Image.new('RGB', (width, height), (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.text((width*0.22, height*0.12), "Z", fill=(220, 200, 80))
    return img

# -------------------------
# Manager
# -------------------------


class MinerManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.current_threads = None

        # xmrig API state
        self.xmrig_api = None
        self.xmrig_api_available = False
        self._xmrig_api_lock = threading.Lock()
        self._stop_polling = threading.Event()

        # wallet rotator
        self._wallet_rotator_thread = None
        self._wallet_rotator_stop = threading.Event()

    def _xmrig_api_base(self):
        host = self.cfg.get("xmrig_api_host", "127.0.0.1")
        port = int(self.cfg.get("xmrig_api_port", 3333))
        return f"http://{host}:{port}"

    def build_cmd(self, threads: int):
        xmrig = self.cfg["xmrig_path"]
        args = [
            str(xmrig),
            "-o", self.cfg["pool"],
            "-u", self.cfg["wallet"],
            "-p", "office",
            f"--threads={threads}"
        ]
        if self.cfg.get("xmrig_extra_args"):
            args.extend(self.cfg["xmrig_extra_args"].split())
        return args

    def start(self, threads: Optional[int] = None):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                logging.info("xmrig already running")
                return {"status": "already_running"}
            if threads is None:
                threads = self.cfg.get("high_threads", 4)
            cmd = self.build_cmd(threads)
            logging.info("Starting xmrig: %s", " ".join(cmd))
            logfile = open("xmrig_stdout.log", "a")
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=logfile, stderr=logfile)
                self.current_threads = threads
                return {"status": "started", "threads": threads}
            except FileNotFoundError as e:
                logging.error("xmrig binary not found: %s", e)
                self.proc = None
                return {"status": "error", "error": str(e)}

    def stop(self):
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                logging.info("xmrig not running")
                return {"status": "not_running"}
            logging.info("Stopping xmrig (graceful)...")
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=self.cfg.get(
                        "graceful_kill_wait", 6))
                except subprocess.TimeoutExpired:
                    logging.warning("xmrig did not exit, killing...")
                    self.proc.kill()
                    self.proc.wait(timeout=3)
                return {"status": "stopped"}
            except Exception as e:
                logging.exception("Error stopping xmrig: %s", e)
                return {"status": "error", "error": str(e)}
            finally:
                self.proc = None
                self.current_threads = None

    def restart(self, threads: Optional[int] = None):
        logging.info("Restarting xmrig...")
        self.stop()
        time.sleep(0.2)
        return self.start(threads)

    def status(self):
        running = self.proc is not None and self.proc.poll() is None
        return {
            "running": running,
            "pid": self.proc.pid if self.proc else None,
            "threads": self.current_threads
        }

    # -------------------------
    # XMRig API helpers
    # -------------------------
    def fetch_xmrig_summary_once(self, timeout=2.0):
        url = self._xmrig_api_base() + "/1/summary"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                data = json.loads(text)
                return data
        except Exception as e:
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
                logging.debug("Fetched XMRig API summary successfully")
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

    # -------------------------
    # set_threads with API attempt + fallback
    # -------------------------
    def try_set_threads_via_xmrig_api(self, threads: int, timeout=2.0):
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

        updated = False
        cpu = cur.get("cpu") or {}
        profiles = cpu.get("profiles") or cpu.get("profile") or {}
        if isinstance(profiles, dict):
            # try known keys
            for k, v in profiles.items():
                if isinstance(v, dict) and "threads" in v:
                    v["threads"] = threads
                    updated = True
        if not updated and "threads" in cpu:
            cpu["threads"] = threads
            updated = True
        if not updated and "max-threads-hint" in cpu:
            cpu["max-threads-hint"] = int(threads)
            updated = True

        if not updated:
            raise RuntimeError(
                "Couldn't find safe threads field in XMRig config")

        try:
            data = json.dumps(cur).encode("utf-8")
            req2 = urllib.request.Request(cfg_url, data=data, method="PUT",
                                          headers={"Content-Type": "application/json", "User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                _ = resp2.read()
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to PUT xmrig config: {e}")

    def set_threads(self, threads: int):
        try:
            if self.xmrig_api_available:
                try:
                    self.try_set_threads_via_xmrig_api(threads)
                    logging.info("Changed threads via XMRig API: %s", threads)
                    self.current_threads = threads
                    return {"status": "ok", "method": "api", "threads": threads}
                except Exception as e:
                    logging.warning("XMRig API threads change failed: %s", e)
            res = self.restart(threads)
            return {"status": "ok", "method": "restart", "threads": threads, "result": res}
        except Exception as e:
            logging.exception("set_threads failed: %s", e)
            return {"status": "error", "error": str(e)}

    # -------------------------
    # Wallet rotator
    # -------------------------
    def wallet_rotator_loop(self):
        rot = self.cfg.get("wallet_rotation", {}) or {}
        wallets = rot.get("wallets") or []
        if not wallets:
            logging.info("Wallet rotator enabled but no wallets configured")
            return
        min_iv = float(rot.get("min_interval_minutes", 0.5))
        iv = float(rot.get("interval_minutes", 2.0))
        if iv < min_iv:
            logging.warning("wallet interval < min interval, adjusting")
            iv = min_iv

        idx = 0
        mode = rot.get("mode", "sequential")
        logging.info(
            "Wallet rotator started: mode=%s interval_minutes=%s wallets=%d", mode, iv, len(wallets))
        while not self._wallet_rotator_stop.is_set():
            next_wallet = None
            if mode == "random":
                next_wallet = random.choice(wallets)
            else:
                next_wallet = wallets[idx % len(wallets)]
                idx += 1
            logging.info("Rotator applying wallet: %s", next_wallet)
            # apply: update config, save and either restart or try API
            self.cfg["wallet"] = next_wallet
            save_config(self.cfg)
            method = rot.get("apply_method", "restart")
            if method == "api" and self.xmrig_api_available:
                try:
                    base = self._xmrig_api_base()
                    cfg_url = base + "/1/config"
                    try:
                        req = urllib.request.Request(
                            cfg_url, headers={"User-Agent": "ZenMiner/1.0"})
                        with urllib.request.urlopen(req, timeout=2) as resp:
                            current = json.loads(
                                resp.read().decode("utf-8", errors="replace"))
                        # apply wallet conservatively
                        if isinstance(current, dict):
                            # try connection / pools structure
                            if "connection" in current and isinstance(current["connection"], dict):
                                if "user" in current["connection"]:
                                    current["connection"]["user"] = next_wallet
                                elif "wallet" in current["connection"]:
                                    current["connection"]["wallet"] = next_wallet
                            elif "wallet" in current:
                                current["wallet"] = next_wallet
                            data = json.dumps(current).encode("utf-8")
                            req2 = urllib.request.Request(cfg_url, data=data, method="PUT",
                                                          headers={"Content-Type": "application/json", "User-Agent": "ZenMiner/1.0"})
                            with urllib.request.urlopen(req2, timeout=2) as r2:
                                _ = r2.read()
                            logging.info("Applied wallet via XMRig API")
                        else:
                            logging.warning(
                                "Unexpected XMRig config format when applying wallet")
                            self.restart()
                    except Exception as e:
                        logging.warning("Wallet apply via API failed: %s", e)
                        self.restart()
                except Exception:
                    logging.exception(
                        "Error while applying wallet via API, restarting fallback")
                    self.restart()
            else:
                logging.info(
                    "Wallet rotator using restart to apply new wallet")
                self.restart()
            # sleep interval
            for _ in range(int(iv * 60)):
                if self._wallet_rotator_stop.is_set():
                    break
                time.sleep(1)

    def start_wallet_rotator(self):
        rot = self.cfg.get("wallet_rotation", {}) or {}
        if not rot.get("enabled") or not rot.get("wallets"):
            logging.info("Wallet rotator disabled or no wallets")
            return
        self._wallet_rotator_stop.clear()
        t = threading.Thread(target=self.wallet_rotator_loop, daemon=True)
        t.start()
        self._wallet_rotator_thread = t

    def stop_wallet_rotator(self):
        self._wallet_rotator_stop.set()


# -------------------------
# Flask app & helpers
# -------------------------
app = Flask("zenminer")
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
    return jsonify(s)


@app.route("/config", methods=["GET", "POST"])
def api_config():
    global cfg
    if request.method == "GET":
        return jsonify(cfg)
    else:
        new = request.json
        if not isinstance(new, dict):
            return jsonify({"status": "error", "error": "invalid payload"}), 400
        cfg.update(new)
        saved = save_config(cfg)
        if not saved:
            return jsonify({"status": "error", "error": "save_failed"}), 500
        return jsonify({"status": "ok", "config": cfg})


@app.route("/apply_config", methods=["POST"])
@require_token
def api_apply_config():
    body = request.json or {}
    restart_flag = bool(body.get("restart", False))
    saved = save_config(cfg)
    if not saved:
        return jsonify({"status": "error", "error": "save_failed"}), 500
    if restart_flag:
        manager.restart()
    # if wallet_rotation changed, restart rotator
    manager.stop_wallet_rotator()
    manager.start_wallet_rotator()
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
    res = manager.start(threads)
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
    res = manager.restart(threads)
    return jsonify(res)


@app.route("/set_threads", methods=["POST"])
@require_token
def api_set_threads():
    payload = request.json or {}
    threads = payload.get("threads")
    if threads is None:
        return jsonify({"status": "error", "error": "missing threads parameter"}), 400
    res = manager.set_threads(int(threads))
    return jsonify(res)

# -------------------------
# Tray + helpers
# -------------------------


def open_config_in_editor():
    path = Path("miner_config.json").resolve()
    try:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logging.error("Couldn't open config in editor: %s", e)


def tray_worker(mm: MinerManager):
    icon = pystray.Icon("zenminer")
    icon.icon = create_image()
    icon.title = "ZenMiner"

    def on_start(icon, item):
        logging.info("Tray -> start")
        mm.start()

    def on_stop(icon, item):
        logging.info("Tray -> stop")
        mm.stop()

    def on_status(icon, item):
        s = mm.status()
        logging.info("Tray -> status: %s", s)

    def on_restart(icon, item):
        logging.info("Tray -> restart")
        mm.restart()

    def on_apply_config(icon, item):
        logging.info("Tray -> apply config")
        try:
            # post apply_config restart=true
            import requests as _r
            url = f"http://{cfg.get('http_host','127.0.0.1')}:{cfg.get('http_port',5515)}/apply_config"
            _r.post(url, json={"restart": True}, timeout=3)
        except Exception as e:
            logging.warning("Tray apply_config failed: %s", e)

    def on_exit(icon, item):
        logging.info("Tray -> exit")
        try:
            mm.stop()
        except Exception:
            pass
        mm.stop_xmrig_api_poller()
        mm.stop_wallet_rotator()
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Start", on_start),
        pystray.MenuItem("Stop", on_stop),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Set threads", pystray.Menu(
            pystray.MenuItem("1", lambda i, it: mm.set_threads(1)),
            pystray.MenuItem("2", lambda i, it: mm.set_threads(2)),
            pystray.MenuItem("4", lambda i, it: mm.set_threads(4)),
            pystray.MenuItem("Custom (edit config)", lambda i,
                             it: open_config_in_editor())
        )),
        pystray.MenuItem("Edit config", lambda i, it: open_config_in_editor()),
        pystray.MenuItem("Apply config (restart)", on_apply_config),
        pystray.MenuItem("Status (log)", on_status),
        pystray.MenuItem("Exit", on_exit)
    )
    icon.menu = menu
    icon.run()


def run_flask(host, port):
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    setup_logging(cfg.get("log_file", "zenminer.log"))
    logging.info("ZenMiner starting...")
    logging.info("Config: %s", json.dumps(cfg, indent=2, ensure_ascii=False))

    manager.start_xmrig_api_poller()
    manager.start_wallet_rotator()

    flask_thread = threading.Thread(target=run_flask, args=(cfg.get(
        "http_host", "127.0.0.1"), int(cfg.get("http_port", 5515))), daemon=True)
    flask_thread.start()

    # run tray in main thread
    try:
        tray_worker(manager)
    except KeyboardInterrupt:
        logging.info("Interrupted")
        manager.stop_xmrig_api_poller()
        manager.stop_wallet_rotator()
        manager.stop()
        sys.exit(0)
