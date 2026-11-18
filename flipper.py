#!/usr/bin/env python3
"""
Hypixel SkyBlock flip finder — Bazaar + Auction House (BIN).

Uses only Hypixel's public, key-free endpoints and the Python standard library.
It only *reads* market data and reports flips; it does not touch the game.

Usage:
    python3 flipper.py                       # one scan, prints + saves a report
    python3 flipper.py --budget 50m          # size bazaar flips to your purse
    python3 flipper.py --watch 60            # rescan the AH every 60s for fresh snipes
    DISCORD_WEBHOOK=https://... python3 flipper.py   # also post the report to Discord
"""
import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import gzip
import io
import json
import os
import re
import statistics
import struct
import sys
import time
import urllib.request

API = "https://api.hypixel.net/v2/skyblock"
UA = {"User-Agent": "skyblock-flipper/1.0"}


# ───────────────────────────── helpers ─────────────────────────────

def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def parse_coins(s):
    """'50m' -> 50_000_000, '2.5b' -> 2_500_000_000, '750k' -> 750_000."""
    s = str(s).strip().lower().replace(",", "")
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(s[-1:], 1)
    return float(s[:-1] if s[-1:] in "kmb" else s) * mult


def fmt(n):
    n = float(n)
    for unit, div in (("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{n:,.1f}"


def pretty(product_id):
    return product_id.replace("ENCHANTMENT_", "Book: ").replace("_", " ").title()


# ───────────────────── minimal NBT reader (for item_bytes) ─────────────────────

def _nbt_payload(buf, t):
    if t == 1:  return struct.unpack(">b", buf.read(1))[0]
    if t == 2:  return struct.unpack(">h", buf.read(2))[0]
    if t == 3:  return struct.unpack(">i", buf.read(4))[0]
    if t == 4:  return struct.unpack(">q", buf.read(8))[0]
    if t == 5:  return struct.unpack(">f", buf.read(4))[0]
    if t == 6:  return struct.unpack(">d", buf.read(8))[0]
    if t == 7:
        n = struct.unpack(">i", buf.read(4))[0]; return buf.read(n)
    if t == 8:
        n = struct.unpack(">H", buf.read(2))[0]; return buf.read(n).decode("utf-8", "replace")
    if t == 9:
        et = buf.read(1)[0]; n = struct.unpack(">i", buf.read(4))[0]
        return [_nbt_payload(buf, et) for _ in range(n)]
    if t == 10:
        out = {}
        while True:
            ct = buf.read(1)[0]
            if ct == 0:
                return out
            nl = struct.unpack(">H", buf.read(2))[0]
            name = buf.read(nl).decode("utf-8", "replace")
            out[name] = _nbt_payload(buf, ct)
    if t == 11:
        n = struct.unpack(">i", buf.read(4))[0]; return list(struct.unpack(f">{n}i", buf.read(4 * n)))
    if t == 12:
        n = struct.unpack(">i", buf.read(4))[0]; return list(struct.unpack(f">{n}q", buf.read(8 * n)))
    raise ValueError(f"bad NBT tag {t}")


def decode_item(item_bytes):
    raw = gzip.decompress(base64.b64decode(item_bytes))
    buf = io.BytesIO(raw)
    buf.read(1); buf.read(struct.unpack(">H", buf.read(2))[0])  # root tag type + name
    root = _nbt_payload(buf, 10)
    item = root["i"][0]
    return item.get("tag", {}).get("ExtraAttributes", {})


# ───────────────────────────── Bazaar ─────────────────────────────

def bazaar_flips(tax, budget, min_weekly_vol, share, top, max_roi):
    """
    Order-flip: place a buy order just above the highest buy order, then a sell
    offer just under the lowest sell offer.  In Hypixel's API:
      quick_status.sellPrice = highest BUY ORDER   (what you pay with an order)
      quick_status.buyPrice  = lowest SELL OFFER   (what you sell at with an offer)
    """
    data = get_json(f"{API}/bazaar")
    rows = []
    for pid, p in data["products"].items():
        q = p["quick_status"]
        buy_at = q["sellPrice"] + 0.1
        sell_at = q["buyPrice"] - 0.1
        if q["sellPrice"] <= 0 or q["buyPrice"] <= 0:
            continue
        weekly = min(q["buyMovingWeek"], q["sellMovingWeek"])
        if weekly < min_weekly_vol:
            continue
        margin = sell_at * (1 - tax) - buy_at
        if margin <= 0 or margin / buy_at > max_roi:
            continue
        hourly_vol = weekly / 168
        units_hr = min(hourly_vol * share, budget / buy_at)
        rows.append({
            "item": pretty(pid),
            "id": pid,
            "buy_order": buy_at,
            "sell_offer": sell_at,
            "margin": margin,
            "roi": margin / buy_at,
            "hourly_vol": hourly_vol,
            "units_hr": units_hr,
            "profit_hr": margin * units_hr,
        })
    rows.sort(key=lambda r: r["profit_hr"], reverse=True)
    return rows[:top]


# ─────────────────────────── Auction House ───────────────────────────

def fetch_all_auctions():
    first = get_json(f"{API}/auctions?page=0")
    pages = first["totalPages"]
    auctions = list(first["auctions"])
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for page in ex.map(lambda i: get_json(f"{API}/auctions?page={i}"), range(1, pages)):
            auctions.extend(page.get("auctions", []))
    return auctions, first["lastUpdated"]


LVL_RE = re.compile(r"\[Lvl (\d+)\]")

# How much of an applied upgrade's bazaar price a buyer actually pays for it.
APPLIED_WEIGHT = {"enchants": 0.85, "hpb": 1.0, "fpb": 0.6, "recomb": 0.8, "gems": 1.0}


def stars_of(ea):
    return ea.get("upgrade_level") or ea.get("dungeon_item_level") or 0


def gem_list(ea):
    """[(TYPE, QUALITY), ...] for every gem actually socketed."""
    gems = ea.get("gems") or {}
    out = []
    for slot, val in gems.items():
        if slot == "unlocked_slots" or slot.endswith("_gem"):
            continue
        quality = val.get("quality") if isinstance(val, dict) else val
        gtype = gems.get(slot + "_gem") or slot.rsplit("_", 1)[0]  # universal slots name their gem separately
        if quality:
            out.append((str(gtype).upper(), str(quality).upper()))
    return sorted(out)


def item_key(a, ea):
    """Exact-match key: listings only compare against the *same* item —
    same rarity, enchantments, reforge, stars, potato books and gemstones."""
    iid = ea.get("id")
    if not iid:
        return None

    if iid == "PET":
        try:
            info = json.loads(ea.get("petInfo", "{}"))
        except Exception:
            return None
        m = LVL_RE.search(a["item_name"])
        lvl = int(m.group(1)) if m else 1
        bucket = "LVL100+" if lvl >= 100 else ("LVL1" if lvl == 1 else "LVL_MID")
        if bucket == "LVL_MID":
            return None  # mid-level pets are priced too inconsistently to compare
        key = ["PET", info.get("type"), info.get("tier"), bucket]
        if info.get("heldItem"):
            key.append(info["heldItem"])
        if info.get("skin"):
            key.append("SKIN:" + info["skin"])
        return tuple(str(k) for k in key)

    if iid == "ENCHANTED_BOOK":
        ench = ea.get("enchantments", {})
        if len(ench) != 1:
            return None
        (name, lvl), = ench.items()
        return ("BOOK", str(name), str(lvl))

    key = [iid, "RARITY:" + a.get("tier", "")]
    if ea.get("rarity_upgrades"):
        key.append("RECOMB")
    key.append(f"STARS:{stars_of(ea)}")
    key.append("REFORGE:" + str(ea.get("modifier") or "none"))
    key.append(f"HPB:{ea.get('hot_potato_count') or 0}")
    ench = ea.get("enchantments") or {}
    key.append("ENCH:" + ",".join(f"{k}{v}" for k, v in sorted(ench.items())))
    key.append("GEMS:" + ",".join(f"{q}_{t}" for t, q in gem_list(ea)))
    if ea.get("attributes"):  # Kuudra attributes swing prices massively
        key.append("ATTR:" + ",".join(f"{k}{v}" for k, v in sorted(ea["attributes"].items())))
    if ea.get("skin"):
        key.append("SKIN:" + ea["skin"])
    return tuple(str(k) for k in key)


def bazaar_prices():
    """Instant-sell price (highest buy order) for every bazaar product."""
    prods = get_json(f"{API}/bazaar")["products"]
    return {pid: p["quick_status"]["sellPrice"] for pid, p in prods.items()}


def upgrade_value(ea, bz):
    """Rough coin value of what's been applied to an item, from bazaar prices."""
    w = APPLIED_WEIGHT
    total = 0.0
    for name, lvl in (ea.get("enchantments") or {}).items():
        total += bz.get(f"ENCHANTMENT_{name.upper()}_{lvl}", 0) * w["enchants"]
    potatoes = int(ea.get("hot_potato_count") or 0)
    total += min(potatoes, 10) * bz.get("HOT_POTATO_BOOK", 0) * w["hpb"]
    total += max(potatoes - 10, 0) * bz.get("FUMING_POTATO_BOOK", 0) * w["fpb"]
    if ea.get("rarity_upgrades"):
        total += bz.get("RECOMBOBULATOR_3000", 0) * w["recomb"]
    for gtype, quality in gem_list(ea):
        total += bz.get(f"{quality}_{gtype}_GEM", 0) * w["gems"]
    return total


def ah_fee(price):
    listing = 0.01 if price < 10e6 else (0.02 if price < 100e6 else 0.025)
    claim = 0.01 if price >= 1e6 else 0.0
    return listing + claim


def auction_flips(min_profit, min_roi, min_listings, max_price, top, max_roi, seen=None):
    """
    Lowest BIN vs. the next-lowest BIN of the exact same item, cross-checked
    against an estimated value:  estimated value = clean base price of the item
    (other listings' price minus their upgrades) + bazaar value of this item's
    enchants, potato books, recomb and gemstones.  The resell target is the
    lower of "next exact listing" and "estimated value", so an overpriced
    second listing can't make a fake flip.
    """
    auctions, updated = fetch_all_auctions()
    bz = bazaar_prices()

    groups, by_base = {}, {}
    for a in auctions:
        if not a.get("bin") or a.get("claimed"):
            continue
        try:
            ea = decode_item(a["item_bytes"])
        except Exception:
            continue
        k = item_key(a, ea)
        if not k:
            continue
        a["_ea"] = ea
        groups.setdefault(k, []).append(a)
        if k[0] not in ("PET", "BOOK"):
            a["_upg"] = upgrade_value(ea, bz)
            clean = max(a["starting_bid"] - a["_upg"], 0)
            by_base.setdefault((ea["id"], None), []).append((clean, a["uuid"]))
            by_base.setdefault((ea["id"], stars_of(ea)), []).append((clean, a["uuid"]))

    def estimate(k, a):
        if k[0] == "BOOK":
            return bz.get(f"ENCHANTMENT_{k[1].upper()}_{k[2]}") or None
        if k[0] == "PET":
            return None
        ea = a["_ea"]
        for bucket in ((ea["id"], stars_of(ea)), (ea["id"], None)):
            others = sorted(c for c, u in by_base.get(bucket, []) if u != a["uuid"])
            if others:
                return statistics.median(others[:3]) + a["_upg"]
        return None

    rows = []
    for k, listings in groups.items():
        if len(listings) < max(min_listings, 2):
            continue
        listings.sort(key=lambda a: a["starting_bid"])
        low, nxt = listings[0], listings[1]
        cost = low["starting_bid"]
        if cost > max_price:
            continue
        next_bin = nxt["starting_bid"] - 1
        est = estimate(k, low)
        resell = min(next_bin, est) if est else next_bin
        profit = resell * (1 - ah_fee(resell)) - cost
        roi = profit / cost if cost else 0
        if profit < min_profit or roi < min_roi or roi > max_roi:
            continue
        if seen is not None and low["uuid"] in seen:
            continue
        rows.append({
            "item": low["item_name"],
            "tier": low.get("tier", ""),
            "key": " ".join(k),
            "cost": cost,
            "next_bin": next_bin,
            "est_value": est,
            "resell": resell,
            "profit": profit,
            "roi": roi,
            "listings": len(listings),
            "uuid": low["uuid"],
            "cmd": f"/viewauction {low['uuid']}",
        })
    rows.sort(key=lambda r: r["profit"], reverse=True)
    return rows[:top], updated, len(auctions)


# ───────────────────────────── output ─────────────────────────────

def bazaar_table(rows):
    out = ["| # | Item | Buy order | Sell offer | Margin | ROI | Vol/hr | Est. profit/hr |",
           "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {r['item']} | {fmt(r['buy_order'])} | {fmt(r['sell_offer'])} | "
                   f"{fmt(r['margin'])} | {r['roi']:.1%} | {fmt(r['hourly_vol'])} | {fmt(r['profit_hr'])} |")
    return "\n".join(out)


def ah_table(rows):
    out = ["| # | Item | Lowest BIN | Next exact BIN | Est. value | Sell at | Profit (after tax) | ROI | Matches | Command |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        est = fmt(r["est_value"]) if r["est_value"] else "—"
        out.append(f"| {i} | {r['item']} ({r['tier']}) | {fmt(r['cost'])} | {fmt(r['next_bin'])} | {est} | "
                   f"{fmt(r['resell'])} | {fmt(r['profit'])} | {r['roi']:.0%} | {r['listings']} | `{r['cmd']}` |")
    return "\n".join(out)


def post_discord(webhook, text):
    # Discord messages max out at 2000 chars; send in chunks.
    for i in range(0, len(text), 1900):
        body = json.dumps({"content": text[i:i + 1900]}).encode()
        req = urllib.request.Request(webhook, data=body,
                                     headers={**UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        time.sleep(0.6)


# ───────────────────────────── main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Find Hypixel SkyBlock bazaar + AH flips.")
    ap.add_argument("--budget", default="100m", help="your purse for bazaar sizing (e.g. 20m, 1.5b)")
    ap.add_argument("--tax", type=float, default=1.25,
                    help="bazaar sell tax %% (1.25 default; 1.125/1.0 with Bazaar Flipper)")
    ap.add_argument("--share", type=float, default=0.10,
                    help="fraction of an item's hourly volume you can realistically capture")
    ap.add_argument("--min-weekly-volume", type=float, default=50_000)
    ap.add_argument("--min-profit", default="500k", help="min AH profit per flip")
    ap.add_argument("--min-roi", type=float, default=0.05, help="min AH ROI (0.05 = 5%%)")
    ap.add_argument("--max-roi", type=float, default=0.75,
                    help="drop flips above this ROI (usually mispriced/misgrouped items, not real flips)")
    ap.add_argument("--min-listings", type=int, default=2,
                    help="min BIN listings of the exact same item (incl. the one you buy; at least 2)")
    ap.add_argument("--max-price", default=None, help="max AH buy price (defaults to budget)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-bazaar", action="store_true")
    ap.add_argument("--no-ah", action="store_true")
    ap.add_argument("--watch", type=int, default=0, metavar="SECS",
                    help="keep rescanning the AH every SECS seconds and print only new snipes")
    ap.add_argument("--daily-after", type=int, default=None, metavar="HOUR",
                    help="only run if it's past HOUR today and today's report doesn't exist yet "
                         "(lets the scheduler call this often without duplicate reports)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports"))
    args = ap.parse_args()

    budget = parse_coins(args.budget)
    max_price = parse_coins(args.max_price) if args.max_price else budget
    min_profit = parse_coins(args.min_profit)

    if args.watch:
        seen = set()
        print(f"Watching the AH every {args.watch}s (Ctrl+C to stop)…")
        while True:
            try:
                rows, _, n = auction_flips(min_profit, args.min_roi, args.min_listings,
                                           max_price, args.top, args.max_roi, seen)
                stamp = dt.datetime.now().strftime("%H:%M:%S")
                for r in rows:
                    seen.add(r["uuid"])
                    print(f"[{stamp}] {r['item']:<40} buy {fmt(r['cost']):>8} → {fmt(r['resell']):>8}"
                          f"  +{fmt(r['profit'])} ({r['roi']:.0%})  {r['cmd']}")
                if not rows:
                    print(f"[{stamp}] no new snipes ({n:,} auctions scanned)")
            except Exception as e:
                print("scan failed:", e, file=sys.stderr)
            time.sleep(args.watch)

    now = dt.datetime.now()
    if args.daily_after is not None:
        todays = os.path.join(args.out, f"flips-{now:%Y-%m-%d}.md")
        if now.hour < args.daily_after or os.path.exists(todays):
            return  # too early, or already done today
        print(f"--- daily run {now:%Y-%m-%d %H:%M} ---", file=sys.stderr)
    sections = [f"# SkyBlock flips — {now:%Y-%m-%d %H:%M}",
                f"Budget {fmt(budget)} · bazaar tax {args.tax}%"]

    if not args.no_bazaar:
        print("Scanning bazaar…", file=sys.stderr)
        bz = bazaar_flips(args.tax / 100, budget, args.min_weekly_volume, args.share, args.top, args.max_roi)
        sections += ["", "## Bazaar order flips",
                     "_Buy order just above top buy order, sell offer just under top sell offer. "
                     f"Profit/hr assumes you fill {args.share:.0%} of hourly volume, capped by budget._",
                     "", bazaar_table(bz)]

    if not args.no_ah:
        print("Scanning auction house (this takes ~10–30s)…", file=sys.stderr)
        ah, updated, n = auction_flips(min_profit, args.min_roi, args.min_listings, max_price, args.top, args.max_roi)
        age = int(time.time() - updated / 1000)
        sections += ["", "## Auction House BIN flips",
                     f"_{n:,} auctions scanned, data {age}s old. Lowest BIN vs next-lowest BIN of the exact "
                     "same item (rarity, enchants, reforge, stars, potato books, gemstones), sold at "
                     "the lower of that or the estimated value. Snipes disappear fast — "
                     "use `--watch 60` for live ones._",
                     "", ah_table(ah)]

    report = "\n".join(sections) + "\n"
    print(report)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"flips-{now:%Y-%m-%d}.md")
    with open(path, "w") as f:
        f.write(report)
    print(f"Saved {path}", file=sys.stderr)

    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        post_discord(hook, report)
        print("Posted to Discord.", file=sys.stderr)


if __name__ == "__main__":
    main()
