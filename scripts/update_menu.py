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
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

try:
    from opencc import OpenCC
    _cc = OpenCC('s2twp')  # simplified -> Taiwan-standard traditional
except Exception:
    _cc = None

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IlmalaLunchBot/1.0; +https://github.com/)"}
TIMEOUT = 20

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
]


def fetch_text(url):
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n")


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
        if not line or _looks_like_boilerplate(line):
            continue
        is_porridge = "puurobaari" in line.lower()
        text, tags, price = extract_tags_and_price(line)
        text = re.sub(r"(?i)puurobaari:?\s*", "", text).strip()
        if not text:
            continue
        items.append({"text": text, "tags": tags, "price": price, "porridge": is_porridge})
    return items


def parse_dylan(section_text):
    items = []
    pending_price = None
    for raw in section_text.split("\n"):
        line = raw.strip().lstrip("-* ").strip()
        if not line or _looks_like_boilerplate(line):
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
            continue
        price = inline_price or pending_price
        pending_price = None
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
    payload = [("auth_key", api_key), ("target_lang", target_lang), ("source_lang", "FI")]
    payload += [("text", t) for t in texts]
    resp = requests.post("https://api-free.deepl.com/v2/translate", data=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return [t["text"] for t in resp.json()["translations"]]


def translate_all(fi_texts, api_key, cache):
    to_translate = [t for t in dict.fromkeys(fi_texts) if t not in cache]
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
        text = fetch_text(rest["url"])
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
