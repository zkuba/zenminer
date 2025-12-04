#!/usr/bin/env python3
# miner_controller.py
"""
Lightweight office-friendly XMRig controller (simple version).
This script starts/stops xmrig based on CPU load and idle time.
Simple, restart-based thread adjustment.
"""

import os
import time
import logging
import subprocess
import json
from pathlib import Path
from typing import Optional

try:
    import psutil
except Exception:
    raise SystemExit("Install requirements: pip install psutil")

DEFAULT_CONFIG = {
    "xmrig_path": "xmrig",
    "pool": "pool.supportxmr.com:3333",
    "wallet": "YOUR_WALLET",
    "xmrig_extra_args": "--donate-level=0",
    "high_threads": 4,
    "low_threads": 1,
    "cpu_threshold_percent": 40,
    "idle_seconds_threshold": 30,
    "check_interval": 5,
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


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.current_threads = None

    def build_cmd(self, threads):
        args = [
            self.cfg["xmrig_path"],
            "-o", self.cfg["pool"],
            "-u", self.cfg["wallet"],
            "-p", "office",
            f"--threads={threads}"
        ]
        if self.cfg.get("xmrig_extra_args"):
            args.extend(self.cfg["xmrig_extra_args"].split())
        return args

    def start(self, threads=None):
        if self.proc and self.proc.poll() is None:
            logging.info("xmrig already running")
            return
        if threads is None:
            threads = self.cfg.get("high_threads", 4)
        cmd = self.build_cmd(threads)
        logging.info("Starting xmrig: %s", " ".join(cmd))
        logfile = open("xmrig_stdout.log", "a")
        try:
            self.proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile)
            self.current_threads = threads
        except FileNotFoundError as e:
            logging.error("xmrig binary not found: %s", e)
            self.proc = None

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            logging.info("xmrig not running")
            return
        logging.info("Terminating xmrig...")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=self.cfg.get("graceful_kill_wait", 6))
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        finally:
            self.proc = None
            self.current_threads = None

    def decide_threads(self):
        batt = None
        try:
            batt = psutil.sensors_battery()
        except Exception:
            batt = None
        if batt and not batt.power_plugged:
            return self.cfg.get("low_threads", 1)
        cpu = psutil.cpu_percent(interval=1)
        if cpu > self.cfg.get("cpu_threshold_percent", 40):
            return self.cfg.get("low_threads", 1)
        # idle detection not implemented here — assume idle
        return self.cfg.get("high_threads", 4)

    def monitor(self):
        logging.info("Controller monitor started")
        while True:
            desired = self.decide_threads()
            if desired != self.current_threads:
                logging.info("Applying threads change: %s -> %s",
                             self.current_threads, desired)
                self.stop()
                time.sleep(0.2)
                self.start(desired)
            time.sleep(self.cfg.get("check_interval", 5))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    c = Controller(cfg)
    c.start()
    try:
        c.monitor()
    except KeyboardInterrupt:
        c.stop()
        logging.info("Stopped by user")
