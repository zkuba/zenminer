
# ZenMiner — Runtime & WebUI CLI Miner Controller
Office-friendly XMRig controller that auto-adjusts mining speed, resource usage and noise based on user activity. REST API + system tray UI.

*(current behaviour of `miner_http_tray.py`)*

This README documents how the current version of **ZenMiner** works.  
Older README files referred to a **JSON-based config system**, but this version of the miner uses a **CLI-driven launcher + Web UI**.  
All behaviour described below matches the actual Python code in `miner_http_tray.py`.

---

# 📌 What ZenMiner Does
ZenMiner:

- Launches **XMRig** with controlled parameters  
- Adjusts **threads dynamically** based on user activity:
  - **active** → low threads  
  - **idle** → medium  
  - **logged_out / locked** → high  
- Rotates between:
  - **user wallet**  
  - **dev-fee wallet** (immutable in code)
- Provides a local **Web UI**:
  - monitoring hashrate  
  - checking uptime  
  - adjusting threads  
  - viewing logs  
  - issuing miner actions  
- Stores logs in a `logs/` directory  
- Tries to configure XMRig via **HTTP API**, falling back to restart if needed.

---

# 🚀 Quick Start

## Requirements
- Windows (recommended) or Linux  
- XMRig (not included — user supplies path)

## Example Run
```powershell
ZenMiner.exe `
  --xmrig-path "C:\miners\xmrig\xmrig.exe" `
  -u YOUR_WALLET `
  -t "1,2,4" `
  --http-port 5515 `
  --http-token zenminer
```

---

# ⚙️ CLI Arguments

### Required
| Arg | Description |
|-----|-------------|
| `--xmrig-path` | Full path to `xmrig.exe` |
| `-u, --wallet` | User wallet address |

### Common Options
| Arg | Description |
|-----|-------------|
| `-o, --pool` | Pool URL (`host:port`) |
| `-p, --worker` | Worker name (default `%COMPUTERNAME%`) |
| `-t, --threads` | Thread spec (`active,idle,logged_out`) |
| `--tp` | Rotator cycle time (minutes) |
| `--donate-level` | Dev-fee percentage |
| `--xmrig-http-port` | XMRig HTTP port (default 18080) |
| `--http-port` | Web UI port (default 5515) |
| `--http-token` | Token for Web UI and XMRig API |
| `--no-tray` | Disable tray icon |
| `--http-debug` | More verbose API logging |

---

# 🧮 Threads Specification

## Integer mode
Order: `active, idle, logged_out`

Example:
```
-t "1,2,4"
```

## Percent mode
Example:
```
-t "25%,50%,100%"
```

### How percent works
Percent → fraction (`25%` → 0.25)  
Threads = `round(fraction * logical_cpu_count)`

Example for **16 logical CPUs**:

| Percent | Threads |
|--------|---------|
| 25% | 4 |
| 50% | 8 |
| 100% | 16 |

### Windows BAT escaping
In `.bat`, escape `%` as `%%`:
```
ZenMiner.exe -t "25%%,50%%,100%%"
```

---

# 🌐 Web UI & API

### URL
```
http://127.0.0.1:<http-port>/
```

### Authentication
Accepted forms:
- `X-Auth-Token: <token>`
- `Authorization: Bearer <token>`
- `?token=<token>`

### Endpoints
| Method | Path | Description |
|--------|-------|-------------|
| `GET /status` | Miner status: uptime, threads, wallet, hashrate |
| `POST /control` | Actions: start/stop/restart/set_threads/rotate_now |
| `GET /log_lines` | Last N lines of log |
| `GET /download_log` | Download ZenMiner log |
| `GET /download_xmrig` | Download XMRig logs |

---

# 🔁 Wallet Rotator

- Rotates between **user wallet** and **hardcoded dev-fee wallet**  
- Tries API first (`PUT /1/config`), restarts XMRig if API fails  
- Dev-worker is auto-generated:  
  ```
  <sha256(wallet)[:8]>_<version>
  ```

---

# 📁 Logging

### App log (rotating)
```
logs/zenminer.log
```
Rotation:
- max size: 5 MB  
- old logs: 5 backups  

### XMRig logs
```
logs/xmrig_stdout.log
logs/xmrig_stderr.log
```

Web UI can download these logs.

---

# 🔄 XMRig Restart Logic
ZenMiner attempts to configure threads or wallet via:
```
PUT /1/config
```
If API returns errors or fails completely → XMRig is restarted.

---

# 🧩 Worker Handling
- User worker = CLI (`--worker`)  
- Dev-fee worker = auto-generated (hash + version)  
- Worker is saved in controller state so future restarts use the same worker.

---

# ❗ Notes About Old JSON README
Older documentation referenced JSON config files.  
The **current version** is entirely **CLI + Web UI** driven.  
JSON is no longer part of the workflow.

---

# 📜 License
MIT (or repository license)
