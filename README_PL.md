# ZenMiner

⚠️ **Uwaga — Windows Defender / antywirus**  
Windows Defender oraz inne programy antywirusowe często oznaczają aplikacje zarządzające minerami (takie jak ZenMiner) jako podejrzane — to zwykle *false positive*. Projekt **nie** dołącza binarki XMRig do release — użytkownik pobiera ją samodzielnie.  
Jeśli Windows Defender przeniesie do kwarantanny lub usunie `ZenMiner.exe`, skorzystaj z instrukcji w sekcji „Rozwiązywanie problemów” aby przywrócić plik i dodać wyjątek dla folderu.

---

**ZenMiner** — lekki manager XMRig dla Windows z adaptacyjną kontrolą wątków CPU, rotacją walletów (user/dev), Web UI i ikoną w trayu.

## Wersja
Wersja pobierana jest z GIT (`git describe`) lub z pliku `VERSION`.  
Aktualna wersja: **0.1.0** (zaktualizuj przed release).

---

## Najważniejsze funkcje
- Rotacja walletów: Zawsze zaczyna od **walletem użytkownika**, okresowo przełącza na stały **dev wallet** (dev-fee).
- Dev wallet jest zablokowany i **niemodyfikowalny** w runtime (nie można zmieniać przez API/UI).
- AdaptiveThreadController — 3 stany:
  - `active` → mniej wątków
  - `idle` → więcej wątków
  - `logged_out` (zablokowany ekran) → stop lub konfiguracja
  - Wykrywanie aktywności przez `GetLastInputInfo` i sprawdzenie stanu sesji.
- Preferowane użycie XMRig HTTP API dla płynnych zmian; restart jako fallback.
- Web UI (Flask) oraz tray (pystray).
- Rotacja logów (RotatingFileHandler).
- Nazwa workera:
  - user period → `worker_name` z config lub `zenminner`
  - dev period  → `user_<8hex>_v<version>` (zahashowany + wersja)

---

## Polityka dystrybucji — WAŻNE
- **Nie dołączaj** `xmrig.exe` do publicznego release. Użytkownik ma sam pobrać XMRig z oficjalnego repo (link w README).
- W paczce powinien znaleźć się `miner_config.json.example`. Użytkownik kopiuje go do `miner_config.json` i ustawia `xmrig_path` oraz `wallet`.
- Dołącz SHA256 EXE w notce release, aby użytkownicy mogli zweryfikować plik.

---

## Szybki start (EXE)
1. Pobierz ZIP z GitHub Releases.
2. Rozpakuj do folderu (np. `C:\ProgramData\ZenMiner`).
3. Skopiuj `miner_config.json.example` → `miner_config.json`.
4. W `miner_config.json` ustaw:
   - `"wallet"` — Twój adres.
   - `"xmrig_path"` — preferowana pełna ścieżka do pliku `xmrig.exe`, np. `"C:\\miners\\xmrig-6.24.0\\xmrig.exe"`, lub `"xmrig.exe"` jeżeli umieścisz xmrig obok `ZenMiner.exe`.
5. Uruchom `ZenMiner.exe`.
6. Web UI: <http://127.0.0.1:5515/ui/>

---

## Uruchomienie z kodu (dla dev)
1. Utwórz virtualenv i zainstaluj:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. Uruchom:
   ```powershell
   python miner_http_tray.py
   ```

---

## Budowa EXE (PyInstaller)
Użyj `build.ps1` w repo (zalecane). Automatycznie zbuduje EXE i zapakuje ZIP w `release/`.

Ręcznie:
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --name ZenMiner-0.1.0 --add-data "web;web" --add-data "miner_config.json.example;." miner_http_tray.py
```

---

## Konfiguracja (ważne pola)
Przykład `miner_config.json`:
```json
"xmrig_path": "C:\\miners\\xmrig-6.24.0\\xmrig.exe",
"pool": "pool.supportxmr.com:3333",
"wallet": "TWÓJ_WALLET",
"worker_name": "",
"active_threads": 1,
"idle_threads": 4,
"logged_out_threads": 0,
"idle_threshold_seconds": 300,
"logged_out_threshold_seconds": 1800
```
Uwaga:
- `xmrig_path` powinien wskazywać na plik wykonywalny `xmrig.exe`. Można też ustawić `"xmrig.exe"` i położyć plik obok `ZenMiner.exe`.

---

## Rozwiązywanie problemów / Windows Defender
Jeżeli Defender kwarantannuje plik:
1. Otwórz **Windows Security** → **Virus & threat protection** → **Protection history**.
2. Znajdź wpis → **Restore** / **Allow on device**.
3. Dodaj wyjątek (Exclusion) dla folderu z ZenMiner:
   - GUI: Virus & threat protection → Manage settings → Exclusions → Add an exclusion → Folder
   - PowerShell (admin):
     ```powershell
     Add-MpPreference -ExclusionPath "C:\path\to\ZenMiner_folder"
     ```

---

## Checklist przed publikacją
- [ ] Uaktualnij `VERSION`.
- [ ] Zbuduj EXE i utwórz ZIP z `ZenMiner.exe`, `miner_config.json.example`, `README.md`, `README_PL.md`, `web/`.
- [ ] Nie dołączaj `xmrig.exe` w publicznym release.
- [ ] Oblicz SHA256 EXE i umieść w notce release.
- [ ] Przetestuj EXE na czystej maszynie/VM.
- [ ] Dodaj instrukcję dotyczącą Defender w README.

---

## Bezpieczeństwo / etyka
- ZenMiner nie zapisuje prywatnych kluczy — `wallet` to tylko adres.
- Dev wallet jest stały i niezmienialny przez API.
- Przejrzystość: informuj użytkowników w release, co robi oprogramowanie.

---

## Contributing
Zgłoszenia i PRy mile widziane. Proszę o szczegóły błędu (OS, logi `zenminer.log`, wersja).
