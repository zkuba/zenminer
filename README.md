# ZenMiner

⚠️ **Windows Defender / Antivirus warning**  
ZenMiner and similar miner-managers are frequently flagged by Windows Defender and other antivirus engines as potential threats (false positive). This project does **not** ship XMRig by default; users must download XMRig themselves from the official repository. If Windows Defender quarantines or removes `ZenMiner.exe`, follow the Troubleshooting section to restore and add an exclusion for the application folder.

---

**ZenMiner** — lightweight XMRig manager for Windows featuring adaptive CPU thread control, automatic wallet rotation (user/dev), Web UI, and system tray integration.

## Version
Version is detected from Git (`git describe`) or from the `VERSION` file.  
Current release: **0.1.0** (update `VERSION` before creating a release).

---

## Key features
- Wallet rotation: starts always with the **user wallet**, periodically switches to an immutable **dev wallet** (dev fee).
- Dev wallet is hardcoded and **immutable at runtime** (cannot be changed via API or UI).
- AdaptiveThreadController with 3 states:
  - `active` → low threads
  - `idle`  → higher threads
  - `logged_out` (screen locked) → stop or configurable thread count
  - Uses `GetLastInputInfo` and lock-state detection on Windows.
- Prefers XMRig HTTP API for smooth reconfiguration; restart used as a safe fallback.
- Web UI (Flask) and system tray (pystray).
- Log rotation (RotatingFileHandler) — logs won't grow forever.
- Worker naming:
  - user period → configured `worker_name` or `"zenminner"`
  - dev period  → hashed ID derived from wallet + version (`user_<8hex>_v<version>`)

---

## Distribution policy — IMPORTANT
- **Do not include** `xmrig.exe` in the public release. XMRig is a third-party binary and is often flagged by AV; users must download it themselves from the official XMRig GitHub Releases page.
- Provide `miner_config.json.example` in the release. Users must copy it to `miner_config.json` and set `xmrig_path` and `wallet`.
- Add SHA256 checksum for any published EXE in release notes, so users can verify integrity.

---

## Quickstart (end user, EXE)
1. Download the release ZIP from GitHub Releases.
2. Unpack to a folder (e.g. `C:\ProgramData\ZenMiner` or `D:\Tools\ZenMiner`).
3. Copy `miner_config.json.example` → `miner_config.json`.
4. Edit `miner_config.json`:
   - Set `"wallet"` to your wallet address.
   - Set `"xmrig_path"` to a full path to XMRig binary (recommended), e.g. `"C:\\miners\\xmrig-6.24.0\\xmrig.exe"`, or `"xmrig.exe"` if you copied xmrig into the same folder as `ZenMiner.exe`.
5. Run `ZenMiner.exe`.
6. Open Web UI: <http://127.0.0.1:5515/ui/>

---

## Development (run from source)
1. Create venv and install deps:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. Run:
   ```powershell
   python miner_http_tray.py
   ```
3. Web UI: <http://127.0.0.1:5515/ui/>

---

## Building EXE (PyInstaller) — recommended flow
**Use `build.ps1`** included in repo. It creates `.venv`, installs deps, runs PyInstaller and packages a ZIP into `/release`.

Manual steps (if you need fine control):
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --name ZenMiner-0.1.0 --add-data "web;web" --add-data "miner_config.json.example;." miner_http_tray.py
```
After building:
- EXE will be in `dist\ZenMiner-0.1.0.exe`.
- Create a ZIP with `ZenMiner-0.1.0.exe`, `miner_config.json.example`, `README.md`, `README_PL.md`, and `web/` (if not embedded).

---

## Configuration (important fields)
Example entries in `miner_config.json`:
```json
"xmrig_path": "C:\\miners\\xmrig-6.24.0\\xmrig.exe",
"pool": "pool.supportxmr.com:3333",
"wallet": "YOUR_WALLET_ADDRESS",
"worker_name": "",
"active_threads": 1,
"idle_threads": 4,
"logged_out_threads": 0,
"idle_threshold_seconds": 300,
"logged_out_threshold_seconds": 1800,
"debounce_seconds": 120
```
Notes:
- `xmrig_path` should point to the **executable file** (preferred). If you use `"xmrig.exe"` then place the XMRig binary next to `ZenMiner.exe`.
- `worker_name` if set will be used during user period; otherwise it defaults to `zenminner`.

---

## Troubleshooting / Windows Defender
If `ZenMiner.exe` is quarantined or removed:
1. Open **Windows Security** → **Virus & threat protection** → **Protection history**.
2. Find the ZenMiner entry → **Restore** and optionally **Allow on device**.
3. Add an exclusion for the folder where you keep ZenMiner:
   - GUI: Virus & threat protection → Manage settings → Exclusions → Add an exclusion → **Folder**
   - PowerShell (admin):
     ```powershell
     Add-MpPreference -ExclusionPath "C:\path\to\ZenMiner_folder"
     ```

**Important**: Document this step clearly for users — many will encounter AV false positives.

---

## Release checklist (before publishing)
- [ ] Update `VERSION` to the new version (e.g. `0.1.0`).
- [ ] Build EXE and create ZIP (release assets).
- [ ] Ensure `miner_config.json.example` contains placeholders only (no private wallets).
- [ ] Do **not** include `xmrig.exe` in the public release.
- [ ] Compute SHA256 of EXE and place it in release notes.
  ```powershell
  Get-FileHash .\dist\ZenMiner-0.1.0.exe -Algorithm SHA256
  ```
- [ ] Add clear instructions about Defender and how to restore & exclude.
- [ ] Test EXE on a clean Windows VM (no Python / different environment).
- [ ] (Optional) Sign EXE with code-signing certificate.

---

## How worker names are chosen
- During **user** period: use `worker_name` from config if set; otherwise use `zenminner`.
- During **dev** period: worker name is deterministic hashed ID: `user_<8hex>_v<version>` (derived from wallet + app version) — useful for later analytics while preserving privacy.

---

## Security & ethics
- ZenMiner does not store private keys — the `wallet` is a public address only.
- Dev wallet is immutable at runtime; any attempt to change it via API is ignored to prevent hijacking.
- Be transparent in release notes that this is mining-related software; the user must explicitly accept the behavior.

---

## Contributing & Issues
PRs and Issues are welcome. Please include:
- Detailed steps to reproduce.
- OS version, Python version (for dev builds), and logs (zenminer.log).
