# Ilmala Lunch Board

Automatically scrapes today's lunch menus from five restaurants around Pasila / Ilmala, translates the dish names, and lets you filter dishes by allergen tags (M / L / VL / G / KM / Veg). The page UI is available in English, Finnish, and Simplified Chinese.

Restaurants covered:

- **Ravintola Akseli** (Ninan Keittiö · Ilmalan Aura) — `ninankeittio.fi`
- **Dylan Luft** (Ilmalantori) — `lounaat.info`
- **Dylan La Ilma** (Ilmalanrinne) — `lounaat.info`
- **Ravintola Studio10** (Nordrest · Yle-talo) — `nordrest.fi`
- **Ravintola Päättäri** (Nordrest · Ilmala) — `nordrest.fi`

## How it works

- `docs/index.html` — static page that `fetch('./menu.json')` at runtime. Pure front end, no backend. Language switcher offers **EN / FI / 中文**.
- `scripts/update_menu.py` — fetches each restaurant page, parses today's menu, translates the dish names via DeepL, and writes `docs/menu.json`.
- `.github/workflows/update-menu.yml` — runs the script automatically every weekday morning and commits the result.

The three source sites have different page structures, so the scraper uses a dedicated parser per layout:

- **Akseli** — flat text with the day's menu at the top; parsing stops at the `Allergeenit` legend header so the catering/meeting-room marketing and footer below it don't leak in as fake dishes. The `Puurobaari` porridge bar is dropped (it's breakfast, not lunch).
- **Dylan Luft / La Ilma** (`lounaat.info`) — matches the day header closest to today (see the "known limitations" note about date typos) and reads allergen codes that appear on their own lines right after each dish name.
- **Studio10 / Päättäri** (`nordrest.fi`) — allergen codes come in a trailing `(...)`. Studio10 is only treated as closed when today's section has no dishes, so a stale summer-holiday "suljettu" banner left in the markup after reopening no longer marks it closed. Päättäri is parsed straight from the DOM because its dish name and tags don't share a text line.

Translation: DeepL translates the Finnish dish names into **EN / ES / ZH-CN**, and Traditional Chinese (ZH-TW) is produced locally via OpenCC. All five fields (`fi` / `en` / `es` / `zh-CN` / `zh-TW`) are stored in `menu.json`; the current page UI surfaces English, Finnish, and Simplified Chinese.

## Deployment (one-time setup)

1. **Create the repo** — make a new GitHub repository (public or private; a public repo on the free Pages tier is simplest), and push this folder's contents keeping the directory structure intact.

2. **Get a free DeepL API key** — sign up for "DeepL API Free" (not Pro) at https://www.deepl.com/pro-api. You'll get a key ending in `:fx`. The free tier allows 500,000 characters/month, far more than this usage (a few dozen dish names per day) will ever need.

3. **Add the key as a repository secret** — repo → Settings → Secrets and variables → Actions → New repository secret, name it `DEEPL_API_KEY`, value = the key from step 2.

4. **Enable GitHub Pages** — repo → Settings → Pages → Build and deployment → Source: "Deploy from a branch" → Branch: `main`, folder: `/docs` → Save. After a few minutes the page is live at `https://<your-username>.github.io/<repo-name>/`.

5. **Trigger the workflow once manually** (no need to wait for tomorrow morning) — repo → Actions → "Update Ilmala Lunch Menu" → "Run workflow". After it finishes, `docs/menu.json` is updated and committed automatically, and Pages shows the new data after its next build (usually 1–2 minutes).

After these five steps everything is automatic: every weekday morning the workflow scrapes, translates, and publishes without any manual work.

The scheduled run fires at **00:17 UTC, Monday–Friday** (03:17 Helsinki in summer / 02:17 in winter). The off-the-hour minute is far less contended than minute 0, so GitHub dispatches it closer to on time, and the early slot leaves hours of slack before anyone checks the menu even if GitHub delays the run.

## Known limitations (stated honestly, not hidden)

- **Scraping is keyword/regex based, not an official API.** If a source site changes its page structure, parsing may break. The script is designed so that a failure at one restaurant doesn't take down the others — the failed one shows "could not read today's menu, check the website" on the page instead of showing wrong data or crashing the whole run.
- **Dish grouping (main / soup / side / dessert) is a heuristic guess**, based on whether a price is present and on keywords (e.g. `keitto` → soup), not an explicit label from the source. It's right most of the time, but individual dishes can land in the wrong group.
- **Dish-name translation is machine translation (DeepL)**, not human-reviewed. Everyday wording is fine, but for allergens the page tells you to confirm with staff in person — the allergen codes (M / L / VL / G / KM / Veg) themselves are extracted verbatim from the source text with regex and are never translated, so they're relatively trustworthy.
- **`ninankeittio.fi`'s date labels are occasionally wrong** (we've seen a "Tuesday" labelled with a date three weeks out). The scraper falls back to matching the date closest to today, and flags the page if the closest match is more than 2 days off — but an occasional manual spot-check is still wise.
- The scheduled job runs once per weekday. If a restaurant is often still un-updated when you check in the morning (e.g. it posts its menu late some days), you can push the cron time in `update-menu.yml` later, or add a second run as a fallback.

## Local testing

```bash
pip install -r requirements.txt --break-system-packages   # or use a virtualenv
export DEEPL_API_KEY=your_key   # optional — without it the script still runs,
                                # but new dish names stay in Finnish (untranslated)
python scripts/update_menu.py
cat docs/menu.json
```
