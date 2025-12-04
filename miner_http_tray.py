#!/usr/bin/env python3
# miner_http_tray.py
# ZenMiner - REST + Tray controller (with XMRig HTTP API read-only integration)

import os
import sys
import time
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional
from flask import Flask, jsonify, request

# --- standard libs for HTTP to XMRig API ---
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

# default config
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
    "http_port": 5000,
    "log_file": "zenminer.log",
    "graceful_kill_wait": 6,
    # xmrig API defaults (can be overridden in miner_config.json)
    "xmrig_api_host": "127.0.0.1",
    "xmrig_api_port": 3333
}

CONFIG_PATH = Path("miner_config.json")


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            logging.warning("Failed to parse miner_config.json: %s", e)
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

    # --- XMRig HTTP API polling & access ---
    def _xmrig_api_base(self):
        host = self.cfg.get("xmrig_api_host", "127.0.0.1")
        port = int(self.cfg.get("xmrig_api_port", 3333))
        return f"http://{host}:{port}"

    def fetch_xmrig_summary_once(self, timeout=2.0):
        """
        Try to GET /1/summary from XMRig API. Returns parsed JSON or raises.
        """
        url = self._xmrig_api_base() + "/1/summary"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ZenMiner/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                # decode bytes -> string
                text = raw.decode("utf-8", errors="replace")
                data = json.loads(text)
                return data
        except urllib.error.HTTPError as he:
            # API exists but returned non-200 (we treat as not available)
            logging.debug("XMRig API HTTPError %s: %s", he.code, he.reason)
            raise
        except urllib.error.URLError as ue:
            logging.debug("XMRig API URLError: %s", ue)
            raise
        except Exception as e:
            logging.debug("XMRig API parse/other error: %s", e)
            raise

    def xmrig_api_poll_loop(self):
        """
        Background thread that periodically polls XMRig API /1/summary and stores result.
        """
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
            # sleep with early exit
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


# Flask app
app = Flask("zenminer")
cfg = load_config()
manager = MinerManager(cfg)


@app.route("/status", methods=["GET"])
def api_status():
    s = manager.status()
    # add config (non-sensitive)
    s["config"] = cfg
    # attach xmrig api info if available
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


@app.route("/start", methods=["POST"])
def api_start():
    data = request.json or {}
    threads = data.get("threads")
    res = manager.start(threads)
    return jsonify(res)


@app.route("/stop", methods=["POST"])
def api_stop():
    res = manager.stop()
    return jsonify(res)


@app.route("/restart", methods=["POST"])
def api_restart():
    data = request.json or {}
    threads = data.get("threads")
    res = manager.restart(threads)
    return jsonify(res)


@app.route("/set_threads", methods=["POST"])
def api_set_threads():
    payload = request.json or {}
    threads = payload.get("threads")
    if threads is None:
        return jsonify({"status": "error", "error": "missing threads parameter"}), 400
    # Keep current behavior: restart to apply threads.
    res = manager.restart(threads)
    return jsonify(res)

# Tray


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

    def on_exit(icon, item):
        logging.info("Tray -> exit")
        try:
            mm.stop()
        except Exception:
            pass
        mm.stop_xmrig_api_poller()
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Start", on_start),
        pystray.MenuItem("Stop", on_stop),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Status (log)", on_status),
        pystray.MenuItem("Exit", on_exit)
    )
    icon.menu = menu
    icon.run()


def run_flask(host, port):
    # Note: use_reloader=False to avoid double-start in threads
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    setup_logging(cfg.get("log_file", "zenminer.log"))
    logging.info("ZenMiner starting...")
    logging.info("Config: %s", json.dumps(cfg, indent=2, ensure_ascii=False))

    # start xmrig API poller
    manager.start_xmrig_api_poller()

    flask_thread = threading.Thread(target=run_flask, args=(cfg.get(
        "http_host", "127.0.0.1"), int(cfg.get("http_port", 5000))), daemon=True)
    flask_thread.start()

    # run tray in main thread
    try:
        tray_worker(manager)
    except KeyboardInterrupt:
        logging.info("Interrupted")
        manager.stop_xmrig_api_poller()
        manager.stop()
        sys.exit(0)
