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
MIN_PARTY = 2                            # min seats needed, even if separated
GOOD_SEAT_MAX_OFF = 8                    # a "good" seat sits within this many
                                         # columns of dead center (for separated seats)
ALERT_ANY_INCREASE = False               # True = also alert on any new seats anywhere

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
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Accept-Language": "en", "Referer": "https://www.cineplex.com/"}
    if with_key:
        headers["Ocp-Apim-Subscription-Key"] = API_KEY
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
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
                            m = re.search(r"showtimeId=(\d+)",
                                          sess.get("seatMapUrl", ""), re.I)
                            if not m:
                                continue
                            yield {
                                "key": start.strftime("%Y-%m-%d %H:%M"),
                                "label": start.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
                                "showtime_id": m.group(1),
                            }
        time.sleep(1)  # be polite


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
                    mid = (run[0]["col"] + run[-1]["col"]) / 2
                    found.append({"row": row["label"],
                                  "labels": [x["label"] for x in run],
                                  "size": len(run),
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
        boundary = uuid.uuid4().hex
        parts = b""
        for name, val in (("chat_id", chat), ("caption", text)):
            parts += (f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{name}\"\r\n\r\n{val}\r\n").encode()
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                  f"filename=\"seats.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        parts += photo.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto", data=parts,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
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

    current, prime_alerts, minor_alerts, best_photo = {}, [], [], None
    for show in qualifying_showtimes():
        if show["key"] in current:                    # duplicate-entry guard
            continue
        try:
            rows, cols = seat_map(show["showtime_id"])
        except RuntimeError as e:
            print(f"  ! {show['key']}: {e}", file=sys.stderr)
            continue
        runs = prime_runs(rows, cols)
        good = good_scattered(rows, cols)
        n = total_open(rows)
        sig = ";".join("+".join(r["labels"]) for r in runs)
        good_sig = ",".join(s["label"] for s in good)
        current[show["key"]] = {"n": n, "prime": sig, "good": good_sig}

        was = prev.get(show["key"], {})
        was = was if isinstance(was, dict) else {"n": was, "prime": "", "good": ""}
        seen_runs = set(was.get("prime", "").split(";")) - {""}
        seen_good = set(was.get("good", "").split(",")) - {""}
        new_runs = [r for r in runs if "+".join(r["labels"]) not in seen_runs]
        new_good = [s for s in good if s["label"] not in seen_good]
        best = f" | best: {'+'.join(runs[0]['labels'])}" if runs else ""
        print(f"  {show['label']}: {n} open, {len(runs)} run(s), "
              f"{len(good)} good central{best}"
              f"{' | NEW' if (new_runs or new_good) else ''}")

        alerted = False
        if new_runs:                                  # best case: seats together
            top = new_runs[0]
            prime_alerts.append(f"{show['label']}: {'+'.join(top['labels'])} "
                                f"({top['size']} TOGETHER, row {top['row']})")
            alerted = True
        elif new_good and len(good) >= MIN_PARTY:     # fallback: separated but central
            new_set = {s["label"] for s in new_good}
            fresh = ", ".join(s["label"] for s in new_good[:4])
            already = [s["label"] for s in good if s["label"] not in new_set]
            extra = f" + already open: {', '.join(already[:4])}" if already else ""
            prime_alerts.append(f"{show['label']}: JUST FREED {fresh}{extra} "
                                f"({len(good)} central seats, separated)")
            alerted = True
        if alerted and best_photo is None:            # image for the best find
            best_photo = render_map(rows, cols, runs, good,
                                    f"The Odyssey IMAX 70mm - {show['label']}",
                                    Path("seatmap.png"))
        if not alerted and ALERT_ANY_INCREASE and n > was.get("n", 0):
            minor_alerts.append(f"{show['label']} — {n} seats (outside target zone)")
        time.sleep(1)

    STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))

    if prime_alerts or minor_alerts:
        head = ("YOUR SEATS ARE OPEN — The Odyssey IMAX 70mm (Scotiabank MTL)\n"
                if prime_alerts else
                "New seats (not in your F–K zone) — The Odyssey IMAX 70mm\n")
        msg = head + "\n".join(prime_alerts + minor_alerts) + f"\nBook NOW: {BOOKING_LINK}"
        sent = []
        if send_telegram(msg, photo=best_photo if prime_alerts else None):
            sent.append("telegram")
        if send_twilio(msg):
            sent.append("twilio")
        print(f"ALERT sent via {sent or 'NO CHANNEL CONFIGURED'}:\n{msg}")
    else:
        print(f"No changes in your target seats across {len(current)} qualifying showtimes.")


if __name__ == "__main__":
    main()
