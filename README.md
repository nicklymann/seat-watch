# Cineplex Seat Watch — The Odyssey IMAX 70mm (Scotiabank Montréal)

Runs in the cloud on GitHub Actions every ~5 minutes, 24/7 — your computer can be off.
It calls the same public endpoints the cineplex.com website uses (no login, no scraping
of rendered pages) and pings your phone only when seats you'd actually take open up in
rows F–K: best case 2–3 **adjacent** seats (anywhere in F–K), or failing that 2+
**separated seats near the center** of those rows. The Telegram alert includes a
rendered image of the actual seat map with your seats highlighted in gold.

Qualifying showtimes: **any time Sat/Sun, and 5:00 PM or later Mon–Fri**, next 28 days
(covers Cineplex's full published range). Alerts name the exact seats and flag which
one JUST FREED vs. already open. Silence = nothing new; an alert means act now.

## Setup (~10 minutes, free)

### 1. Create the Telegram alert bot (recommended — free, instant push)
1. Install Telegram on your phone, then message **@BotFather**.
2. Send `/newbot`, pick any name/username. BotFather replies with a **bot token** — save it.
3. Message your new bot anything (e.g. "hi") so it can reply to you.
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and copy the
   `"chat":{"id": ...}` number — that's your **chat ID**.

*(Optional instead/also: Twilio for real SMS — you'll need a Twilio account, a phone
number, and the four values listed in step 3.)*

### 2. Create the GitHub repo
1. Sign in at github.com → **New repository** → name it anything (e.g. `seat-watch`),
   set it **Private**, create.
2. Upload `watcher.py` and `README.md`: repo page → **Add file → Upload files** →
   drag them in → Commit.
3. Add the workflow file. **Heads up: the `.github` folder is hidden on Mac**
   (names starting with a dot don't show in Finder — press `Cmd+Shift+.` inside
   this folder to reveal it). Easiest way that avoids the hidden-folder problem
   entirely: on the repo page click **Add file → Create new file**, type exactly
   `.github/workflows/watch.yml` as the filename (typing the `/` creates the
   folders), then open the hidden `watch.yml` from this folder (or re-download it
   from the chat), copy its contents in, and **Commit**.

### 3. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID |
| `TWILIO_ACCOUNT_SID` | *(optional)* Twilio SID |
| `TWILIO_AUTH_TOKEN` | *(optional)* Twilio auth token |
| `TWILIO_FROM` | *(optional)* your Twilio number, e.g. `+15145551234` |
| `TWILIO_TO` | *(optional)* your cell, e.g. `+15145559876` |

### 4. Test it
Repo → **Actions** tab → *Cineplex seat watch* → **Run workflow**. Watch the log:
you should see each qualifying showtime with its live seat count, and a Telegram
message if any have open seats. After that it runs itself every ~15 minutes.

## Notes & honest caveats
- **GitHub cron isn't exact** — runs can lag 3–15 min at busy times. Still far better
  than a laptop-bound watcher.
- Seats get re-grabbed fast on this release; an alert means *book immediately* via
  the link in the message.
- The API key in `watcher.py` is the public one embedded in cineplex.com for every
  visitor. If Cineplex rotates it, the run will fail with 401 — grab the new key from
  the site (DevTools → Network → any `showtimes` request → `Ocp-Apim-Subscription-Key`
  header) and set it as a `CINEPLEX_API_KEY` secret (add it under `env:` in the workflow).
- If runs start failing, GitHub emails you automatically, so a silent-blind watcher
  can't sneak up on you.
- Polling is modest (a few dozen small requests per run) but it's still automated
  access to Cineplex's site — keep the interval reasonable and shut the workflow off
  once you've got your tickets: repo → Actions → the workflow → "…" → **Disable workflow**.

## Tweaks
Edit the config block at the top of `watcher.py`:
- `TARGET_ROWS = ("F","G","H","I","J","K")` — the rows you'll accept
- `MIN_ADJACENT = 2` — alert threshold for seats together (set 3 to only hear about trios)
- `MIN_PARTY = 2` / `GOOD_SEAT_MAX_OFF = 8` — separated-seats rule: alert when at
  least MIN_PARTY open seats each sit within GOOD_SEAT_MAX_OFF columns of dead center
- `ALERT_ANY_INCREASE = False` — set `True` to also get (noisier) alerts when any
  seats appear anywhere, even scattered singles outside F–K
- `WEEKDAY_EARLIEST_HOUR = 17` — change the 5 PM cutoff
- `DAYS_AHEAD = 28` — widen/narrow the window
- `LOCATION_ID` / `MOVIE_PATTERN` — different theatre or film
- Cron interval: `.github/workflows/watch.yml` → the `cron:` line (GitHub's minimum is 5 min)
