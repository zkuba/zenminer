# ZenMiner — kontroler XMRig (CLI + Web UI)  
*(aktualne zachowanie `miner_http_tray.py`)*

Ten dokument opisuje **rzeczywistą**, aktualną funkcjonalność ZenMiner.  
Poprzednie README mówiły o konfiguracji JSON — ta wersja aplikacji działa w oparciu o **parametry CLI** oraz **Web UI**.  
Opis poniżej odpowiada dokładnie temu, co robi obecny kod Python.

---

# 📌 Co robi ZenMiner?

ZenMiner:

- uruchamia **XMRig** z określonym zestawem parametrów  
- **automatycznie dostosowuje liczbę wątków** w zależności od aktywności użytkownika:
  - **active** — minimalne obciążenie  
  - **idle** — średnie  
  - **logged_out / locked** — maksymalne  
- rotuje pomiędzy:
  - **portfelem użytkownika**  
  - **stałym portfelem dev-fee** (w kodzie)  
- zapewnia **Web UI** umożliwiające:
  - podgląd hashrate  
  - sprawdzanie uptime  
  - zmianę liczby wątków  
  - przegląd logów  
  - start/stop/restart XMRig  
- zapisuje logi w katalogu `logs/`  
- próbuje zmieniać konfigurację XMRig przez **HTTP API**, a jeśli API zawiedzie — restartuje XMRig.

---

# 🚀 Szybki start

## Wymagania
- Windows (zalecane) lub Linux  
- XMRig (nie jest dołączony — użytkownik podaje pełną ścieżkę)

## Przykład uruchomienia
```powershell
ZenMiner.exe `
  --xmrig-path "C:\miners\xmrig\xmrig.exe" `
  -u TWÓJ_WALLET `
  -t "1,2,4" `
  --http-port 5515 `
  --http-token zenminer
```

---

# ⚙️ Parametry CLI

## Wymagane
| Parametr | Opis |
|----------|------|
| `--xmrig-path` | Pełna ścieżka do `xmrig.exe` |
| `-u, --wallet` | Adres portfela użytkownika |

## Najważniejsze opcje
| Parametr | Opis |
|----------|------|
| `-o, --pool` | Adres poola (host:port) |
| `-p, --worker` | Nazwa workera (domyślnie `%COMPUTERNAME%`) |
| `-t, --threads` | Specyfikacja wątków (active,idle,logged_out) |
| `--tp` | Czas trwania cyklu rotatora (minuty) |
| `--donate-level` | Procent dev-fee w cyklu |
| `--xmrig-http-port` | Port API XMRig (domyślnie 18080) |
| `--http-port` | Port Web UI (domyślnie 5515) |
| `--http-token` | Token dla Web UI i XMRig API |
| `--no-tray` | Bez ikony w trayu |
| `--http-debug` | Więcej logów HTTP |

---

# 🧮 Specyfikacja wątków (`--threads`)

## Tryb liczbowy
```
-t "1,2,4"
```
Kolejność:
1. active  
2. idle  
3. logged_out

## Tryb procentowy
```
-t "25%,50%,100%"
```

### Jak działają procenty?
- `25%` → 0.25  
- ZenMiner mnoży to przez **liczbę logicznych CPU** (`multiprocessing.cpu_count()`)

Przykład dla CPU z **16 logicznymi wątkami**:

| Procent | Wynik |
|---------|--------|
| 25% | 4 wątki |
| 50% | 8 wątków |
| 100% | 16 wątków |

### Uwaga na pliki `.bat` (Windows CMD)
`%` trzeba podwoić:

```
ZenMiner.exe -t "25%%,50%%,100%%"
```

W PowerShell NIE trzeba podwajać.

---

# 🌐 Web UI i API

### Adres Web UI
```
http://127.0.0.1:<http-port>/
```

### Autoryzacja
Jeśli ustawiono token:

- `X-Auth-Token: <token>`  
- lub `Authorization: Bearer <token>`  
- lub `?token=<token>` w URL  

### Endpoints
| Method | Path | Opis |
|--------|-------|------|
| `GET /status` | status XMRig (uptime, threads, wallet, hashrate) |
| `POST /control` | start / stop / restart / set_threads / rotate_now |
| `GET /log_lines` | ostatnie N linii logów |
| `GET /download_log` | pobranie logu aplikacji |
| `GET /download_xmrig` | pobranie logów XMRig |

---

# 🔁 Rotator portfeli (dev-fee)

- Przełącza pomiędzy **portfelem użytkownika** i **portfelem dev-fee** (zaszytym w kodzie)  
- Najpierw próbuje zmienić konfigurację przez **XMRig API**  
- Jeśli to się nie uda → restartuje XMRig  
- Worker podczas dev-fee jest automatycznie generowany:
  ```
  <sha256(wallet)[:8]>_<wersja_aplikacji>
  ```

---

# 📁 Logi

### Log aplikacji (rotowany)
```
logs/zenminer.log
```
Parametry rotacji:
- max 5 MB  
- 5 kopii zapasowych  

### Logi XMRig
```
logs/xmrig_stdout.log
logs/xmrig_stderr.log
```
Dostępne do pobrania z Web UI.

---

# 🔄 Restart XMRig
ZenMiner próbuje ustawić portfel/wątki poprzez:
```
PUT /1/config
```
Jeśli API nie odpowiada lub zgłasza błąd → wykonywany jest **restart XMRig**.

---

# 🧩 Obsługa workera

- Worker użytkownika = wartość z `--worker`  
- Worker dev-fee = automatycznie generowany (hash + wersja)  
- Po udanej zmianie przez API worker jest zapisywany w stanie aplikacji, aby restart XMRig uruchomił go z poprawną wartością.

---

# ❗ Informacja o starym README
Starsze wersje opisywały system konfiguracji *JSON*.  
Aktualna wersja ZenMiner działa wyłącznie na:

- parametrach CLI  
- Web UI  
- API runtime  

JSON nie jest już elementem standardowego workflow.

---

# 📜 Licencja
MIT / zgodnie z plikiem licencji w repozytorium.
