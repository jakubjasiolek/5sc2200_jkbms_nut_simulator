# bmsups — wirtualny UPS NUT dla Eaton 5SC z baterią LFP (JK BMS)

Eaton 5SC liczy stan naładowania modelem ołowiowym. Z pakietem LFP 16S 50 Ah pokazuje
0% i `LB` po kilku minutach na baterii, chociaż realnie zostają godziny pracy (log z 24.10:
Eaton zgłosił LB po 6 min, a bateria działała jeszcze ~2 h przy 970 W).

`bmsups` to jeden kontener, który:

* czyta Eatona przez USB (prawdziwy `usbhid-ups`, w NUT jako **`eaton-raw`**),
* czyta JK BMS przez BLE (albo kabel serial) przez `jktool`,
* wystawia **skorygowanego UPS-a `ups`** przez `upsd` na porcie 3493:
  * status Eatona (OL/OB/CHRG…) **bez jego fałszywego `LB`**,
  * `battery.charge` = SoC z BMS,
  * `battery.runtime` liczony z energii w baterii i mocy obciążenia,
  * własne `LB`, gdy na baterii przez 30 s spełniony jest **którykolwiek** warunek:
    SoC ≤ 20%, czas pracy ≤ 15 min, najsłabsze ogniwo ≤ 3.00 V, BMS wyłączył MOS rozładowania,
    brak danych z BMS > 10 min,
* przy `LB` od razu wysyła do Eatona **`shutdown.return`** z `ups.delay.shutdown=180`.
  Eaton odcina wyjście po 180 s (Unraid zdąży się zamknąć) i włącza je, gdy wróci prąd,
  więc serwer startuje sam,
* udostępnia dashboard BMS/UPS na **:8080** (historia 30 dni w `/config/bms_log.csv`).

```
usbhid-ups [eaton-raw] ─┐
                         ├─ upsd :3493 ── Unraid (plugin NUT, netclient) / inne serwery / Home Assistant
dummy-ups  [ups] ◄─ /run/nut/ups.dev ◄─ bridge.py ◄─ jktool (BLE / serial) ◄─ JK BMS
                                           └─ dashboard :8080
```

Gdy dane z BMS są nieświeże (> 60 s), SoC jest dalej liczony z mocy pobieranej przez UPS
(stosunek DC/AC uczy się sam w czasie pracy na baterii; z logów wynosi ~1.18).

## Instalacja na Unraid

### 1. Sprawdzenie (terminal Unraid)
```bash
ls /sys/class/bluetooth          # musi pokazać hci0 (po przeniesieniu adaptera z VM)
lsmod | grep -E 'btusb|bluetooth'
lsusb | grep -iE '0463|8087'     # UPS i adapter BT widoczne na hoście
pgrep -a bluetoothd              # powinno być puste (bluetoothd działa w kontenerze)
```
Jeśli `/sys/class/bluetooth` jest puste, jądro Unraid nie obsługuje BT. Wtedy użyj kabla
UART/RS485 do BMS i ustaw `BMS_TARGET=serial:/dev/ttyUSB0,115200` (port i prędkość zależą
od modelu BMS i adaptera).

### 2. Przeniesienie USB z VM na Unraid
W ustawieniach VM odznacz *MGE UPS Systems UPS* (0463:ffff) i *Intel AX211 Bluetooth*
(8087:0033), a następnie zrestartuj VM. Wyłącz wbudowany apcupsd w Unraid
(*Settings → UPS Settings → Start APC UPS daemon: No*).

### 3. Obraz (GitHub + GHCR, raz)
1. Utwórz **publiczne** repo `bmsups` na GitHubie i wypchnij ten katalog (branch `main`).
2. Actions (`.github/workflows/build.yml`) uruchomi testy i zbuduje `ghcr.io/<user>/bmsups:latest`.
3. GitHub → Packages → `bmsups` → *Package settings* → *Change visibility: Public*.

### 4. Kontener
1. W `unraid/bmsups.xml` zamień `GITHUB_USER` na swój login i skopiuj plik na Unraid:
   `/boot/config/plugins/dockerMan/templates-user/my-bmsups.xml`.
2. *Docker → Add Container → Template: bmsups* → sprawdź `BMS link` (MAC BMS) → *Apply*.
3. Przenieś historię: `~/bms_log.csv` z VM → `/mnt/user/appdata/bmsups/bms_log.csv`
   (zatrzymaj wcześniej kontener; nowe kolumny są dopisywane automatycznie).
4. Dashboard: `http://<unraid>:8080`. Hasła NUT (jeśli ich nie ustawiłeś) są w
   `/mnt/user/appdata/bmsups/secrets.env`.

### 5. Plugin NUT w Unraid
Zainstaluj *NUT for UNRAID* (Community Applications) i ustaw:
* tryb **netclient** (slave), host `127.0.0.1`, port `3493`, UPS **`ups`**,
  użytkownik `monuser` + hasło z `secrets.env`,
* shutdown: **na sygnał Low Battery** (bateria/czas = „wg UPS-a”). Progi ustawiasz w kontenerze
  (`LB_SOC`, `LB_RUNTIME_S`), nie w pluginie. Nie ustawiaj shutdownu „po X sekundach na baterii”,
  bo Unraid wyłączy się niepotrzebnie wcześnie,
* *killpower* w pluginie **wyłączony**, bo robi to kontener.

Po zmianach widżet UPS w Unraid pokazuje SoC z BMS i realny czas pracy.

### 6. Inne urządzenia
Dowolny klient NUT: `ups@<ip-unraid>:3493`, użytkownik `monuser`. Przykład dla `upsmon.conf`:
```
MONITOR ups@192.168.1.10 1 monuser <hasło> secondary
```
Surowe dane Eatona (do diagnostyki, **nie do shutdownu**): `eaton-raw@<ip-unraid>`.

### 7. Wyłączenie starej wersji na VM
Gdy wszystko działa na Unraid: `sudo systemctl disable --now bmsdash nut-driver@eaton nut-server`.

## Zmienne
| zmienna | domyślnie | opis |
|---|---|---|
| `BMS_TARGET` | `bt:hci0/dev_28_D4_1E_90_02_0F,ffe1` | transport jktool; `none` = bez BMS |
| `LB_SOC` / `LB_RUNTIME_S` / `LB_CELL_V` | 20 / 900 / 3.0 | progi LB |
| `LB_HOLD_S` | 30 | jak długo warunek musi trwać |
| `BMS_STALE_LB_S` | 600 | brak BMS na baterii → LB |
| `KILLPOWER` / `KILLPOWER_DELAY_S` | 1 / 180 | `shutdown.return` przy LB, wyjście off po N s |
| `CAPACITY_AH` / `V_NOM` / `RESERVE_SOC` | 50 / 51.2 / 5 | parametry pakietu |
| `NUT_MONITOR_USER` / `NUT_MONITOR_PASSWORD` | monuser / (gen.) | klienci NUT |
| `NUT_ADMIN_USER` / `NUT_ADMIN_PASSWORD` | admin / (gen.) | upscmd/upsrw |
| `UPS_NAME` / `UPS_RAW_NAME` | ups / eaton-raw | nazwy w NUT |
| `NUT_PORT` / `HTTP_PORT` | 3493 / 8080 | porty (sieć host) |

## Test po wdrożeniu
1. `upsc ups@localhost` w Unraid pokazuje `battery.type: LiFePO4` i `battery.charge` = SoC z BMS.
2. **Wyciągnij wtyczkę na 5 min**: `ups` ma pokazać `OB DISCHRG` **bez LB**, a Unraid nie może
   się wyłączyć. Po podłączeniu: `OL CHRG`.
3. **Test LB/killpower** (przerwa w pracy serwera!): ustaw `LB_SOC=99`, odłącz prąd, po ~30 s
   pojawi się `LB` i Unraid zacznie się zamykać, a w logu kontenera będzie `KILLPOWER … shutdown.return`.
   Po 180 s Eaton odcina wyjście. Podłącz prąd: Eaton włącza wyjście, serwer startuje
   (BIOS: *Restore on AC power loss = Power On*). Potem przywróć `LB_SOC=20`.

## Rozwój lokalny
```bash
python3 -m unittest discover -s tests -v      # testy + replay zdarzenia z ~/bms_log.csv
cd app && UPS_RAW=eaton@localhost BMS_TARGET=none CONFIG_DIR=/tmp/bmsups \
  VIRTUAL_DEV_PATH=/tmp/bmsups/ups.dev KILLPOWER=0 HTTP_PORT=18080 python3 bridge.py
```
BMS przyjmuje tylko **jedno** połączenie BLE naraz. Na VM zatrzymaj `bmsdash.service`,
zanim uruchomisz bridge z prawdziwym `BMS_TARGET`. Na Unraid aplikacja JK na telefonie
nie połączy się, dopóki działa kontener.

## Pliki
* `app/estimator.py`: logika SoC/runtime/LB/killpower (bez I/O, testowalna)
* `app/bridge.py`: wątki UPS/BMS, plik `.dev`, CSV, HTTP
* `app/bmsdash.html`: dashboard
* `rootfs/`: entrypoint, supervisord, uruchamianie sterowników NUT
* `unraid/bmsups.xml`: szablon Unraid
