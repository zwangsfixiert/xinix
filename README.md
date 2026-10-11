# Deep Trades View für Hyperliquid Perpetuals

Nachbau des "Deep Trades"-Features (DeepCharts) für Hyperliquid: Einzelne Fills
eines Takers werden zu **Deep Trades** zusammengefasst und als Bubbles auf den
Kerzen dargestellt, mit persistenter Historie in SQLite und Live-Stream.

Nur lesender Zugriff auf öffentliche Hyperliquid-Endpunkte. Keine API-Keys, keine Orders.

```
Hyperliquid WS (trades) ──► collector ──► SQLite (WAL) ──► API (FastAPI) ──► Browser
Hyperliquid /info (candleSnapshot) ─────────────────────────┘   /candles /deeptrades /stream
```

## Komponenten

| Pfad | Inhalt |
|---|---|
| `deeptrades/collector.py` | WebSocket-Collector: abonniert `trades`, schreibt jeden Fill nach SQLite, Reconnect mit Backoff, Ping alle 30 s, Retention |
| `deeptrades/aggregate.py` | Aggregation Fills → Deep Trades, Absorption-Logik |
| `deeptrades/api.py` | FastAPI: `/candles`, `/deeptrades`, WebSocket `/stream`, liefert das Frontend unter `/` aus |
| `deeptrades/db.py` | SQLite-Schema, Insert mit Dedupe, Abfragen, Retention |
| `web/deep-trades-hyperliquid.html` | Single-File-Frontend (Canvas), Live- und Demo-Modus |
| `config.toml` | Konfiguration |
| `deploy/systemd/*.service` | systemd-Units für Collector und API |
| `deploy/nginx/deeptrades.conf` | Nginx-Reverse-Proxy mit TLS und Basic Auth |
| `scripts/smoke_check.py` | Prüft die echte Hyperliquid-API gegen die erwartete Feldstruktur |
| `tests/` | pytest-Tests |

## Datenmodell

Hyperliquid liefert Trades (WebSocket `trades` und `recentTrades`) laut Doku als

```json
{"coin":"BTC","side":"B","px":"62000.0","sz":"0.01","hash":"0x…","time":1718000000000,"tid":123,"users":["0xBUYER","0xSELLER"]}
```

Es gibt **keine** separaten Felder `buyer`/`seller`: `users` ist `[buyer, seller]`.
Der Collector speichert daraus die Spalten `coin, time, px, sz, side, buyer, seller, hash, tid`
(Adressen lowercase). Index auf `(coin, time)`, Dedupe über `UNIQUE(coin, tid)`.

## Aggregation

* **Taker:** bei `side == "B"` der Buyer, sonst der Seller. Maker ist die Gegenseite.
* **Gruppierung:** gleiche Coin, gleicher Taker, gleiche Seite, Zeitstempel innerhalb von
  `window_ms` (Default 500 ms) **ab dem ersten Fill** der Gruppe (`t - t_first <= window_ms`,
  Grenze inklusive). Das Fenster ist am ersten Fill verankert, damit ein durchgehend
  handelnder Taker nicht zu einer endlosen Kette verschmilzt. Fills anderer Taker
  dazwischen unterbrechen eine Gruppe nicht.
* **Pro Deep Trade:** `vwap`, `notional` (Σ px·sz), `sz`, `fills`, `makers` (unterschiedliche
  Maker-Adressen), `px_min`/`px_max` (Preis-Range), `px_first`/`px_last`, `taker`,
  `start_time`/`end_time`, `id` (`<coin>-<erste tid>`).
* **Absorption** (optional, `[aggregation]`): Deep Trade mit `notional >=
  absorption_min_notional` (pro Coin überschreibbar). Gemessen wird der letzte Trade-Preis
  `absorption_horizon_s` Sekunden nach Ende des Deep Trades. Absorption = Preis liegt
  nicht jenseits des Extrems in Trade-Richtung (Kauf: `≤ px_max·(1+tol)`, Verkauf:
  `≥ px_min·(1−tol)`, `tol = absorption_tolerance_bps`). Gab es danach keinen Trade, gilt
  der Preis als unverändert. `absorption` ist `true`/`false` oder `null`, solange der
  Horizont noch nicht erreicht ist.

## API

| Endpoint | Beschreibung |
|---|---|
| `GET /candles?coin=BTC&interval=1m&start=<ms>&end=<ms>` | Proxy auf `candleSnapshot`; Antwort unverändert durchgereicht (`t,T,s,i,o,c,h,l,v,n`). `end` optional (jetzt). |
| `GET /deeptrades?coin=BTC&start=<ms>&end=<ms>&min_notional=50000[&taker=0x…]` | Deep Trades mit `start_time` im Bereich. Bereich max. `max_range_hours`. |
| `WS /stream?coin=BTC` | Nachrichten: `hello`, `fills` (`[[time,px,sz,side],…]` für die Live-Kerze), `deeptrade` (Objekt wie oben; wird mit derselben `id` erneut gesendet, wenn die Absorption entschieden ist), `ping`. |
| `GET /config` | Coins, Intervalle, Fenster, Absorption-Schwellen |
| `GET /health` | Alter des letzten gespeicherten Fills |

Die API liest die SQLite-Datei des Collectors (read-only) und berechnet die Deep Trades
bei jeder Anfrage aus den Roh-Fills. Dadurch wirkt eine Änderung von `window_ms` sofort
auch auf die Historie. Der Live-Stream liest neue Zeilen per `rowid` (Polling alle 250 ms)
und schließt ein Fenster 1,5 s nach seinem Ende, damit verspätete Fills noch dazukommen.

## Frontend

`web/deep-trades-hyperliquid.html`, wird von der API unter `/` ausgeliefert.

* Lädt beim Start `/config`, die Kerzen (300 Stück) und die Deep-Trade-Historie, danach live über `/stream`.
* Bubbles: grün = Taker-Kauf, rot = Taker-Verkauf, Fläche ∝ Notional, Position = Startzeit/VWAP,
  vertikale Linie = Preis-Range. **Absorption:** gestrichelter gelber Ring + „A“, in der Liste `◎ ABS`.
* Filter: Min. Notional, **Taker-Adresse** (Teilstring; Klick auf eine Bubble setzt den Filter), „nur Absorption“.
* Bedienung: Ziehen = scrollen, Mausrad = zoomen, Doppelklick = zurück zum aktuellen Stand.
* **Demo-Modus:** automatisch, wenn die API nicht erreichbar ist, oder per Button bzw. `?demo=1`.
* `?api=https://host` verwendet eine andere API-Basis.

## Setup (lokal)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/smoke_check.py BTC     # echte API-Struktur prüfen
.venv/bin/python -m deeptrades.collector         # Terminal 1
.venv/bin/python -m deeptrades.api               # Terminal 2 → http://127.0.0.1:8000/
.venv/bin/pytest -q
```

Die Config wird aus `--config`, `$DEEPTRADES_CONFIG` oder `./config.toml` gelesen.

## Deployment (systemd + Nginx + Basic Auth)

```bash
sudo useradd --system --home /opt/deeptrades --shell /usr/sbin/nologin deeptrades
sudo mkdir -p /opt/deeptrades/data
sudo cp -r deeptrades web config.toml requirements.txt /opt/deeptrades/
sudo python3.11 -m venv /opt/deeptrades/.venv
sudo /opt/deeptrades/.venv/bin/pip install -r /opt/deeptrades/requirements.txt
sudo chown -R deeptrades:deeptrades /opt/deeptrades
# in /opt/deeptrades/config.toml: db_path = "/opt/deeptrades/data/deeptrades.sqlite"

sudo cp deploy/systemd/deeptrades-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deeptrades-collector deeptrades-api
journalctl -u deeptrades-collector -f
```

Nginx:

```bash
sudo apt install nginx apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-deeptrades <benutzer>
sudo cp deploy/nginx/deeptrades.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/deeptrades.conf /etc/nginx/sites-enabled/
# server_name und Zertifikatspfade anpassen (z. B. certbot --nginx -d <domain>)
sudo nginx -t && sudo systemctl reload nginx
```

Die API lauscht nur auf `127.0.0.1:8000`; erreichbar ist sie ausschließlich über Nginx.
Der `/stream`-Block setzt die Upgrade-Header für WebSockets. Browser senden die
Basic-Auth-Zugangsdaten beim WebSocket-Handshake auf derselben Origin mit.

**Speicherbedarf:** pro Fill ca. 150–200 Byte inkl. Index. Bei 30 Tagen Retention und
mehreren Hunderttausend Fills pro Tag für BTC sind einige GB realistisch; `retention_days` anpassen.

## Tests

```bash
.venv/bin/pytest -q
```

* `tests/test_aggregate.py`: Fenstergrenzen (inklusiv, +1 ms, verankertes Fenster), Taker-Zuordnung, VWAP/Notional/Range/Maker, Absorption, Parsing.
* `tests/test_db.py`: WAL, Dedupe über tid, Retention.
* `tests/test_integration.py`: Collector gegen einen lokalen WS-Server im dokumentierten Hyperliquid-Format (Subscribe, Ping, Dedupe); API `/deeptrades` und `/stream` auf einer Test-DB.

Die Integrationstests ersetzen keinen Test gegen die echte API. Dafür ist `scripts/smoke_check.py` da.
