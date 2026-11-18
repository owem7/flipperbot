Run bot for daily views on the best auction and bazaar flips

## Run locally

```
python3 server.py        # http://localhost:8765
python3 flipper.py       # one scan, prints + saves a report to reports/ (on this Mac only)
```

Only the Python standard library is used (Python 3.9+).

## Deploy to Render

1. In Render, choose **New → Blueprint** and pick this repo. `render.yaml` sets everything up.
2. Render runs `python server.py`; the server picks up Render's `PORT` and listens on `0.0.0.0`.
3. Health check: `/healthz`.

Notes:
- Scans are cached for 60s (`SCAN_CACHE_SECS`) so many visitors don't each re-download the Auction House.
- Daily reports are only kept on the Mac (`reports/`, written by the scheduled scan). They aren't in the repo or on the website.
- On the free plan the service sleeps when idle, so the first visit after a while takes ~30–60s.

## How Auction House flips are found

Each listing is only compared with listings of the **exact same item**: same rarity (incl. recomb),
enchantments, reforge, stars, potato books, gemstones (plus attributes/skins). Pets match on type,
rarity, level 1 / 100, held item and skin.

The item's **estimated value** = clean base price of that item (other listings' prices minus their
upgrades) + bazaar value of its enchants, potato books, recomb and gems. A flip sells at the lower of
the next exact-match BIN and the estimated value. Pets have no estimate and use the next BIN alone.
