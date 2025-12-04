 #!/usr/bin/env python3
# miner_http_tray.py
# ZenMiner - REST + Tray controller

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
    "graceful_kill_wait": 6
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
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
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
    d.text((width*0.22, height*0.12), "Z", fill=(220,200,80))
    return img

class MinerManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.current_threads = None

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
                self.proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile)
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
                    self.proc.wait(timeout=self.cfg.get("graceful_kill_wait", 6))
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

# Flask app
app = Flask("zenminer")
cfg = load_config()
manager = MinerManager(cfg)

@app.route("/status", methods=["GET"])
def api_status():
    s = manager.status()
    s["config"] = cfg
    return jsonify(s)

@app.route("/config", methods=["GET", "POST"])
def api_config():
    global cfg
    if request.method == "GET":
        return jsonify(cfg)
    else:
        new = request.json
        if not isinstance(new, dict):
            return jsonify({"status":"error","error":"invalid payload"}), 400
        cfg.update(new)
        saved = save_config(cfg)
        if not saved:
            return jsonify({"status":"error","error":"save_failed"}), 500
        return jsonify({"status":"ok","config":cfg})

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
        return jsonify({"status":"error","error":"missing threads parameter"}), 400
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

    flask_thread = threading.Thread(target=run_flask, args=(cfg.get("http_host","127.0.0.1"), int(cfg.get("http_port",5000))), daemon=True)
    flask_thread.start()

    # run tray in main thread
    try:
        tray_worker(manager)
    except KeyboardInterrupt:
        logging.info("Interrupted")
        manager.stop()
        sys.exit(0)
