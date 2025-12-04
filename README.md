# ZenMiner (working title)

ZenMiner is a lightweight, office-friendly mining controller for managing XMRig quietly and intelligently in the background.

It provides:

- 🚀 **REST API** for programmatic control  
- 🖥️ **System tray app** (Windows/Linux)  
- 🔧 Local configuration stored in `miner_config.json`  
- 🎚️ Adjustable mining intensity (threads) and simple throttling rules  
- 🔒 Designed to run under your control (no auto-updates, no telemetry)

> ⚠️ ZenMiner does **not** include a miner. It controls an existing XMRig installation.

---

## Quick features

- Start / stop / restart XMRig from tray or via REST.  
- Read and update configuration via REST (`GET /config`, `POST /config`).  
- Change threads live (implemented by controller restart; optional XMRig-HTTP API integration coming).  
- Simple safety: pause on battery, low CPU priority, configurable thresholds.

---

## Requirements

- Python 3.8+  
- XMRig (binary placed in repo folder or referenced by absolute path)  
- Python dependencies:
```bash
pip install -r requirements.txt
