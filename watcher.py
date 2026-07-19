#!/usr/bin/env python3
"""
Cineplex IMAX 70mm seat watcher — The Odyssey @ Cinéma Banque Scotia Montréal.

Watches qualifying showtimes (Sat/Sun any time; Mon–Fri >= 5 PM) over the next
14 days and alerts ONLY when something changes in the seats you care about:
a run of >= MIN_ADJACENT adjacent Standard seats in rows F–K. Alerts include a
rendered image of the actual seat map (Telegram) with the prime seats
highlighted. No alert = nothing new. State lives in state.json.

Optional: set ALERT_ANY_INCREASE = True to also get plain-text alerts whenever
any qualifying showtime's total availability increases (noisier).

Deps: Pillow (for the map image; everything else is stdlib). Without Pillow
it degrades gracefully to text-only alerts.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────
LOCATION_ID = 9406                       # Cinéma Banque Scotia Montréal
MOVIE_PATTERN = re.compile(r"odyssey", re.I)
REQUIRED_EXPERIENCES = {"imax", "70mm"}  # session must carry BOTH tags
DAYS_AHEAD = 28
WEEKDAY_EARLIEST_HOUR = 17               # 5 PM cutoff Mon–Fri
THEATRE_TZ = ZoneInfo("America/Toronto")

TARGET_ROWS = ("F", "G", "H", "I", "J", "K")  # the rows you want
MIN_ADJACENT = 2                         # alert on >= this many seats together
IDEAL_ADJACENT = 3                       # runs this size or bigger rank equal-best
RUN_MAX_OFF = 10                         # runs must reach within this many columns
                                         # of row center (sides are bad seats)
BORDERLINE_OFF = 8                       # finds beyond this get tagged [borderline]
ROW_PREF = "HIGJFK"                      # row tie-break for "best seat" (middle rows first)
BEST_SEAT_HOURS = (11, 19)               # best-seat pick considers shows 11 AM–7 PM
MIN_PARTY = 2                            # min seats needed, even if separated
GOOD_SEAT_MAX_OFF = 10                   # a "good" seat sits within this many
                                         # columns of dead center (for separated seats)
ALERT_ANY_INCREASE = False               # True = also alert on any new seats anywhere
MIN_LEAD_MINUTES = 30                    # ignore shows already started or starting
                                         # sooner than this (no useless alerts)

STATE_FILE = Path(__file__).parent / "state.json"

# Public API key embedded in the cineplex.com frontend (sent by every
# visitor's browser). Overridable via env if it ever rotates.
API_KEY = os.environ.get("CINEPLEX_API_KEY", "dcdac5601d864addbc2675a2e96cb1f8")

SHOWTIMES_URL = ("https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes"
                 "?language=en&locationId={loc}&date={date}")
SEATS_URL = ("https://apis.cineplex.com/prod/ticketing/api/v1/theatre/{loc}"
             "/showtime/{showtime}/seat-availability")
LAYOUT_URL = ("https://apis.cineplex.com/prod/ticketing/api/v1/theatre/{loc}"
              "/showtime/{showtime}/seat-layout")
BOOKING_LINK = "https://www.cineplex.com/theatre/cinema-banque-scotia-montreal?openTM=true"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def get_json(url: str, with_key: bool = False, retries: int = 3):
    import gzip
    import zlib
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Accept-Encoding": "gzip, deflate",
               "Accept-Language": "en", "Referer": "https://www.cineplex.com/"}
    if with_key:
        headers["Ocp-Apim-Subscription-Key"] = API_KEY
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.status == 204 or not raw:   # no data published for date
                    return None
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return json.loads(raw.decode())
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url.split('?')[0]} ({last_err})")


# ── Showtime discovery ────────────────────────────────────────────────────
def qualifying_showtimes():
    today = datetime.now(THEATRE_TZ).date()
    for offset in range(DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        date_str = f"{day.month}/{day.day}/{day.year}"     # API wants M/D/YYYY
        try:
            data = get_json(SHOWTIMES_URL.format(
                loc=LOCATION_ID, date=urllib.parse.quote(date_str, safe="")),
                with_key=True)
        except RuntimeError as e:
            print(f"  ! {day}: {e}", file=sys.stderr)
            continue
        if not data:                                 # date not published yet
            continue
        for location in data if isinstance(data, list) else [data]:
            for d in location.get("dates", []):
                for movie in d.get("movies", []):
                    if not MOVIE_PATTERN.search(movie.get("name", "")):
                        continue
                    for exp in movie.get("experiences", []):
                        tags = {t.lower() for t in exp.get("experienceTypes", [])}
                        if not REQUIRED_EXPERIENCES <= tags:
                            continue
                        for sess in exp.get("sessions", []) or exp.get("showtimes", []):
                            start = datetime.fromisoformat(sess["showStartDateTime"])
                            is_weekend = start.weekday() >= 5
                            if not is_weekend and start.hour < WEEKDAY_EARLIEST_HOUR:
                                continue
                            # skip shows already started / starting too soon
                            now_local = datetime.now(THEATRE_TZ).replace(tzinfo=None)
                            if (start - now_local).total_seconds() < MIN_LEAD_MINUTES * 60:
                                continue
                            m = re.search(r"showtimeId=(\d+)",
                                          sess.get("seatMapUrl", ""), re.I)
                            if not m:
                                continue
                            yield {
                                "key": start.strftime("%Y-%m-%d %H:%M"),
                                "label": start.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
                                "showtime_id": m.group(1),
                                # deep link to this showtime's live seat map with
                                # its Buy Tickets button (works without a session)
                                "book_url": (f"https://www.cineplex.com/ticketing/preview"
                                             f"?locationId={LOCATION_ID}"
                                             f"&showtimeId={m.group(1)}&dbox=false"),
                            }
        time.sleep(0.25)  # be polite


# ── Seat map + prime-run detection ────────────────────────────────────────
def seat_map(showtime_id: str):
    """Return (rows, total_cols). rows = [{label, seats:[{col,label,type,open}]}]"""
    layout = get_json(LAYOUT_URL.format(loc=LOCATION_ID, showtime=showtime_id))
    avail = get_json(SEATS_URL.format(loc=LOCATION_ID, showtime=showtime_id))
    open_ids = {k for k, v in (avail.get("seatAvailabilities") or {}).items()
                if v == "Available"}
    rows = []
    std = layout.get("standardSeats") or {}
    for row in std.get("rows", []):
        if not row.get("label"):                     # spacer rows
            continue
        seats = sorted(
            [{"col": s["column"], "label": s["label"], "type": s.get("type", ""),
              "open": s["id"] in open_ids} for s in row.get("seats", [])],
            key=lambda x: x["col"])
        rows.append({"label": row["label"], "seats": seats})
    return rows, std.get("columnCount") or layout.get("totalColumns", 35)


def prime_runs(rows, total_cols):
    """Adjacent open Standard seats in TARGET_ROWS, best (biggest, most central) first."""
    center = (total_cols - 1) / 2
    found = []
    for row in rows:
        if row["label"].upper() not in TARGET_ROWS:
            continue
        run = []
        for s in row["seats"] + [{"open": False, "col": -99, "type": ""}]:  # sentinel flush
            if s["open"] and s["type"] == "Standard" and (not run or s["col"] == run[-1]["col"] + 1):
                run.append(s)
            else:
                if len(run) >= MIN_ADJACENT:
                    # nearest edge of the run to center (sides are bad seats)
                    near = min(abs(run[0]["col"] - center),
                               abs(run[-1]["col"] - center))
                    if near <= RUN_MAX_OFF:
                        mid = (run[0]["col"] + run[-1]["col"]) / 2
                        found.append({"row": row["label"],
                                      "labels": [x["label"] for x in run],
                                      "size": len(run), "near": near,
                                      "center_off": round(abs(mid - center), 1)})
                run = [s] if (s["open"] and s["type"] == "Standard") else []
    found.sort(key=lambda r: (-min(r["size"], IDEAL_ADJACENT), r["center_off"]))
    return found


def total_open(rows) -> int:
    return sum(1 for r in rows for s in r["seats"] if s["open"])


def good_scattered(rows, total_cols):
    """Open Standard seats in TARGET_ROWS close enough to center to count as
    'good' even when separated. Sorted most-central first."""
    center = (total_cols - 1) / 2
    seats = []
    for row in rows:
        if row["label"].upper() not in TARGET_ROWS:
            continue
        for s in row["seats"]:
            if s["open"] and s["type"] == "Standard":
                off = abs(s["col"] - center)
                if off <= GOOD_SEAT_MAX_OFF:
                    seats.append({"row": row["label"], "label": s["label"],
                                  "off": round(off, 1)})
    seats.sort(key=lambda x: x["off"])
    return seats


# ── Seat map image ────────────────────────────────────────────────────────
def render_map(rows, total_cols, runs, good, label, path: Path):
    """PNG of the auditorium; prime seats gold, other open seats green."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    prime = {(r["row"], lab) for r in runs for lab in r["labels"]}
    prime |= {(s["row"], s["label"]) for s in good}
    cell, pad_l, pad_t, pad_b = 22, 46, 56, 34
    w = pad_l * 2 + total_cols * cell
    h = pad_t + len(rows) * cell + pad_b
    img = Image.new("RGB", (w, h), (16, 20, 28))
    dr = ImageDraw.Draw(img)
    # screen
    dr.rounded_rectangle([pad_l + cell, 12, w - pad_l - cell, 30], 6, fill=(90, 100, 115))
    dr.text((w / 2 - 24, 15), "SCREEN", fill=(16, 20, 28))
    dr.text((8, 34), label.encode("ascii", "replace").decode(), fill=(200, 205, 215))
    for i, row in enumerate(rows):
        y = pad_t + i * cell
        in_zone = row["label"].upper() in TARGET_ROWS
        lab_col = (255, 193, 7) if in_zone else (120, 128, 140)
        dr.text((pad_l - 20, y + 4), row["label"], fill=lab_col)
        dr.text((w - pad_l + 8, y + 4), row["label"], fill=lab_col)
        for s in row["seats"]:
            x = pad_l + s["col"] * cell
            box = [x + 2, y + 2, x + cell - 4, y + cell - 4]
            if (row["label"], s["label"]) in prime:
                dr.rounded_rectangle(box, 4, fill=(255, 193, 7), outline=(255, 255, 255))
            elif s["open"] and s["type"] == "Standard":
                dr.rounded_rectangle(box, 4, fill=(46, 204, 113))
            elif s["open"]:                              # wheelchair/companion open
                dr.rounded_rectangle(box, 4, fill=(52, 152, 219))
            else:
                dr.rounded_rectangle(box, 4, fill=(52, 58, 70))
    dr.text((pad_l, h - 24), "gold = your seats   green = open   blue = wheelchair/companion   grey = taken",
            fill=(150, 156, 168))
    img.save(path)
    return path


# ── Alert channels ────────────────────────────────────────────────────────
def send_telegram(text: str, photo: Path | None = None) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    if photo and photo.exists():
        # Telegram photo captions max out at 1024 chars
        caption = text if len(text) <= 1000 else text[:960] + "\n...(truncated)"
        boundary = uuid.uuid4().hex
        parts = b""
        for name, val in (("chat_id", chat), ("caption", caption)):
            parts += (f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{name}\"\r\n\r\n{val}\r\n").encode()
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                  f"filename=\"seats.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        parts += photo.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto", data=parts,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        body = urllib.parse.urlencode({"chat_id": chat,
                                       "text": text[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=30).read()
    return True


def send_twilio(text: str) -> bool:
    sid, tok = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    frm, to = os.environ.get("TWILIO_FROM"), os.environ.get("TWILIO_TO")
    if not all((sid, tok, frm, to)):
        return False
    import base64
    body = urllib.parse.urlencode({"From": frm, "To": to, "Body": text}).encode()
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=body, headers={"Authorization": f"Basic {auth}",
                            "Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=30).read()
    return True


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> None:
    prev = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass

    current, prime_alerts, minor_alerts = {}, [], []
    alert_media, best_seat = [], None
    for show in qualifying_showtimes():
        if show["key"] in current:                    # duplicate-entry guard
            continue
        try:
            rows, cols = seat_map(show["showtime_id"])
        except RuntimeError as e:
            print(f"  ! {show['key']}: {e}", file=sys.stderr)
            continue

        # track the single best open seat (most central, middle rows first)
        # across daytime/evening shows (11 AM - 7 PM starts)
        hour = int(show["key"][11:13])
        if BEST_SEAT_HOURS[0] <= hour <= BEST_SEAT_HOURS[1]:
            center = (cols - 1) / 2
            for row in rows:
                rl = row["label"].upper()
                if rl not in TARGET_ROWS:
                    continue
                rank = ROW_PREF.find(rl)
                for s in row["seats"]:
                    if s["open"] and s["type"] == "Standard":
                        score = (abs(s["col"] - center), rank)
                        if best_seat is None or score < best_seat["score"]:
                            best_seat = {"score": score, "label": s["label"],
                                         "show": show["label"]}
        runs = prime_runs(rows, cols)
        good = good_scattered(rows, cols)
        n = total_open(rows)
        # every open Standard seat in the target rows, by label
        open_fk = sorted({s["label"] for r in rows
                          if r["label"].upper() in TARGET_ROWS
                          for s in r["seats"]
                          if s["open"] and s["type"] == "Standard"})
        current[show["key"]] = {"open": ",".join(open_fk), "n": n}

        was = prev.get(show["key"], {})
        was = was if isinstance(was, dict) else {}
        if "open" in was:
            prev_open = set(was["open"].split(",")) - {""}
        else:                       # old/missing state: baseline quietly this run
            prev_open = set(open_fk)
        new_seats = {l for l in open_fk if l not in prev_open}

        # a run only alerts if it contains >= 2 seats that JUST freed —
        # known blocks growing by one edge seat stay silent
        fresh_runs = [r for r in runs
                      if sum(1 for l in r["labels"] if l in new_seats) >= 2]
        new_central = [s for s in good if s["label"] in new_seats]
        best = f" | best: {'+'.join(runs[0]['labels'])}" if runs else ""
        print(f"  {show['label']}: {n} open, {len(runs)} run(s), "
              f"{len(good)} good central{best}"
              f"{' | FRESH' if (fresh_runs or new_central) else ''}")

        alerted = False
        if fresh_runs:                                # best case: a fresh pair+ together
            top = fresh_runs[0]
            freed = [l for l in top["labels"] if l in new_seats]
            tag = " [borderline, side-ish]" if top.get("near", 0) > BORDERLINE_OFF else ""
            prime_alerts.append(f"NEW: {show['label']}: {'+'.join(top['labels'])} "
                                f"({top['size']} TOGETHER, row {top['row']}{tag}; "
                                f"just freed: {', '.join(freed)})\n"
                                f"➜ BOOK THIS SHOW: {show['book_url']}")
            alerted = True
        elif (new_central and len(good) >= MIN_PARTY
              and len(good) - len(new_central) < MIN_PARTY):
            # separated-but-central seats alert only when the fresh ones COMPLETE
            # the pair — one more seat next to already-known ones stays silent
            def mark(s):                              # ~ prefix = borderline seat
                return ("~" if s["off"] > BORDERLINE_OFF else "") + s["label"]
            fresh = ", ".join(mark(s) for s in new_central[:4])
            already = [mark(s) for s in good
                       if s["label"] not in new_seats]
            extra = f" + already open: {', '.join(already[:4])}" if already else ""
            prime_alerts.append(f"NEW: {show['label']}: JUST FREED {fresh}{extra} "
                                f"({len(good)} central-ish seats, separated; "
                                f"~ = more side-ish)\n"
                                f"➜ BOOK THIS SHOW: {show['book_url']}")
            alerted = True
        if alerted:                                   # one image per alerted showtime
            alert_media.append((show["label"], rows, cols, runs, good))
        if not alerted and ALERT_ANY_INCREASE and n > was.get("n", 0):
            minor_alerts.append(f"{show['label']} — {n} seats (outside target zone)")
        time.sleep(0.25)

    STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))

    if prime_alerts or minor_alerts:
        head = ("YOUR SEATS ARE OPEN — The Odyssey IMAX 70mm (Scotiabank MTL)\n"
                "(every line below is NEW since the last check)\n"
                if prime_alerts else
                "New seats (not in your F–K zone) — The Odyssey IMAX 70mm\n")
        lines = prime_alerts + minor_alerts
        if len(lines) > 10:                          # keep push notifications short
            lines = lines[:10] + [f"...plus {len(lines) - 10} more showtimes"]
        msg = head + "\n".join(lines)
        if best_seat:
            msg += (f"\nBest single seat right now: {best_seat['label']} "
                    f"({best_seat['show']})")
        msg += f"\nBook NOW: {BOOKING_LINK}"
        sent = []
        for name, fn in (("telegram", send_telegram), ("twilio", send_twilio)):
            try:                                     # a failed send must never
                if fn(msg):                          # crash the run / lose state
                    sent.append(name)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {name} send failed: {e}", file=sys.stderr)
        # then one captioned seat-map image per alerted showtime (max 3)
        for label, rows_, cols_, runs_, good_ in alert_media[:3]:
            try:
                p = render_map(rows_, cols_, runs_, good_,
                               f"The Odyssey IMAX 70mm - {label}",
                               Path("seatmap.png"))
                if p:
                    send_telegram(f"Seat map — {label} (gold = your seats)",
                                  photo=p)
            except Exception as e:  # noqa: BLE001
                print(f"  ! map send failed for {label}: {e}", file=sys.stderr)
        print(f"ALERT sent via {sent or 'NO CHANNEL WORKED'}:\n{msg}")
    else:
        print(f"No changes in your target seats across {len(current)} qualifying showtimes.")


if __name__ == "__main__":
    main()
