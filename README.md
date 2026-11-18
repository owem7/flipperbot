Run bot for daily views on the best auction and bazaar flips

## Run locally

```
python3 server.py        # http://localhost:8765
python3 flipper.py       # one scan, prints + saves a report to reports/
```

Only the Python standard library is used (Python 3.9+).

## Deploy to Render

1. In Render, choose **New → Blueprint** and pick this repo. `render.yaml` sets everything up.
2. Render runs `python server.py`; the server picks up Render's `PORT` and listens on `0.0.0.0`.
3. Health check: `/healthz`.

Notes:
- Scans are cached for 60s (`SCAN_CACHE_SECS`) so many visitors don't each re-download the Auction House.
- Render's disk is temporary: the Reports tab shows the reports committed to `reports/`; new ones written on the server are lost on redeploy.
- On the free plan the service sleeps when idle, so the first visit after a while takes ~30–60s.
