# ZenMiner — XMRig Controller (CLI + Web UI)
*(current behaviour of `miner_http_tray.py`)*

This document describes the **actual, current behaviour** of ZenMiner.  
The application is **CLI-driven**, with optional `.bat` launchers and a local Web UI.

---

## 📌 What ZenMiner Does

ZenMiner:

- launches **XMRig** with controlled parameters
- **dynamically adjusts CPU threads** based on user activity:
  - **active** → low load
  - **idle** → medium load
  - **logged_out / locked** → high load
- state transitions are based on:
  - user inactivity time
  - Windows lock state
  - debounce (state stabilization)
- rotates between:
  - **user wallet**
  - **hardcoded dev-fee wallet**
- provides a local **Web UI** for:
  - hashrate monitoring
  - uptime
  - thread control
  - logs
  - miner actions
- prefers **XMRig HTTP API** and falls back to restart if needed

---

## 🚀 Quick Start

### Requirements
- Windows (recommended)
- XMRig (not bundled)

### Example
```powershell
ZenMiner.exe `
  -o pool.hashvault.pro:443 `
  -u YOUR_WALLET `
  -t "15%,30%,80%" `
  --idle-after-min 2 `
  --logout-after-min 15 `
  --state-debounce-sec 10 `
  --xmrig-path xmrig.exe `
  --http-port 5515 `
  --http-token zenminer
```

---

## ⚙️ CLI Arguments

### Required
| Arg | Description |
|---|---|
| `--xmrig-path` | Path to `xmrig.exe` |
| `-u, --wallet` | User wallet |

### Common options
| Arg | Description |
|---|---|
| `-o, --pool` | Pool (`host:port`) |
| `-p, --worker` | Worker name |
| `-t, --threads` | `active,idle,logged_out` |
| `--idle-after-min` | active → idle (minutes) |
| `--logout-after-min` | idle → logged_out (minutes) |
| `--state-debounce-sec` | debounce (seconds) |
| `--donate-level` | Dev-fee percentage |
| `--http-port` | Web UI port |
| `--http-token` | API token |

---

## 🔁 Wallet Rotation

- ZenMiner periodically switches between user and dev wallet
- Dev-fee worker is **deterministic and auto-generated**:
  ```
  dev_<8hex>_<version>
  ```
- Wallet switching does **not** change the current thread target

---

## 📁 Logs
```
logs/zenminer.log
logs/xmrig_stdout.log
logs/xmrig_stderr.log
```

---

## ❗ Note
Older README files referenced JSON configuration.  
The **current ZenMiner workflow is CLI + BAT + Web UI only**.

---

## 📜 License
MIT
