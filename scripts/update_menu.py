#!/usr/bin/env python3
"""
Fetches today's lunch menu from three Ilmala/Pasila restaurants, translates
dish names into EN / ES / ZH-CN (via DeepL) and ZH-TW (via local OpenCC
conversion of the ZH-CN result), and writes docs/menu.json for the static
page to fetch at runtime.

Designed to run daily from GitHub Actions. If a site's markup has changed
and a restaurant can't be parsed, that restaurant is marked "unavailable"
in the JSON instead of crashing the whole run -- the other restaurants
still get updated.

Known fragility (documented on purpose, not hidden):
- ninankeittio.fi's date labels in the weekly list have occasionally been
  wrong (e.g. a Tuesday labelled with a date three weeks out). We match by
  the closest date to today rather than an exact string match to absorb
  small typos, and flag it if the closest match is more than 2 days away.
- Menu group (soup/main/side/dessert) is inferred heuristically from
  price presence and keywords, not from an explicit label in the source.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

try:
    from opencc import OpenCC
    _cc = OpenCC('s2twp')  # simplified -> Taiwan-standard traditional
except Exception:
    _cc = None

# ninankeittio.fi (Akseli) sits behind Cloudflare. Our old self-identifying
# "IlmalaLunchBot" User-Agent was intermittently served a Cloudflare
# interstitial -- a 200 response whose body is a "checking your browser" page
# with NONE of the weekly menu in it -- when the request came from GitHub
# Actions' datacenter IPs. That surfaced as Akseli's recurring
# "no_date_header_found" even though the menu (decided and published a week
# ahead) was always on the real page. Presenting as an ordinary browser, with
# the Accept headers a browser sends, avoids the bot challenge.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}
TIMEOUT = 20
FETCH_RETRIES = 3
RETRY_BACKOFF = 3  # seconds; multiplied by the attempt number

# A Cloudflare/anti-bot interstitial returns 200 (so raise_for_status passes)
# but the body is tiny and carries one of these tell-tale phrases instead of
# the page. Treat such a response as a failed fetch and retry rather than
# parsing it into an empty ("unavailable") menu.
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl",
    "enable javascript and cookies",
    "checking your browser",
    "attention required",
)

FI_WEEKDAYS = ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai"]
VALID_TAGS = {"M", "L", "VL", "G", "KM", "VEG"}

DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
MENU_JSON_PATH = os.path.join(DOCS_DIR, "menu.json")
CACHE_PATH = os.path.join(DOCS_DIR, "translation_cache.json")

RESTAURANTS = [
    {
        "key": "akseli",
        "type": "akseli",
        "url": "https://www.ninankeittio.fi/helsinki-ilmala-akseli/",
    },
    {
        "key": "luft",
        "type": "dylan",
        "url": "https://www.lounaat.info/lounas/dylan-luft/helsinki",
    },
    {
        "key": "lailma",
        "type": "dylan",
        "url": "https://www.lounaat.info/lounas/dylan-la-ilma/helsinki",
    },
    {
        "key": "studio10",
        "type": "nordrest",
        "url": "https://nordrest.fi/restaurang/yle-studio10/",
    },
    {
        "key": "paattari",
        "type": "paattari",
        "url": "https://nordrest.fi/restaurang/ravintola-paattari/",
    },
]


def _looks_like_challenge(html):
    low = html.lower()
    return len(html) < 2000 or any(m in low for m in CHALLENGE_MARKERS)


def fetch_html(url):
    """GET the page, retrying if the response is a Cloudflare/anti-bot
    interstitial rather than the real content. Raises on the last attempt if
    every response still looks like a challenge, so the caller marks the
    restaurant parse_error instead of silently emitting an empty menu."""
    for attempt in range(1, FETCH_RETRIES + 1):
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
        if not _looks_like_challenge(html):
            return html
        print(
            f"WARN: {url} returned a bot-challenge/empty page "
            f"(attempt {attempt}/{FETCH_RETRIES})",
            file=sys.stderr,
        )
        if attempt < FETCH_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise requests.RequestException(f"bot-challenge page after {FETCH_RETRIES} attempts: {url}")


def fetch_soup(url):
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup


def fetch_text(url):
    return fetch_soup(url).get_text("\n")


def closest_day_section(text, today):
    """Find the header (Weekday D.M.) whose date is closest to today, and
    return (section_text, distance_in_days) or (None, None) if no header found."""
    pattern = re.compile(
        r"(Maanantai|Tiistai|Keskiviikko|Torstai|Perjantai)(?:na)?\s+(\d{1,2})\.(\d{1,2})\.",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None, None

    best = None
    for i, m in enumerate(matches):
        day, month = int(m.group(2)), int(m.group(3))
        for year_offset in (0, -1, 1):
            try:
                d = date(today.year + year_offset, month, day)
            except ValueError:
                continue
            dist = abs((d - today).days)
            if best is None or dist < best[0]:
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                best = (dist, text[start:end])
    return (best[1], best[0]) if best else (None, None)


def is_closed_section(section_text):
    return "suljettu" in section_text.lower()


def nordrest_day_section(text, today):
    """nordrest.fi's Studio10 page has no per-day dates in the weekly list --
    headers are bilingual weekday names only (e.g. 'GIOVENDí / TORSTAI'), and
    the whole week's menu appears twice in the fetched text (duplicate markup
    block, seemingly a responsive/mobile variant). So instead of matching a
    date like closest_day_section(), we just look up today's Finnish weekday
    name and take the text up to the next weekday header -- this stays
    correct even with the duplicated block, since the next header in
    document order is always the true end of today's section."""
    weekday_idx = today.weekday()  # Mon=0 .. Sun=6
    if weekday_idx > 4:
        return None
    fi_name = FI_WEEKDAYS[weekday_idx]
    header_re = re.compile(r"\b(" + "|".join(FI_WEEKDAYS) + r")\b", re.IGNORECASE)
    matches = list(header_re.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1).lower() == fi_name.lower():
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[m.end():end]
    return None


# Studio10's dishes carry allergen tags in a trailing parenthesis, e.g.
# "Pasta con pancetta e panna – Kermaista pekonipastaa (L)", rather than the
# inline standalone-token style the other restaurants use. We only treat a
# trailing "(...)" as tags if every comma-separated token inside it is a
# known code -- otherwise it's likely an unrelated parenthetical remark and
# the line gets skipped rather than mis-parsed as a dish.
NORDREST_TAG_CODES = {"M", "L", "VL", "G", "KM", "V", "VE", "VEG"}
NORDREST_LINE_RE = re.compile(r"^(.*\S)\s*\(([^)]*)\)\s*$")


def parse_nordrest(section_text):
    items = []
    for raw in section_text.split("\n"):
        line = raw.strip()
        if not line or _looks_like_boilerplate(line):
            continue
        m = NORDREST_LINE_RE.match(line)
        if not m:
            continue
        desc, tag_str = m.group(1), m.group(2)
        tokens = [t.strip().upper() for t in tag_str.split(",") if t.strip()]
        if not tokens or not all(t in NORDREST_TAG_CODES for t in tokens):
            continue  # trailing parens weren't allergen tags -- skip the line
        tags = {"Veg" if t in ("V", "VE", "VEG") else t for t in tokens}
        clean = re.sub(r"\s{2,}", " ", desc).strip(" -–")
        if not clean:
            continue
        items.append({"text": clean, "tags": sorted(tags), "price": None, "porridge": False})
    return items


PAATTARI_HEADER_RE = re.compile(
    r"^(" + "|".join(FI_WEEKDAYS) + r")(?:na)?\s+\d{1,2}\.\d{1,2}\.", re.IGNORECASE
)
PAATTARI_TAGS_ONLY_RE = re.compile(r"^\(([^)]*)\)$")


def paattari_day_paragraphs(soup, today):
    """Päättäri's weekly menu (nordrest.fi, Elementor-built) is a run of <p>
    tags: a bold-underlined 'Weekday D.M.YYYY' header, then one <p> per dish
    with the name in <strong> and the tagged description in <em>, up to the
    next weekday header. Unlike Studio10 (another nordrest.fi restaurant),
    name and tags don't share a text line, so this walks the DOM directly
    instead of flattened text -- flattening would put name and tags on
    separate lines with no reliable way to tell where one dish ends and the
    next begins (verified by inspecting the actual markup)."""
    weekday_idx = today.weekday()  # Mon=0 .. Sun=6
    if weekday_idx > 4:
        return None
    fi_name = FI_WEEKDAYS[weekday_idx]

    all_ps = soup.find_all("p")
    header_positions = [
        (i, PAATTARI_HEADER_RE.match(p.get_text(" ", strip=True)))
        for i, p in enumerate(all_ps)
    ]
    header_positions = [(i, m) for i, m in header_positions if m]
    if not header_positions:
        return None

    for pos, (i, m) in enumerate(header_positions):
        if m.group(1).lower() == fi_name.lower():
            end = header_positions[pos + 1][0] if pos + 1 < len(header_positions) else len(all_ps)
            return all_ps[i + 1:end]
    return None


def _paattari_split_tags(s):
    """Pull a trailing '(TAG, TAG)' off a description, or recognize a
    string that's nothing but a tag group (Päättäri sometimes puts the tags
    in their own line/element with no accompanying text). Returns
    (remaining_text, tags_set)."""
    s = s.strip()
    if not s:
        return "", set()
    m = PAATTARI_TAGS_ONLY_RE.match(s)
    if not m:
        m = NORDREST_LINE_RE.match(s)
    if m:
        groups = m.groups()
        desc, tag_str = (groups[0], groups[1]) if len(groups) == 2 else ("", groups[0])
        tokens = [t.strip().upper() for t in tag_str.split(",") if t.strip()]
        if tokens and all(t in NORDREST_TAG_CODES for t in tokens):
            tags = {"Veg" if t in ("V", "VE", "VEG") else t for t in tokens}
            return desc.strip(), tags
    return s, set()


def parse_paattari(day_paragraphs):
    items = []
    for p in day_paragraphs:
        strong = p.find("strong")
        if strong:
            name = strong.get_text(" ", strip=True)
            em = p.find("em")
            desc_raw = em.get_text(" ", strip=True) if em else ""
            text, tags = _paattari_split_tags(desc_raw)
            if not name:
                continue
            full_text = f"{name} – {text}" if text else name
            items.append({"text": full_text, "tags": sorted(tags), "price": None, "porridge": False})
            continue

        plain = p.get_text(" ", strip=True)
        if not plain or _looks_like_boilerplate(plain):
            continue
        text, tags = _paattari_split_tags(plain)
        if not text:
            continue
        items.append({"text": text, "tags": sorted(tags), "price": None, "porridge": False})
    return items


TAG_TOKEN = r"(?:VL|KM|VEG|VE|L|M|G)"
# Matches one or more comma-separated allergen codes as a standalone word
# group anywhere in the line, e.g. "M,G,KM" in the middle of a sentence --
# these menus tag each food component inline, not just at the end.
TAG_CLUSTER_RE = re.compile(rf"\b{TAG_TOKEN}(?:\s*,\s*{TAG_TOKEN})*\b", re.IGNORECASE)


def extract_tags_and_price(raw_line):
    """Pull all allergen-tag mentions and a euro price off a line, returning
    (clean_description, tags_list, price_str_or_None). Tags can appear
    anywhere in the line (each dish component is often tagged separately),
    not just at the very end."""
    line = raw_line.strip()

    price = None
    # NB: don't put \b right after the € sign -- € isn't a word character,
    # so \b never matches there and the price silently fails to extract.
    price_match = re.search(r"(\d{1,2}[.,]\d{2})\s*(€|e\b)", line, re.IGNORECASE)
    if price_match:
        price = price_match.group(1).replace(".", ",") + " €"
        line = line[: price_match.start()] + line[price_match.end():]

    # bracketed markdown-link tags e.g. [l](#l "Laktoositon")
    bracket_tags = re.findall(r"\[(l|g|m|vl|km|veg)\]\([^)]*\)", line, re.IGNORECASE)
    line = re.sub(r"\[(l|g|m|vl|km|veg)\]\([^)]*\)", " ", line, flags=re.IGNORECASE)

    cluster_tags = []

    def _collect(m):
        cluster_tags.extend(m.group(0).split(","))
        return " "

    line = TAG_CLUSTER_RE.sub(_collect, line)

    all_tags = set()
    for t in list(bracket_tags) + cluster_tags:
        t = t.strip().upper()
        if t in ("VE", "VEG"):
            all_tags.add("Veg")
        elif t in ("M", "L", "VL", "G", "KM"):
            all_tags.add(t)

    clean = re.sub(r"[*_`]", "", line)
    # A parenthesised tag like "(veg)" leaves empty "( )" behind once its code
    # is pulled out -- drop the now-empty brackets so they don't show in names.
    clean = re.sub(r"\(\s*\)", " ", clean)
    clean = re.sub(r"\s*,\s*,+", ",", clean)     # collapse doubled commas left behind
    clean = re.sub(r"\s{2,}", " ", clean)
    clean = re.sub(r"\s+,", ",", clean).strip(" ,.-")  # e.g. "Ends , paahdettua" -> "Ends, paahdettua"
    return clean, sorted(all_tags), price


# Boilerplate/legend lines to ignore no matter which restaurant page they show
# up on. Matched as a case-insensitive substring against the whole line.
BOILERPLATE_SNIPPETS = [
    "allergeenit", "käytämme suomalaista", "tulosta lounaslista",
    "vähänlaktoosinen", "laktoositon /", "maidoton /", "gluteeniton /",
    "kananmunaton /", "vegaaninen /",
    "pehmis & lisukkeet", "huomioimme myös muut erikoisruokavaliot",
    "lisätietoja ruoan allergeeneistä",
]


def _looks_like_boilerplate(line):
    low = line.lower()
    if len(line) < 4:
        return True
    if re.match(r"^viikko\s+\d+", low):
        return True
    return any(snippet in low for snippet in BOILERPLATE_SNIPPETS)


def parse_akseli(section_text):
    items = []
    for raw in section_text.split("\n"):
        # Handles both plain BeautifulSoup text (no bullet marker) and
        # markdown-style "- *text*" bullets, in case the source format
        # changes or this is re-used with a markdown-based fetch.
        line = raw.strip().lstrip("-* ").strip()
        # The daily menu always ends at the "Allergeenit" legend header; below
        # it is permanent site furniture (allergen key, catering/meeting-room
        # marketing, contact details, footer) that isn't today's food. Stop
        # here rather than let all of it leak in as fake dishes.
        if re.match(r"^allergeenit\b", line, re.IGNORECASE):
            break
        if not line or _looks_like_boilerplate(line):
            continue
        # The "Puurobaari" porridge bar is Akseli's breakfast offering, not
        # part of lunch, so drop it. The "Puurobaari:" label sits on its own
        # line and the porridge dish on the next, so match the dish by its
        # name (…puuroa) as well as the label itself.
        if "puuro" in line.lower():
            continue
        text, tags, price = extract_tags_and_price(line)
        if not text:
            continue
        items.append({"text": text, "tags": tags, "price": price, "porridge": False})
    return items


def _is_dylan_placeholder(line):
    low = line.lower()
    return "lounaslista ravintolan sivuilta" in low or (
        "katso" in low and "sivuilta" in low
    )


# lounaat.info marks a dish component that carries NO allergen codes with a
# bare "-" standing in where its codes would go, right before the "ja" (Finnish
# "and") that introduces the next component, e.g.
#   "Rapeaa kiovan kanaa - ja aoilia  m  g"
# means the (breaded) chicken has no codes and only the aioli is M/G. Without
# special handling the trailing "m"/"g" get merged onto the whole line, falsely
# marking the chicken dairy-/gluten-free. We split on " - ja " so the codes
# attach to the sauce alone. The plain " - " used merely as a name separator
# (e.g. "Poulet au vinaigre - lyonin kanaa") is NOT matched, because the marker
# is specifically a dash immediately followed by "ja".
DYLAN_NO_TAG_MARKER_RE = re.compile(r"^(?P<main>.+?)\s+-\s+ja\s+(?P<sauce>.+)$", re.IGNORECASE)


def _split_dylan_no_tag_marker(text):
    """Return (main_without_tags, sauce) when `text` uses the "- ja" no-allergen
    marker, or (text, None) when it doesn't."""
    m = DYLAN_NO_TAG_MARKER_RE.match(text)
    if not m:
        return text, None
    main = m.group("main").strip(" ,.-")
    sauce = m.group("sauce").strip(" ,.-")
    if not main or not sauce:
        return text, None
    return main, sauce


def parse_dylan(section_text):
    items = []
    pending_price = None
    for raw in section_text.split("\n"):
        line = raw.strip().lstrip("-* ").strip()
        if not line:
            continue
        # lounaat.info sometimes has no menu for the day and shows only a
        # "check the restaurant's own site" placeholder (e.g. "Katso päivän
        # lounaslista ravintolan sivuilta!"). Skip it so it isn't emitted as a
        # fake dish -- the restaurant then falls through to unavailable.
        if _is_dylan_placeholder(line):
            continue
        # A line that's *only* allergen codes (e.g. a lone "g") is 1-2 chars
        # and would otherwise be swallowed by the boilerplate length filter
        # below -- check for it first so it survives to the tag-merging step.
        is_tag_only_line = bool(re.fullmatch(rf"{TAG_TOKEN}(?:\s*,\s*{TAG_TOKEN})*", line, re.IGNORECASE))
        if not is_tag_only_line and _looks_like_boilerplate(line):
            continue
        low = line.lower()
        if "buffetlounas" in low or "sisältää" in low:
            continue
        if low.startswith("lounas kello"):
            break
        price_only = re.fullmatch(r"\d{1,2}[.,]\d{2}\s*e", line, re.IGNORECASE)
        if price_only:
            pending_price = price_only.group(0).lower().replace("e", "").strip() + " €"
            continue
        text, tags, inline_price = extract_tags_and_price(line)
        if not text:
            # Dylan's pages often put a dish's allergen codes on their own
            # line(s) right after the dish name (e.g. "Curry-kookoskanakeitto"
            # then "m" then "g") instead of inline, so a tag-only line has no
            # text of its own -- attach it to the most recently added dish.
            if tags and items:
                items[-1]["tags"] = sorted(set(items[-1]["tags"]) | set(tags))
            continue
        price = inline_price or pending_price
        pending_price = None
        main, sauce = _split_dylan_no_tag_marker(text)
        if sauce is not None:
            # The main component's codes are the empty "-" placeholder, so it
            # gets no tags; any inline tags (and the tag-only lines that follow)
            # belong to the sauce, which becomes items[-1] for the merge step.
            items.append({"text": main, "tags": [], "price": price, "porridge": False})
            items.append({"text": sauce, "tags": tags, "price": None, "porridge": False})
            continue
        items.append({"text": text, "tags": tags, "price": price, "porridge": False})
    return items


def classify_groups(items):
    if not items:
        return []
    n = len(items)
    keyed = []
    for i, it in enumerate(items):
        low = it["text"].lower()
        if it.get("porridge"):
            key = "porridge"
        elif "keitto" in low:
            key = "soup"
        elif it["price"] is None and i == n - 1:
            key = "dessert"
        elif it["price"] is None:
            key = "side"
        else:
            key = "main"
        keyed.append((key, it))

    order = ["porridge", "main", "side", "soup", "dessert"]
    groups = []
    for key in order:
        its = [it for k, it in keyed if k == key]
        if its:
            groups.append({"key": key, "items": its})
    return groups


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def deepl_translate(texts, target_lang, api_key):
    if not texts:
        return []
    # DeepL deprecated auth_key-in-form-body in Nov 2025; auth now goes in
    # the Authorization header instead.
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    payload = [("target_lang", target_lang), ("source_lang", "FI")]
    payload += [("text", t) for t in texts]
    resp = requests.post("https://api-free.deepl.com/v2/translate", data=payload, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return [t["text"] for t in resp.json()["translations"]]


def translate_all(fi_texts, api_key, cache):
    # Re-attempt anything never cached, or cached with a null from a prior
    # failed DeepL call -- otherwise a transient API error permanently
    # poisons that dish's translation, since it "exists" in the cache.
    to_translate = [
        t for t in dict.fromkeys(fi_texts)
        if not cache.get(t) or any(cache[t].get(k) is None for k in ("en", "es", "zh-CN"))
    ]
    if to_translate and api_key:
        for target, cache_key in (("EN", "en"), ("ES", "es"), ("ZH", "zh-CN")):
            try:
                results = deepl_translate(to_translate, target, api_key)
            except requests.RequestException as e:
                print(f"WARN: DeepL translation to {target} failed: {e}", file=sys.stderr)
                results = [None] * len(to_translate)
            for fi, translated in zip(to_translate, results):
                cache.setdefault(fi, {})[cache_key] = translated
    elif to_translate and not api_key:
        print("WARN: no DEEPL_API_KEY set, leaving new dishes untranslated", file=sys.stderr)

    out = {}
    for fi in fi_texts:
        entry = cache.get(fi, {})
        en = entry.get("en") or fi
        es = entry.get("es") or fi
        zh_cn = entry.get("zh-CN") or fi
        zh_tw = _cc.convert(zh_cn) if (_cc and zh_cn) else zh_cn
        out[fi] = {"fi": fi, "en": en, "es": es, "zh-CN": zh_cn, "zh-TW": zh_tw}
    return out


def main():
    api_key = os.environ.get("DEEPL_API_KEY", "")
    today = datetime.now(timezone(timedelta(hours=3))).date()  # Europe/Helsinki (EEST, UTC+3 in summer)

    raw_results = {}
    all_fi_texts = []
    for rest in RESTAURANTS:
        payload = build_restaurant_payload_pass1(rest, today)
        raw_results[rest["key"]] = payload
        for g in payload.get("_groups_raw", []):
            for it in g["items"]:
                all_fi_texts.append(it["text"])

    cache = load_cache()
    translations = translate_all(all_fi_texts, api_key, cache)
    save_cache(cache)

    output = {
        "date": today.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "restaurants": {},
    }
    for rest in RESTAURANTS:
        raw = raw_results[rest["key"]]
        groups_raw = raw.pop("_groups_raw", [])
        raw["groups"] = [
            {
                "key": g["key"],
                "items": [
                    {"name": translations[it["text"]], "tags": it["tags"], "price": it["price"]}
                    for it in g["items"]
                ],
            }
            for g in groups_raw
        ]
        output["restaurants"][rest["key"]] = raw

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"Wrote {MENU_JSON_PATH}")


def build_restaurant_payload_pass1(rest, today):
    """Fetch + parse only (no translation yet); keeps raw FI groups under
    _groups_raw so main() can batch-translate everything in one pass."""
    try:
        if rest["type"] == "paattari":
            soup = fetch_soup(rest["url"])
            paragraphs = paattari_day_paragraphs(soup, today)
            if paragraphs is None:
                return {"closed": False, "unavailable": True, "note": "no_date_header_found", "_groups_raw": []}
            items = parse_paattari(paragraphs)
            groups = [{"key": "main", "items": items}] if items else []
            return {
                "closed": False,
                "unavailable": len(groups) == 0,
                "note": None,
                "_groups_raw": groups,
            }

        text = fetch_text(rest["url"])

        if rest["type"] == "nordrest":
            section = nordrest_day_section(text, today)
            if section is None:
                return {"closed": False, "unavailable": True, "note": "no_date_header_found", "_groups_raw": []}
            # Parse dishes first: actual dishes are authoritative over any
            # "suljettu" text, because these pages keep stale holiday-closure
            # banners in the markup for weeks after reopening (e.g. a "suljettu
            # 3.7-4.8" note still shown days after the 5.8 reopening). Only
            # trust the closed signal when today's section has no dishes.
            items = parse_nordrest(section)
            if not items and is_closed_section(section):
                return {"closed": True, "unavailable": False, "note": None, "_groups_raw": []}
            groups = [{"key": "main", "items": items}] if items else []
            return {
                "closed": False,
                "unavailable": len(groups) == 0,
                "note": None,
                "_groups_raw": groups,
            }

        section, distance = closest_day_section(text, today)
        if section is None:
            return {"closed": False, "unavailable": True, "note": "no_date_header_found", "_groups_raw": []}
        if is_closed_section(section):
            return {"closed": True, "unavailable": False, "note": None, "_groups_raw": []}

        if rest["type"] == "akseli":
            items = parse_akseli(section)
        else:
            items = parse_dylan(section)

        groups = classify_groups(items)
        note = f"date_mismatch:{distance}d" if (distance is not None and distance > 2) else None
        return {
            "closed": False,
            "unavailable": len(groups) == 0,
            "note": note,
            "_groups_raw": groups,
        }
    except Exception as e:  # noqa: BLE001
        print(f"ERROR parsing {rest['key']} ({rest['url']}): {e}", file=sys.stderr)
        return {"closed": False, "unavailable": True, "note": "parse_error", "_groups_raw": []}


if __name__ == "__main__":
    main()
