#!/usr/bin/env python3
"""IndieGem catalog builder: Steam fetch + Gemini summary + static HTML."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "games.json"
TEMPLATES_DIR = ROOT / "templates"
DOCS_DIR = ROOT / "docs"
POSTS_DIR = DOCS_DIR / "posts"
ASSETS_DIR = DOCS_DIR / "assets"

# Schedule I store ID is 3164500 (3164000 is a different app).
SEED_APP_IDS = [
    2379780,  # Balatro
    1794680,  # Vampire Survivors
    646570,   # Slay the Spire
    3164500,  # Schedule I
    1145360,  # Hades
    105600,   # Terraria
    1942280,  # Brotato
    250900,   # The Binding of Isaac: Rebirth
    367520,   # Hollow Knight
    504230,   # Celeste
    264710,   # Subnautica
    1966720,  # Lethal Company
    413150,   # Stardew Valley
    960090,   # Bloons TD 6
    1332010,  # Stray
    1426210,  # It Takes Two
    892970,   # Valheim
    1158310,  # Crusader Kings III
    281990,   # Stellaris
    394360,   # Hearts of Iron IV
    242760,   # The Forest
    553850,   # HELLDIVERS 2
    1086940,  # Baldur's Gate 3
    1245620,  # ELDEN RING
    2124490,  # SILENT HILL 2
]
JST = timezone(timedelta(hours=9))
STEAM_SLEEP = 1.2
LOG = logging.getLogger("indiegem")

REVIEW_SCORE_JA = {
    "Overwhelmingly Positive": "\u5727\u5012\u7684\u306b\u597d\u8a55",
    "Very Positive": "\u975e\u5e38\u306b\u597d\u8a55",
    "Positive": "\u597d\u8a55",
    "Mostly Positive": "\u3084\u3084\u597d\u8a55",
    "Mixed": "\u8cdb\u5426\u4e21\u8ad6",
    "Mostly Negative": "\u3084\u3084\u4e0d\u8a55",
    "Negative": "\u4e0d\u8a55",
    "Very Negative": "\u975e\u5e38\u306b\u4e0d\u8a55",
    "Overwhelmingly Negative": "\u5727\u5012\u7684\u306b\u4e0d\u8a55",
    "\u5727\u5012\u7684\u306b\u597d\u8a55": "\u5727\u5012\u7684\u306b\u597d\u8a55",
    "\u975e\u5e38\u306b\u597d\u8a55": "\u975e\u5e38\u306b\u597d\u8a55",
    "\u597d\u8a55": "\u597d\u8a55",
    "\u3084\u3084\u597d\u8a55": "\u3084\u3084\u597d\u8a55",
    "\u8cdb\u5426\u4e21\u8ad6": "\u8cdb\u5426\u4e21\u8ad6",
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "excerpt": {"type": "string"},
        "why_now": {"type": "string"},
        "buzz_story": {"type": "string"},
        "three_line_summary": {"type": "array", "items": {"type": "string"}},
        "swamp_points": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
        "specs_note": {"type": "string"},
        "language_note": {"type": "string"},
    },
    "required": [
        "excerpt",
        "why_now",
        "buzz_story",
        "three_line_summary",
        "swamp_points",
        "caveats",
        "specs_note",
        "language_note",
    ],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.8",
}
COOKIES = {
    "birthtime": "568022401",
    "mature_content": "1",
    "wants_mature_content": "1",
    "lastagecheckage": "1-0-1988",
    "timezoneOffset": "32400,0",
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def now_jst() -> datetime:
    return datetime.now(JST)


def iso_now() -> str:
    return now_jst().isoformat(timespec="seconds")


def format_jst(dt: datetime | None = None) -> str:
    current = dt or now_jst()
    return current.strftime("%Y-%m-%d %H:%M JST")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def http_get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response | None:
    kwargs.setdefault("timeout", 30)
    for attempt in range(5):
        try:
            response = session.get(url, **kwargs)
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 4)) + attempt * 2
                LOG.warning("429 for %s, sleep %ss", url, wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOG.warning("GET failed (%s/%s) %s: %s", attempt + 1, 5, url, exc)
            time.sleep(2 ** attempt)
    return None


def load_existing() -> dict[int, dict[str, Any]]:
    if not DATA_PATH.exists():
        return {}
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("games.json is invalid; starting fresh")
        return {}
    games = {}
    for item in payload.get("games", []):
        try:
            games[int(item["app_id"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    return games


def discover_app_ids(session: requests.Session, steam_key: str, extra_ids: list[int], seed_only: bool) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()

    def add(app_id: int) -> None:
        if app_id and app_id not in seen:
            seen.add(app_id)
            ordered.append(app_id)

    for app_id in SEED_APP_IDS + extra_ids:
        add(app_id)
    if seed_only:
        return ordered

    featured = http_get(
        session,
        "https://store.steampowered.com/api/featuredcategories/",
        params={"cc": "jp", "l": "japanese"},
    )
    time.sleep(STEAM_SLEEP)
    if featured is not None:
        try:
            body = featured.json()
            for key in ("specials", "top_sellers", "new_releases", "coming_soon"):
                for item in body.get(key, {}).get("items", []):
                    if int(item.get("type", 0)) == 0:
                        add(int(item["id"]))
        except (ValueError, TypeError, KeyError) as exc:
            LOG.warning("featuredcategories parse failed: %s", exc)

    search = http_get(
        session,
        "https://store.steampowered.com/search/results/",
        params={
            "query": "",
            "start": 0,
            "count": 40,
            "infinite": 1,
            "filter": "globaltopsellers",
            "category1": 998,
            "tags": 492,
            "cc": "jp",
            "l": "japanese",
        },
    )
    time.sleep(STEAM_SLEEP)
    if search is not None:
        html_blob = ""
        try:
            payload = search.json()
            html_blob = payload.get("results_html") or ""
        except ValueError:
            html_blob = search.text
        for match in re.findall(r"data-ds-appid=\"(\d+)\"", html_blob):
            add(int(match))

    spy = http_get(session, "https://steamspy.com/api.php", params={"request": "top100in2weeks"})
    time.sleep(STEAM_SLEEP)
    if spy is not None:
        try:
            for key in spy.json().keys():
                if str(key).isdigit():
                    add(int(key))
        except ValueError:
            LOG.warning("steamspy parse failed")

    if steam_key:
        charts = http_get(
            session,
            "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/",
            params={"key": steam_key},
        )
        time.sleep(STEAM_SLEEP)
        if charts is not None:
            try:
                ranks = charts.json().get("response", {}).get("ranks", [])
                for row in ranks:
                    add(int(row.get("appid") or row.get("app_id") or 0))
            except (ValueError, TypeError):
                LOG.warning("charts parse failed")

    LOG.info("discovered %s candidate app ids", len(ordered))
    return ordered


def fetch_app_details(session: requests.Session, app_id: int) -> dict[str, Any] | None:
    response = http_get(
        session,
        "https://store.steampowered.com/api/appdetails",
        params={"appids": app_id, "cc": "jp", "l": "japanese"},
    )
    time.sleep(STEAM_SLEEP)
    if response is None:
        return None
    try:
        payload = response.json().get(str(app_id), {})
    except ValueError:
        return None
    if not payload.get("success"):
        LOG.warning("appdetails unsuccessful for %s", app_id)
        return None
    return payload.get("data") or None


def fetch_reviews(session: requests.Session, app_id: int, language: str, limit: int = 20) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    response = http_get(
        session,
        f"https://store.steampowered.com/appreviews/{app_id}",
        params={
            "json": 1,
            "language": language,
            "filter": "all",
            "purchase_type": "all",
            "num_per_page": min(limit, 100),
            "cursor": "*",
        },
    )
    time.sleep(STEAM_SLEEP)
    if response is None:
        return {}, []
    try:
        payload = response.json()
    except ValueError:
        return {}, []
    summary = payload.get("query_summary") or {}
    reviews = []
    for item in payload.get("reviews") or []:
        text = strip_html(item.get("review") or "")
        if not text:
            continue
        reviews.append(
            {
                "voted_up": bool(item.get("voted_up")),
                "playtime_hours": round((item.get("author") or {}).get("playtime_forever", 0) / 60, 1),
                "text": text[:700],
            }
        )
        if len(reviews) >= limit:
            break
    return summary, reviews


def fetch_ccu(session: requests.Session, app_id: int) -> int:
    response = http_get(
        session,
        "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        params={"appid": app_id},
    )
    time.sleep(0.4)
    if response is None:
        return 0
    try:
        body = response.json().get("response", {})
        if int(body.get("result", 0)) == 1:
            return int(body.get("player_count") or 0)
    except (ValueError, TypeError):
        return 0
    return 0


def is_indie(details: dict[str, Any]) -> bool:
    blobs: list[str] = []
    for key in ("genres", "categories"):
        for row in details.get(key) or []:
            blobs.append(str(row.get("description") or ""))
    text = " ".join(blobs).lower()
    return "indie" in text or "\u30a4\u30f3\u30c7\u30a3\u30fc" in text or "\u30a4\u30f3\u30c7\u30a3" in text


def parse_price(details: dict[str, Any]) -> tuple[str, int, bool]:
    if details.get("is_free"):
        return "Free", 0, True
    overview = details.get("price_overview") or {}
    formatted = overview.get("final_formatted") or overview.get("initial_formatted") or "N/A"
    discount = int(overview.get("discount_percent") or 0)
    return formatted, discount, False


def has_japanese_support(details: dict[str, Any]) -> bool:
    langs = strip_html(details.get("supported_languages") or "").lower()
    return "japanese" in langs or "\u65e5\u672c\u8a9e" in langs


def map_review_desc(raw: str, positive: float) -> str:
    if raw in REVIEW_SCORE_JA:
        return REVIEW_SCORE_JA[raw]
    if positive >= 95:
        return REVIEW_SCORE_JA["Overwhelmingly Positive"]
    if positive >= 80:
        return REVIEW_SCORE_JA["Very Positive"]
    if positive >= 70:
        return REVIEW_SCORE_JA["Positive"]
    if positive >= 40:
        return REVIEW_SCORE_JA["Mixed"]
    return raw or "N/A"


def normalize_summary(raw: dict[str, Any]) -> dict[str, Any]:
    three = [str(x).strip() for x in (raw.get("three_line_summary") or []) if str(x).strip()]
    swamp = [str(x).strip() for x in (raw.get("swamp_points") or []) if str(x).strip()]
    caveats = [str(x).strip() for x in (raw.get("caveats") or []) if str(x).strip()]
    while len(three) < 3:
        three.append(str(raw.get("excerpt") or "\u8981\u7d04\u3092\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002"))
    return {
        "excerpt": str(raw.get("excerpt") or "")[:180],
        "why_now": str(raw.get("why_now") or three[0]),
        "buzz_story": str(raw.get("buzz_story") or three[1]),
        "three_line_summary": three[:3],
        "swamp_points": swamp[:3] or ["\u73fe\u5728\u306e\u30ec\u30d3\u30e5\u30fc\u304b\u3089\u6e1b\u70b9\u3092\u62bd\u51fa\u4e2d\u3067\u3059\u3002"],
        "caveats": caveats[:2] or ["\u8cbb\u3084\u3059\u6027\u306f\u30d7\u30ec\u30a4\u30b9\u30bf\u30a4\u30eb\u306b\u3088\u3063\u3066\u5272\u308c\u307e\u3059\u3002"],
        "specs_note": str(raw.get("specs_note") or ""),
        "language_note": str(raw.get("language_note") or ""),
    }


def fallback_summary(game: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    name = game["name"]
    pos = game["positive_percent"]
    ccu = game["ccu"]
    disc = game["discount_percent"]
    desc = game.get("short_description") or ""
    why = []
    if disc >= 15:
        why.append(f"{disc}% OFF")
    if ccu >= 200:
        why.append(f"CCU {ccu:,}")
    if pos:
        why.append(f"{pos}% positive")
    why_now = (
        f"{name} "
        + "\u306f\u4eca\u3001"
        + (" / ".join(why) if why else "\u30b9\u30c8\u30a2\u3068\u30b3\u30df\u30e5\u30cb\u30c6\u30a3\u3067\u63d0\u5531\u3055\u308c\u3066\u3044\u307e\u3059")
        + "\u3002"
    )
    buzz = desc[:160] or (
        "Steam\u30ec\u30d3\u30e5\u30fc\u3068\u516c\u958b\u30e1\u30bf\u30c7\u30fc\u30bf\u304b\u3089\u3001"
        "\u73fe\u5728\u306e\u8a71\u984c\u6027\u3092\u6574\u7406\u3057\u3066\u3044\u307e\u3059\u3002"
    )
    liked = [r["text"][:80] for r in reviews if r.get("voted_up")][:2]
    swamp = liked or [
        "\u30b7\u30e7\u30fc\u30c8\u30bb\u30c3\u30b7\u30e7\u30f3\u3067\u3082\u6df1\u3044\u3084\u308a\u8fbc\u307f\u304c\u3042\u308b\u8a2d\u8a08\u3002",
        "\u30a4\u30f3\u30c7\u30a3\u30fc\u306a\u3089\u3067\u306f\u306e\u500b\u6027\u304c\u5f37\u3044\u3002",
    ]
    caveats = [
        "\u73fe\u5728\u306e\u30d0\u30e9\u30f3\u30b9\u3084\u96e3\u6613\u5ea6\u306f\u66f4\u65b0\u3067\u5909\u308f\u308b\u53ef\u80fd\u6027\u304c\u3042\u308a\u307e\u3059\u3002"
    ]
    if not game.get("has_japanese"):
        caveats.append("\u65e5\u672c\u8a9eUI\u304c\u516c\u5f0f\u306b\u78ba\u8a8d\u3067\u304d\u306a\u3044\u305f\u3081\u3001\u8a00\u8a9e\u4f9d\u5b58\u5ea6\u306b\u6ce8\u610f\u3002")
    specs = game.get("pc_requirements") or "\u516c\u5f0f\u30b9\u30c8\u30a2\u306e\u6700\u4f4e\u52d5\u4f5c\u74b0\u5883\u3092\u78ba\u8a8d\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
    language = (
        "\u65e5\u672c\u8a9e\u5bfe\u5fdc\u3042\u308a\u3002"
        if game.get("has_japanese")
        else "\u65e5\u672c\u8a9e\u5bfe\u5fdc\u306f\u4e0d\u660e\u307e\u305f\u306f\u975e\u5bfe\u5fdc\u3002\u82f1\u8a9e\u30ec\u30d3\u30e5\u30fc\u3092\u542b\u3081\u3066\u8981\u7d04\u3057\u3066\u3044\u307e\u3059\u3002"
    )
    return normalize_summary(
        {
            "excerpt": (desc[:90] or why_now) ,
            "why_now": why_now,
            "buzz_story": buzz,
            "three_line_summary": [
                why_now,
                buzz[:120],
                f"{name} / {game.get('review_score_desc')} {pos}%",
            ],
            "swamp_points": swamp[:3],
            "caveats": caveats[:2],
            "specs_note": specs[:240],
            "language_note": language,
        }
    )


def should_resummarize(existing: dict[str, Any] | None, ttl_days: int, total_reviews: int) -> bool:
    if not existing or not (existing.get("summary") or {}).get("excerpt"):
        return True
    stamp = existing.get("summarized_at") or existing.get("updated_at")
    if not stamp:
        return True
    try:
        previous = datetime.fromisoformat(stamp)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=JST)
    except ValueError:
        return True
    if now_jst() - previous >= timedelta(days=ttl_days):
        return True
    old_total = int(existing.get("total_reviews") or 0)
    if old_total and total_reviews > int(old_total * 1.1):
        return True
    return False


def gemini_summarize(game: dict[str, Any], reviews_ja: list[dict[str, Any]], reviews_en: list[dict[str, Any]], model_name: str, api_key: str) -> dict[str, Any] | None:
    try:
        from google import genai
    except ImportError:
        LOG.warning("google-genai is not installed")
        return None

    prompt = {
        "role": "IndieGem editor",
        "instruction": (
            "Summarize this indie Steam game for a Japanese editorial site. "
            "Write EVERY field in natural Japanese. "
            "three_line_summary must contain exactly 3 sentences: "
            "(1) why it is buzzing now, (2) how the buzz started, (3) what the game feels like. "
            "swamp_points: 2-3 addiction hooks. caveats: 1-2 caveats or mixed opinions. "
            "excerpt: about 70-90 Japanese characters for a card teaser. "
            "Do not invent patch notes. Stay faithful to reviews and metadata."
        ),
        "game": {
            "name": game["name"],
            "developers": game["developers"],
            "price": game["price_formatted"],
            "discount_percent": game["discount_percent"],
            "ccu": game["ccu"],
            "ccu_delta": game["ccu_delta"],
            "positive_percent": game["positive_percent"],
            "review_score_desc": game["review_score_desc"],
            "short_description": game.get("short_description"),
            "pc_requirements": game.get("pc_requirements"),
            "supported_languages": game.get("supported_languages"),
            "has_japanese": game.get("has_japanese"),
            "release_date": game.get("release_date"),
        },
        "reviews_japanese": reviews_ja[:12],
        "reviews_english": reviews_en[:12],
    }
    client = genai.Client(api_key=api_key)
    last_error = None
    for attempt in range(6):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=json.dumps(prompt, ensure_ascii=False),
                config={
                    "temperature": 0.4,
                    "response_mime_type": "application/json",
                    "response_json_schema": SUMMARY_SCHEMA,
                    "automatic_function_calling": {"disable": True},
                },
            )
            text = getattr(response, "text", None) or ""
            parsed = json.loads(text)
            return normalize_summary(parsed)
        except Exception as exc:  # noqa: BLE001 - API surface varies by SDK version
            last_error = exc
            LOG.warning("Gemini failed attempt %s: %s", attempt + 1, exc)
            wait = 2 ** attempt + 1
            match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc), flags=re.I)
            if match:
                wait = max(wait, int(float(match.group(1))) + 2)
            LOG.info("sleep %ss before Gemini retry", wait)
            time.sleep(wait)
    LOG.error("Gemini gave up: %s", last_error)
    return None


def assemble_game(
    app_id: int,
    details: dict[str, Any],
    summary_all: dict[str, Any],
    reviews_ja: list[dict[str, Any]],
    ccu: int,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    total = int(summary_all.get("total_reviews") or 0)
    positive = int(summary_all.get("total_positive") or 0)
    percent = round((positive / total) * 100, 1) if total else 0.0
    price_formatted, discount, is_free = parse_price(details)
    developers = list(details.get("developers") or [])
    publishers = list(details.get("publishers") or [])
    langs = strip_html(details.get("supported_languages") or "")
    req = strip_html((details.get("pc_requirements") or {}).get("minimum") if isinstance(details.get("pc_requirements"), dict) else "")
    prev_ccu = int((previous or {}).get("ccu") or 0)
    ccu_delta = (ccu - prev_ccu) if previous else 0
    header = details.get("header_image") or details.get("capsule_image") or ""
    desc_raw = summary_all.get("review_score_desc") or ""
    game = {
        "app_id": app_id,
        "name": details.get("name") or f"App {app_id}",
        "developers": developers,
        "publishers": publishers,
        "header_image": header,
        "short_description": strip_html(details.get("short_description") or ""),
        "price_formatted": "\u7121\u6599" if is_free else price_formatted,
        "discount_percent": discount,
        "is_free": is_free,
        "review_score_desc": map_review_desc(desc_raw, percent),
        "total_reviews": total,
        "total_positive": positive,
        "positive_percent": percent,
        "ccu": ccu,
        "ccu_prev": prev_ccu,
        "ccu_delta": ccu_delta,
        "ccu_display": f"{ccu:,}" + (f" +{ccu_delta:,}" if ccu_delta > 0 else ""),
        "release_date": (details.get("release_date") or {}).get("date") or "",
        "coming_soon": bool((details.get("release_date") or {}).get("coming_soon")),
        "supported_languages": langs,
        "has_japanese": has_japanese_support(details),
        "pc_requirements": req[:400],
        "steam_url": f"https://store.steampowered.com/app/{app_id}/?utm_source=indiegem",
        "updated_at": iso_now(),
        "updated_at_jst": format_jst(),
    }
    return game


def save_games(games: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": iso_now(), "games": games}
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def copy_assets() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    src = TEMPLATES_DIR / "assets"
    if src.exists():
        for file in src.iterdir():
            if file.is_file():
                shutil.copy2(file, ASSETS_DIR / file.name)
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


def render_site(games: list[dict[str, Any]], site_base_url: str) -> None:
    copy_assets()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    ranked = sorted(games, key=lambda g: (g.get("ccu_delta", 0), g.get("ccu", 0)), reverse=True)
    og_image = ranked[0]["header_image"] if ranked else f"{site_base_url}/assets/favicon.svg"
    index_html = env.get_template("index.html").render(
        games=ranked,
        updated_at_jst=format_jst(),
        site_base_url=site_base_url,
        og_image=og_image,
    )
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    post_tpl = env.get_template("post.html")
    for game in ranked:
        canonical = f"{site_base_url}/posts/{game['app_id']}.html"
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Review",
            "name": f"{game['name']} | IndieGem",
            "inLanguage": "ja",
            "dateModified": game["updated_at"],
            "reviewBody": game["summary"]["excerpt"],
            "itemReviewed": {
                "@type": "VideoGame",
                "name": game["name"],
                "url": game["steam_url"],
                "image": game["header_image"],
                "author": {"@type": "Organization", "name": ", ".join(game["developers"]) or "Unknown"},
            },
        }
        html_out = post_tpl.render(
            game=game,
            canonical_url=canonical,
            json_ld=json.dumps(json_ld, ensure_ascii=False),
        )
        (POSTS_DIR / f"{game['app_id']}.html").write_text(html_out, encoding="utf-8")

    sitemap_urls = [f"{site_base_url}/"] + [f"{site_base_url}/posts/{g['app_id']}.html" for g in ranked]
    urlset = "\n".join(f"  <url><loc>{html.escape(u)}</loc></url>" for u in sitemap_urls)
    (DOCS_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urlset}\n</urlset>\n",
        encoding="utf-8",
    )
    robots = f"User-agent: *\nAllow: /\nSitemap: {site_base_url}/sitemap.xml\n"
    (DOCS_DIR / "robots.txt").write_text(robots, encoding="utf-8")
    LOG.info("rendered index + %s posts", len(ranked))


def process_one(
    session: requests.Session,
    app_id: int,
    previous: dict[str, Any] | None,
    gemini_key: str,
    model_name: str,
    ttl_days: int,
    skip_gemini: bool,
) -> dict[str, Any] | None:
    details = fetch_app_details(session, app_id)
    if not details:
        return None
    if details.get("type") not in (None, "game", "Game") and str(details.get("type", "")).lower() != "game":
        if app_id not in SEED_APP_IDS:
            LOG.info("skip non-game %s (%s)", app_id, details.get("type"))
            return None
    summary_all, _ = fetch_reviews(session, app_id, "all", limit=1)
    _, reviews_ja = fetch_reviews(session, app_id, "japanese", limit=15)
    _, reviews_en = fetch_reviews(session, app_id, "english", limit=15)
    ccu = fetch_ccu(session, app_id)
    game = assemble_game(app_id, details, summary_all, reviews_ja, ccu, previous)

    need_ai = should_resummarize(previous, ttl_days, game["total_reviews"])
    summary = None
    used_gemini = False
    if previous and not need_ai:
        summary = previous.get("summary")
        game["summarized_at"] = previous.get("summarized_at") or previous.get("updated_at")
    elif gemini_key and not skip_gemini:
        summary = gemini_summarize(game, reviews_ja, reviews_en, model_name, gemini_key)
        used_gemini = summary is not None
        time.sleep(2.0)
    if not summary:
        summary = fallback_summary(game, reviews_ja + reviews_en)
        game["summarized_at"] = iso_now()
        LOG.info("fallback summary for %s", game["name"])
    elif used_gemini:
        game["summarized_at"] = iso_now()
    game["summary"] = normalize_summary(summary)
    return game


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the IndieGem static catalog")
    parser.add_argument("--seed-only", action="store_true", help="Process only the 4 seed AppIDs")
    parser.add_argument("--skip-gemini", action="store_true", help="Skip Gemini and use fallback summaries")
    parser.add_argument("--max-games", type=int, default=None)
    parser.add_argument("--app-ids", default="", help="Comma-separated extra AppIDs")
    parser.add_argument("--only-app-ids", default="", help="Process only these AppIDs")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    load_dotenv(ROOT / ".env")
    args = parse_args()
    steam_key = os.getenv("STEAM_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
    max_games = args.max_games if args.max_games is not None else env_int("MAX_GAMES", 25)
    ttl_days = env_int("SUMMARY_TTL_DAYS", 7)
    site_base_url = os.getenv("SITE_BASE_URL", "").strip().rstrip("/") or "https://example.github.io/IndieGem"
    extra_ids = [int(x) for x in args.app_ids.split(",") if x.strip().isdigit()]
    only_ids = [int(x) for x in args.only_app_ids.split(",") if x.strip().isdigit()]

    session = build_session()
    existing = load_existing()
    if only_ids:
        candidates = only_ids
    else:
        candidates = discover_app_ids(session, steam_key, extra_ids, args.seed_only)

    collected: list[dict[str, Any]] = []
    processed_ids: set[int] = set()
    for app_id in candidates:
        if len(collected) >= max_games:
            break
        if app_id in processed_ids:
            continue
        processed_ids.add(app_id)
        try:
            details_probe = None
            if app_id not in SEED_APP_IDS:
                details_probe = fetch_app_details(session, app_id)
                if not details_probe or not is_indie(details_probe):
                    LOG.info("skip non-indie %s", app_id)
                    continue
                # Reuse probe by temporarily caching via previous? We'll refetch below; small extra cost.
            game = process_one(
                session,
                app_id,
                existing.get(app_id),
                gemini_key,
                model_name,
                ttl_days,
                args.skip_gemini,
            )
            if game:
                collected.append(game)
                LOG.info("ok %s (%s) ccu=%s pos=%s", game["app_id"], game["name"], game["ccu"], game["positive_percent"])
        except Exception as exc:  # noqa: BLE001
            LOG.exception("failed app %s: %s", app_id, exc)
            continue

    if not collected:
        LOG.error("no games collected")
        return 1

    # Keep previously known games that were not refreshed, so the catalog accumulates.
    collected_ids = {g["app_id"] for g in collected}
    for app_id, old in existing.items():
        if app_id not in collected_ids and old.get("summary"):
            collected.append(old)

    collected.sort(key=lambda g: (g.get("ccu_delta", 0), g.get("ccu", 0), g.get("positive_percent", 0)), reverse=True)
    save_games(collected)
    render_site(collected, site_base_url)
    LOG.info("wrote %s", DATA_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
