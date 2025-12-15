# ZenMiner — kontroler XMRig (CLI + Web UI)
*(aktualne zachowanie `miner_http_tray.py`)*

Ten dokument opisuje **rzeczywistą, aktualną funkcjonalność ZenMiner**.  
Aplikacja działa w oparciu o **parametry CLI**, **pliki .bat** oraz **Web UI**.

---

## 📌 Co robi ZenMiner?

ZenMiner:

- uruchamia **XMRig** z określonym zestawem parametrów
- **automatycznie dostosowuje liczbę wątków CPU** w zależności od aktywności użytkownika:
  - **active** — minimalne obciążenie
  - **idle** — średnie obciążenie
  - **logged_out / locked** — maksymalne obciążenie
- stany są wyliczane na podstawie:
  - czasu braku aktywności (klawiatura / mysz)
  - stanu blokady Windows
  - debounce (stabilizacja zmiany stanu)
- rotuje pomiędzy:
  - **portfelem użytkownika**
  - **stałym portfelem dev-fee** (zaszytym w kodzie)
- zapewnia **Web UI** umożliwiające:
  - podgląd hashrate
  - sprawdzanie uptime
  - zmianę liczby wątków
  - przegląd logów
  - start / stop / restart XMRig
- zapisuje logi w katalogu `logs/`
- preferuje **XMRig HTTP API** do zmian runtime, a gdy to niemożliwe — wykonuje restart

---

## 🚀 Szybki start

### Wymagania
- Windows (zalecane)
- XMRig (nie jest dołączony)

### Przykład uruchomienia
```powershell
ZenMiner.exe `
  -o pool.hashvault.pro:443 `
  -u TWÓJ_WALLET `
  -t "15%,30%,80%" `
  --idle-after-min 2 `
  --logout-after-min 15 `
  --state-debounce-sec 10 `
  --xmrig-path xmrig.exe `
  --http-port 5515 `
  --http-token zenminer
```

---

## ⚙️ Parametry CLI

### Wymagane
| Parametr | Opis |
|--------|-----|
| `--xmrig-path` | Ścieżka do `xmrig.exe` (pełna lub `xmrig.exe` obok ZenMiner.exe) |
| `-u, --wallet` | Adres portfela użytkownika |

### Najważniejsze opcje
| Parametr | Opis |
|--------|-----|
| `-o, --pool` | Pool (`host:port`), domyślnie polecany `pool.hashvault.pro:443` |
| `-p, --worker` | Nazwa workera (domyślnie `%COMPUTERNAME%`) |
| `-t, --threads` | Wątki `active,idle,logged_out` |
| `--idle-after-min` | Czas (min) active → idle |
| `--logout-after-min` | Czas (min) idle → logged_out |
| `--state-debounce-sec` | Debounce zmiany stanu (sekundy) |
| `--donate-level` | Procent dev-fee |
| `--xmrig-http-port` | Port API XMRig |
| `--http-port` | Port Web UI |
| `--http-token` | Token Web UI / API |
| `--no-tray` | Wyłącza ikonę w trayu |

---

## 🧮 Specyfikacja wątków

### Tryb liczbowy
```
-t "1,2,4"
```

### Tryb procentowy
```
-t "25%,50%,100%"
```

> W plikach `.bat` znak `%` musi być zapisany jako `%%`.

---

## 🔁 Rotator portfeli (dev-fee)

- ZenMiner okresowo przełącza się pomiędzy **user wallet** i **dev wallet**
- Worker w okresie dev-fee jest **automatycznie generowany**:
  ```
  dev_<8hex>_<version>
  ```
- Zmiana walleta **nie wpływa na liczbę wątków**

---

## 📁 Logi
```
logs/zenminer.log
logs/xmrig_stdout.log
logs/xmrig_stderr.log
```

---

## ❗ Ważna informacja
Starsze wersje ZenMiner używały konfiguracji JSON.  
**Aktualna wersja działa wyłącznie w oparciu o CLI / BAT / Web UI.**

---

## 📜 Licencja
MIT
