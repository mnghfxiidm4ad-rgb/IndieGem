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
GEMINI_CALL_SLEEP = 7
GEMINI_RETRY_WAITS = (15, 30, 60)
GEMINI_HTTP_TIMEOUT_MS = 45_000
# Steam content_descriptors: 1/3/4 = sexual content / adult-only.
ADULT_DESCRIPTOR_IDS = {1, 3, 4}
ADULT_NAME_RE = re.compile(
    r"hentai|nsfw|\bporn\b|r-18|18\+|adult only|アダルト|エロゲ",
    re.I,
)
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
        "headline": {"type": "string"},
        "target_audience": {"type": "string"},
        "core_mechanics": {"type": "string"},
        "community_reaction": {"type": "string"},
        "verdict": {"type": "string"},
        "article_markdown": {"type": "string"},
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
        "headline",
        "target_audience",
        "core_mechanics",
        "community_reaction",
        "verdict",
        "article_markdown",
    ],
}

GENERATION_CONFIG: dict[str, Any] = {
    "temperature": 0.5,
    "max_output_tokens": 3000,
}

SYSTEM_INSTRUCTION = """あなたはインディーゲームの構造・システム設計に精通したゲームレビュアー・キュレーターです。「IndieGem」の専属ライターとして、Steamの隠れた名作や急上昇タイトルを紹介する日本語記事を執筆します。

### 編集ガイドライン:
1. コアループと手触りの言語化: ストアのあらすじをなぞるだけでなく、「何が面白いのか」「操作感やリソース管理のジレンマ」「コアとなるゲームメカニクス」を具体的に解説してください。
2. 読者ターゲットの明確化: 「誰に刺さるゲームなのか（例: デッキ構築型好き、高難度アクション好き）」を冒頭から提示してください。
3. トーン＆マナー: 落ち着きつつもゲーム愛が伝わる、論理的で明瞭な「です・ます調」。安易な誇大広告表現（「神ゲー確定」「絶対買うべき」等）は避け、長所と人を選ぶポイント（難易度、UI、言語の壁など）を誠実に併記してください。
4. フォーマット: Markdown形式。H2/H3タグ、箇条書きを適切に使い、短時間で要点が把握できる可読性を重視してください。

### 事実の扱い:
- 開発元・パブリッシャー・価格帯・日本語対応・レビュー評価・リリース日は、ユーザーメッセージの [VERIFIED STEAM METADATA] のみを正とします。
- メタデータにないDLC、開発秘話、パッチ内容、受賞歴、架空のエピソードは推測も禁止です。
- プレイヤーレビューは傾向の要約に使い、一件の感想を全体の事実として断定しません。
"""

FACT_RULES = """【ファクトに関する厳格なルール】
- 開発元、パブリッシャー、価格帯、日本語対応状況などは、必ず上記メタデータに厳密に従ってください。
- メタデータにない情報（架空の開発エピソード、実在しないDLC情報など）を推測・捏造することは禁止します。
- 日本語非対応の場合、英語や有志翻訳の敷居（言語依存度）について客観的に言及してください。
- 同時接続数・レビュー件数・好評率を書く場合も、メタデータにある数値以外は使わないでください。
- プレイヤーレビューは「評価の傾向」として要約し、一件の感想を全体の事実にしないでください。
"""

OUTPUT_TEMPLATE_INSTRUCTION = """【出力記事テンプレート】
article_markdown は次の Markdown 構成で書いてください（全体で約1,000〜1,500文字）。

# 【レビュー】[ゲームタイトル]：[ゲームの本質・魅力を表すキャッチコピー]

## 1. 3行でわかる本作の魅力
忙しい人向けの要点を箇条書き3点。誰に刺さるかが分かるようにする。

## 2. ゲームシステムのコア
基本ルール、コアループ、操作感やリソース管理のジレンマ、独自メカニクスを具体的に解説する。

## 3. ここが面白い＆人を選ぶポイント
長所と留意点（難易度、UI、言語の壁など）を両論併記する。H3で「面白いところ」「人を選ぶところ」を分ける。

## 4. プレイヤーの評価とコミュニティの反応
Steamレビュー傾向と、実際に評価されている点・分かれている点を要約する。

## 5. 総評・こんな人におすすめ＆購入ガイド
おすすめのプレイヤー像、日本語対応状況（メタデータ準拠）、ストアリンクへの誘導。

あわせて JSON の各フィールドへ同じ内容を分解して格納すること。
- headline: H1のキャッチコピー部分のみ（【レビュー】とゲームタイトルは含めない）
- target_audience: 刺さるプレイヤー像を1〜2文
- three_line_summary: セクション1の3点
- core_mechanics: セクション2の本文（プレーンテキスト）
- swamp_points: 面白い点 2〜3個
- caveats: 人を選ぶ点 1〜2個
- community_reaction: セクション4の本文
- verdict: セクション5の本文
- excerpt: カード用の70〜90字の日本語ティーザー（誇大表現なし）
- why_now: いま読む価値・注目理由を1文（メタデータのレビュー/同接に基づく）
- buzz_story: メカニクスと手触りの要約1文
- specs_note: 動作環境はメタデータとストア要件の範囲でのみ
- language_note: 日本語対応はメタデータに厳密に従う
個別フィールドは Markdown 記法を使わず、プレーンな日本語で書く。Markdown は article_markdown のみ。
"""

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


class GeminiQuotaError(RuntimeError):
    """Daily Gemini quota is exhausted; later titles should skip the API."""


def classify_gemini_error(exc: Exception) -> str:
    """Return 'daily', 'rpm', or 'other' from a Gemini SDK / HTTP error."""
    text = str(exc)
    low = text.lower()
    compact = re.sub(r"\s+", "", low)
    retry_s = None
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", text, flags=re.I)
    if match:
        retry_s = float(match.group(1))
    daily_tokens = (
        "perday",
        "requestsperday",
        "generaterequestsperday",
        "exceededyourcurrentquota",
        "quotaexceeded",
    )
    if retry_s is not None and retry_s >= 300:
        return "daily"
    if any(token in compact for token in daily_tokens):
        return "daily"
    if "429" in low or "resource_exhausted" in compact or "ratelimit" in compact:
        return "rpm"
    return "other"


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


def has_japanese_text(value: str | None) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", value or ""))


def parse_gemini_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


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


def _add_search_app_ids(session: requests.Session, add, search_filter: str, count: int = 50) -> None:
    search = http_get(
        session,
        "https://store.steampowered.com/search/results/",
        params={
            "query": "",
            "start": 0,
            "count": count,
            "infinite": 1,
            "filter": search_filter,
            "category1": 998,
            "tags": 492,
            "cc": "jp",
            "l": "japanese",
            "hide_filtered_results_explained": 1,
        },
    )
    time.sleep(STEAM_SLEEP)
    if search is None:
        return
    html_blob = ""
    try:
        payload = search.json()
        html_blob = payload.get("results_html") or ""
    except ValueError:
        html_blob = search.text
    found = 0
    for match in re.findall(r'data-ds-appid="(\d+)"', html_blob):
        add(int(match))
        found += 1
    LOG.info("search filter=%s yielded %s app ids", search_filter, found)


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

    # Indie search first so new articles are actual rising/hidden indies,
    # not AAA hits that happen to have an Indie genre tag.
    for search_filter in ("popularnew", "globaltopsellers"):
        _add_search_app_ids(session, add, search_filter)

    featured = http_get(
        session,
        "https://store.steampowered.com/api/featuredcategories/",
        params={"cc": "jp", "l": "japanese"},
    )
    time.sleep(STEAM_SLEEP)
    if featured is not None:
        try:
            body = featured.json()
            for key in ("specials", "new_releases"):
                for item in body.get(key, {}).get("items", []):
                    if int(item.get("type", 0)) == 0:
                        add(int(item["id"]))
        except (ValueError, TypeError, KeyError) as exc:
            LOG.warning("featuredcategories parse failed: %s", exc)

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


def is_coming_soon(details: dict[str, Any]) -> bool:
    return bool((details.get("release_date") or {}).get("coming_soon"))


def recommendation_count(details: dict[str, Any]) -> int:
    recs = details.get("recommendations") or {}
    try:
        return int(recs.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def is_adult_content(details: dict[str, Any]) -> bool:
    descriptors = (details.get("content_descriptors") or {}).get("ids") or []
    for raw in descriptors:
        try:
            if int(raw) in ADULT_DESCRIPTOR_IDS:
                return True
        except (TypeError, ValueError):
            continue
    blobs: list[str] = [str(details.get("name") or "")]
    notes = (details.get("content_descriptors") or {}).get("notes") or ""
    blobs.append(str(notes))
    for key in ("genres", "categories"):
        for row in details.get(key) or []:
            blobs.append(str(row.get("description") or ""))
    text = " ".join(blobs)
    if ADULT_NAME_RE.search(text):
        return True
    lowered = text.lower()
    return "sexual content" in lowered or "nudity" in lowered or "\u30cb\u30e5\u30fc\u30c7\u30a3\u30c6\u30a3" in text


def new_title_skip_reason(app_id: int, details: dict[str, Any]) -> str | None:
    """Return a reason to skip a candidate, or None if it is worth an article."""
    if not is_game_app(details) and app_id not in SEED_APP_IDS:
        return f"non-game ({details.get('type')})"
    if app_id not in SEED_APP_IDS and not is_indie(details):
        return "non-indie"
    if is_coming_soon(details):
        return "coming soon"
    if is_adult_content(details):
        return "adult content"
    recs = recommendation_count(details)
    min_reviews = env_int("MIN_REVIEWS_NEW", 15)
    max_reviews = env_int("MAX_REVIEWS_NEW", 80000)
    # recommendations.total is absent for many small titles; treat 0 as unknown.
    if 0 < recs < min_reviews:
        return f"too few reviews ({recs})"
    if recs > max_reviews:
        return f"already well-known ({recs} reviews)"
    if not has_japanese_support(details) and recs == 0:
        return "no Japanese support and no review signal"
    if not has_japanese_support(details) and 0 < recs < env_int("MIN_REVIEWS_EN_ONLY", 80):
        return "no Japanese support and too few reviews"
    return None


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


def join_names(values: Any, fallback: str = "不明") -> str:
    if isinstance(values, str) and values.strip():
        return values.strip()
    if isinstance(values, (list, tuple)):
        names = [str(item).strip() for item in values if str(item).strip()]
        if names:
            return " / ".join(names)
    return fallback


def extract_tag_list(details: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for key in ("genres", "categories"):
        for row in details.get(key) or []:
            desc = str(row.get("description") or "").strip()
            if not desc:
                continue
            lowered = desc.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tags.append(desc)
    return tags[:16]


def describe_japanese_support(supported_languages: str) -> str:
    text = strip_html(supported_languages or "")
    if not text:
        return "不明（ストアの対応言語欄が空）"
    token = ""
    for part in re.split(r"[,、]", text):
        if "日本語" in part or re.search(r"\bjapanese\b", part, flags=re.I):
            token = part.strip()
            break
    if not token:
        return "非対応（インターフェース / 吹き替え / 字幕いずれも公式非対応）"
    has_audio = "*" in token
    dub = "対応" if has_audio else "非対応"
    return f"インターフェース: 対応 / 吹き替え: {dub} / 字幕: 対応"


def format_review_summary(game: dict[str, Any]) -> str:
    desc = str(game.get("review_score_desc") or "N/A")
    percent = game.get("positive_percent")
    total = int(game.get("total_reviews") or 0)
    if percent not in (None, ""):
        if total:
            return f"{desc} {percent}%（{total:,}件）"
        return f"{desc} {percent}%"
    return desc


def format_price_band(game: dict[str, Any]) -> str:
    if game.get("is_free"):
        return "無料"
    price = str(game.get("price_formatted") or "不明")
    discount = int(game.get("discount_percent") or 0)
    if discount > 0:
        return f"{price}（{discount}% OFF）"
    return price


def build_verified_metadata(game: dict[str, Any]) -> str:
    tags = game.get("tags") or []
    if isinstance(tags, list):
        tags_list = ", ".join(str(tag) for tag in tags if str(tag).strip()) or "不明"
    else:
        tags_list = str(tags).strip() or "不明"
    japanese_support = game.get("japanese_support") or describe_japanese_support(
        str(game.get("supported_languages") or "")
    )
    lines = [
        "[VERIFIED STEAM METADATA]",
        f"- タイトル: {game.get('name') or '不明'}",
        f"- 開発元 / パブリッシャー: {join_names(game.get('developers'))} / {join_names(game.get('publishers'))}",
        f"- リリース日: {game.get('release_date') or '不明'}",
        f"- レビュー状況: {format_review_summary(game)}",
        f"- 主なタグ / ジャンル: {tags_list}",
        f"- 日本語対応: {japanese_support}",
        f"- 価格帯: {format_price_band(game)}",
        f"- ストアURL: {game.get('steam_url') or '不明'}",
    ]
    ccu = game.get("ccu")
    if isinstance(ccu, int):
        lines.append(f"- 同時接続（取得時点）: {ccu:,}")
    delta = game.get("ccu_delta")
    if isinstance(delta, int) and delta != 0:
        lines.append(f"- 同接差分: {delta:+,}")
    return "\n".join(lines)


def format_reviews_for_prompt(reviews: list[dict[str, Any]], limit: int = 10) -> str:
    if not reviews:
        return "（該当レビューなし）"
    lines: list[str] = []
    for index, item in enumerate(reviews[:limit], 1):
        vote = "好評" if item.get("voted_up") else "不評"
        hours = item.get("playtime_hours")
        hours_s = f"{hours}時間" if hours not in (None, "") else "プレイ時間不明"
        text = str(item.get("text") or "").strip()
        if len(text) > 500:
            text = text[:500] + "…"
        lines.append(f"{index}. [{vote} / {hours_s}] {text}")
    return "\n".join(lines)


def build_summarize_prompt(
    game: dict[str, Any],
    reviews_ja: list[dict[str, Any]],
    reviews_en: list[dict[str, Any]],
) -> str:
    desc = str(game.get("short_description") or "").strip() or "（ストア短文なし）"
    req = str(game.get("pc_requirements") or "").strip() or "（記載なし）"
    return f"""IndieGemの日本語レビュー記事を執筆してください。ストア説明の翻訳ではなく、「ゲームメカニクスのコア」「プレイフィールの手触り」「プレイヤーのリアルな評価」「どんなゲーマーに刺さるか」を、客観的かつ熱量のある日本語で書いてください。

---
{build_verified_metadata(game)}

{FACT_RULES}

{OUTPUT_TEMPLATE_INSTRUCTION}

---
[STORE SHORT DESCRIPTION]
{desc}

[PC REQUIREMENTS (VERIFIED SNIPPET)]
{req}

[PLAYER REVIEWS — JAPANESE]
{format_reviews_for_prompt(reviews_ja, 10)}

[PLAYER REVIEWS — ENGLISH]
{format_reviews_for_prompt(reviews_en, 10)}
"""


def build_generation_config(disable_thinking: bool) -> dict[str, Any]:
    config: dict[str, Any] = {
        **GENERATION_CONFIG,
        "system_instruction": SYSTEM_INSTRUCTION,
        "response_mime_type": "application/json",
        "response_json_schema": SUMMARY_SCHEMA,
        "automatic_function_calling": {"disable": True},
    }
    if disable_thinking:
        config["thinking_config"] = {"thinking_budget": 0}
    return config


def _clean_text_list(raw: Any, limit: int) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        items = [raw.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw if str(item).strip()]
    else:
        items = []
    return items[:limit]


def assemble_article_markdown(game: dict[str, Any], summary: dict[str, Any]) -> str:
    title = str(game.get("name") or "本作")
    headline = str(summary.get("headline") or "").strip() or "メカニクスと手触りで選ぶ一作"
    three = [str(item).strip() for item in (summary.get("three_line_summary") or []) if str(item).strip()]
    swamp = [str(item).strip() for item in (summary.get("swamp_points") or []) if str(item).strip()]
    caveats = [str(item).strip() for item in (summary.get("caveats") or []) if str(item).strip()]
    core = str(summary.get("core_mechanics") or summary.get("buzz_story") or "").strip()
    community = str(summary.get("community_reaction") or summary.get("why_now") or "").strip()
    verdict = str(summary.get("verdict") or "").strip()
    audience = str(summary.get("target_audience") or "").strip()
    language = str(summary.get("language_note") or "").strip()
    url = str(game.get("steam_url") or "").strip()

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- 情報を整理中です。"

    parts = [f"# 【レビュー】{title}：{headline}", ""]
    if audience:
        parts.extend([f"**こんな人に刺さります:** {audience}", ""])
    parts.extend(
        [
            "## 1. 3行でわかる本作の魅力",
            bullets(three[:3]),
            "",
            "## 2. ゲームシステムのコア",
            core or "公開情報の範囲で、遊びの骨格を確認してください。",
            "",
            "## 3. ここが面白い＆人を選ぶポイント",
            "### 面白いところ",
            bullets(swamp),
            "",
            "### 人を選ぶところ",
            bullets(caveats),
            "",
            "## 4. プレイヤーの評価とコミュニティの反応",
            community or "Steamレビューの傾向を確認してください。",
            "",
            "## 5. 総評・こんな人におすすめ＆購入ガイド",
            verdict or language or "購入前に公式ストアの最新情報を確認してください。",
        ]
    )
    if language and language not in (verdict or ""):
        parts.extend(["", language])
    if url:
        parts.extend(["", f"ストアページ: {url}"])
    return "\n".join(parts).strip()


def apply_fact_guard(game: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    japanese_support = str(
        game.get("japanese_support")
        or describe_japanese_support(str(game.get("supported_languages") or ""))
    )
    if not game.get("has_japanese"):
        summary["language_note"] = (
            f"公式日本語は非対応です（{japanese_support}）。"
            "英語UIの読解や有志翻訳の要否は、テキスト量・メニュー依存度・ボイス依存度を踏まえて判断してください。"
        )
    elif not str(summary.get("language_note") or "").strip():
        summary["language_note"] = f"日本語対応状況は次のとおりです。{japanese_support}"
    markdown = str(summary.get("article_markdown") or "").strip()
    if len(markdown) < 400:
        summary["article_markdown"] = assemble_article_markdown(game, summary)
    return summary


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


def normalize_summary(
    raw: dict[str, Any],
    game: dict[str, Any] | None = None,
    *,
    enforce_facts: bool = False,
) -> dict[str, Any]:
    three = _clean_text_list(raw.get("three_line_summary"), 3)
    swamp = _clean_text_list(raw.get("swamp_points"), 3)
    caveats = _clean_text_list(raw.get("caveats"), 2)
    excerpt = str(raw.get("excerpt") or "").strip()
    while len(three) < 3:
        three.append(excerpt or "要約を取得できませんでした。")
    headline = str(raw.get("headline") or "").strip()
    headline = re.sub(r"^【レビュー】[^：:]*[：:]", "", headline).strip()
    out = {
        "excerpt": excerpt[:180],
        "why_now": str(raw.get("why_now") or three[0]),
        "buzz_story": str(raw.get("buzz_story") or (three[1] if len(three) > 1 else three[0])),
        "three_line_summary": three[:3],
        "swamp_points": swamp[:3] or ["現在のレビューから沼点を抽出中です。"],
        "caveats": caveats[:2] or ["費やす時間や好みはプレイスタイルによって割れます。"],
        "specs_note": str(raw.get("specs_note") or "").strip(),
        "language_note": str(raw.get("language_note") or "").strip(),
        "headline": headline,
        "target_audience": str(raw.get("target_audience") or "").strip(),
        "core_mechanics": str(raw.get("core_mechanics") or "").strip(),
        "community_reaction": str(raw.get("community_reaction") or "").strip(),
        "verdict": str(raw.get("verdict") or "").strip(),
        "article_markdown": str(raw.get("article_markdown") or "").strip(),
    }
    if game and enforce_facts:
        return apply_fact_guard(game, out)
    return out


def fallback_summary(game: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    name = game["name"]
    pos = game["positive_percent"]
    ccu = game["ccu"]
    disc = game["discount_percent"]
    desc = game.get("short_description") or ""
    tags = game.get("tags") or []
    tag_hint = "、".join(str(tag) for tag in tags[:3] if str(tag).strip())
    why = []
    if disc >= 15:
        why.append(f"{disc}% OFF")
    if ccu >= 200:
        why.append(f"同時接続 {ccu:,}")
    if pos:
        why.append(f"好評率 {pos}%")
    review_s = format_review_summary(game)
    why_now = (
        f"{name} は今、"
        + (" / ".join(why) if why else "ストアとコミュニティで提起されています")
        + "。"
    )
    buzz = desc[:160] or (
        "Steamレビューと公開メタデータから、現在の話題性を整理しています。"
    )
    liked = [r["text"][:80] for r in reviews if r.get("voted_up")][:2]
    swamp = liked or [
        "ショートセッションでも深いやり込みがある設計。",
        "インディーならではの個性が強い。",
    ]
    caveats = [
        "現在のバランスや難易度は更新で変わる可能性があります。"
    ]
    japanese_support = str(
        game.get("japanese_support")
        or describe_japanese_support(str(game.get("supported_languages") or ""))
    )
    if not game.get("has_japanese"):
        caveats.append("公式日本語UIが確認できないため、言語依存度に注意。")
    specs = game.get("pc_requirements") or "公式ストアの最低動作環境を確認してください。"
    if game.get("has_japanese"):
        language = f"日本語対応あり。{japanese_support}"
    else:
        language = (
            f"公式日本語は非対応です（{japanese_support}）。"
            "英語や有志翻訳の敷居は、テキスト量とUI依存度で判断してください。"
        )
    audience = (
        f"{tag_hint}が好きなPCゲーマー"
        if tag_hint
        else "インディーの個性的なメカニクスを探すPCゲーマー"
    )
    core = desc[:400] or "ストア公開情報の範囲では、遊びの骨格は公式ページの説明を参照してください。"
    community = (
        f"Steamのレビュー状況は {review_s} です。"
        "個別の感想ではなく、評価の傾向としてご覧ください。"
    )
    verdict = (
        f"{name} は、{audience} に向く一作です。"
        f"価格帯は {format_price_band(game)}。{language}"
        "購入前にストアページで最新情報を確認してください。"
    )
    payload = {
        "excerpt": (desc[:90] or why_now),
        "why_now": why_now,
        "buzz_story": buzz,
        "three_line_summary": [
            why_now,
            buzz[:120],
            f"{name} / {game.get('review_score_desc')} {pos}%",
        ],
        "swamp_points": swamp[:3],
        "caveats": caveats[:2],
        "specs_note": str(specs)[:240],
        "language_note": language,
        "headline": "公開データから見える遊びの核",
        "target_audience": audience,
        "core_mechanics": core,
        "community_reaction": community,
        "verdict": verdict,
    }
    payload["article_markdown"] = assemble_article_markdown(game, payload)
    return normalize_summary(payload, game, enforce_facts=True)


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

    prompt = build_summarize_prompt(game, reviews_ja, reviews_en)
    models: list[str] = []
    for name in (model_name, "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-latest"):
        if name and name not in models:
            models.append(name)

    client = genai.Client(api_key=api_key, http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS})
    last_error = None
    model_idx = 0
    disable_thinking = True
    for attempt in range(4):
        active_model = models[min(model_idx, len(models) - 1)]
        LOG.info("Gemini wait %ss before request (%s, attempt %s)", GEMINI_CALL_SLEEP, active_model, attempt + 1)
        time.sleep(GEMINI_CALL_SLEEP)
        config = build_generation_config(disable_thinking)
        try:
            response = client.models.generate_content(
                model=active_model,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None) or ""
            if not text.strip():
                raise ValueError("empty Gemini response")
            parsed = parse_gemini_json(text)
            LOG.info("Gemini wait %ss after success", GEMINI_CALL_SLEEP)
            time.sleep(GEMINI_CALL_SLEEP)
            return normalize_summary(parsed, game, enforce_facts=True)
        except Exception as exc:  # noqa: BLE001 - API surface varies by SDK version
            last_error = exc
            kind = classify_gemini_error(exc)
            err_text = str(exc)
            not_found = "404" in err_text or "not found" in err_text.lower()
            thinking_unsupported = "thinking" in err_text.lower() and disable_thinking
            LOG.warning("Gemini failed attempt %s/4 (%s, %s): %s", attempt + 1, kind, active_model, exc)
            if thinking_unsupported:
                disable_thinking = False
                LOG.info("retrying Gemini without thinking_config")
                continue
            if not_found and model_idx + 1 < len(models):
                model_idx += 1
                LOG.info("switching Gemini model to %s", models[model_idx])
                continue
            if attempt >= 3:
                break
            wait = GEMINI_RETRY_WAITS[attempt]
            LOG.info("exponential backoff: sleep %ss before Gemini retry", wait)
            time.sleep(wait)
    LOG.error("Gemini gave up: %s", last_error)
    if last_error and classify_gemini_error(last_error) == "daily":
        raise GeminiQuotaError(str(last_error)) from last_error
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
    tags = extract_tag_list(details)
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
        "tags": tags,
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
        "japanese_support": describe_japanese_support(langs),
        "pc_requirements": req[:400],
        "steam_url": f"https://store.steampowered.com/app/{app_id}/?utm_source=indiegem",
        "updated_at": iso_now(),
        "updated_at_jst": format_jst(),
        "added_at": (previous or {}).get("added_at") or iso_now(),
        "added_at_jst": (previous or {}).get("added_at_jst") or format_jst(),
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
    for name in ("about.html", "privacy.html"):
        src_page = TEMPLATES_DIR / name
        if src_page.exists():
            shutil.copy2(src_page, DOCS_DIR / name)


def render_site(games: list[dict[str, Any]], site_base_url: str) -> None:
    copy_assets()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    ranked = sorted(
        games,
        key=lambda g: g.get("added_at") or g.get("updated_at") or "",
        reverse=True,
    )
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
        if not str(game.get("japanese_support") or "").strip():
            game["japanese_support"] = describe_japanese_support(
                str(game.get("supported_languages") or "")
            )
        canonical = f"{site_base_url}/posts/{game['app_id']}.html"
        headline = str((game.get("summary") or {}).get("headline") or "").strip()
        review_name = f"【レビュー】{game['name']}：{headline}" if headline else f"{game['name']} | IndieGem"
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Review",
            "name": review_name,
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

    sitemap_urls = [
        f"{site_base_url}/",
        f"{site_base_url}/about.html",
        f"{site_base_url}/privacy.html",
    ] + [f"{site_base_url}/posts/{g['app_id']}.html" for g in ranked]
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


def is_game_app(details: dict[str, Any]) -> bool:
    app_type = str(details.get("type") or "").lower()
    return app_type in ("", "game")


def ensure_added_at(game: dict[str, Any]) -> dict[str, Any]:
    if not game.get("added_at"):
        game["added_at"] = game.get("summarized_at") or game.get("updated_at") or iso_now()
    if not game.get("added_at_jst"):
        game["added_at_jst"] = format_jst()
    return game


def refresh_existing_game(session: requests.Session, previous: dict[str, Any]) -> dict[str, Any]:
    """Update Steam metrics only. Keep the existing Japanese summary."""
    app_id = int(previous["app_id"])
    details = fetch_app_details(session, app_id)
    if not details:
        LOG.warning("keep previous metrics for %s; Steam details missing", app_id)
        return ensure_added_at(previous)
    summary_all, _ = fetch_reviews(session, app_id, "all", limit=1)
    ccu = fetch_ccu(session, app_id)
    game = assemble_game(app_id, details, summary_all, [], ccu, previous)
    previous_summary = previous.get("summary") or {}
    if previous_summary.get("excerpt"):
        game["summary"] = normalize_summary(previous_summary, game, enforce_facts=False)
    else:
        game["summary"] = fallback_summary(game, [])
    game["summarized_at"] = previous.get("summarized_at") or previous.get("updated_at") or iso_now()
    game["added_at"] = previous.get("added_at") or previous.get("summarized_at") or previous.get("updated_at") or iso_now()
    game["added_at_jst"] = previous.get("added_at_jst") or format_jst()
    return game


def pick_new_app_ids(
    session: requests.Session,
    candidates: list[int],
    existing_ids: set[int],
    limit: int,
) -> list[int]:
    selected: list[int] = []
    inspected = 0
    max_inspect = env_int("MAX_NEW_INSPECT", 80)
    for app_id in candidates:
        if len(selected) >= limit:
            break
        if inspected >= max_inspect:
            LOG.info("stop scanning new candidates after %s inspections", max_inspect)
            break
        if app_id in existing_ids:
            continue
        details = fetch_app_details(session, app_id)
        inspected += 1
        if not details:
            continue
        reason = new_title_skip_reason(app_id, details)
        if reason:
            LOG.info("skip %s (%s): %s", app_id, details.get("name"), reason)
            continue
        selected.append(app_id)
        LOG.info("queued new title %s (%s)", app_id, details.get("name"))
    return selected


def is_weak_fallback_article(game: dict[str, Any], source: str) -> bool:
    """Fallback copy that is English store text / no reviews looks broken on the site."""
    if source != "fallback":
        return False
    excerpt = str((game.get("summary") or {}).get("excerpt") or "")
    if not has_japanese_text(excerpt):
        return True
    if int(game.get("total_reviews") or 0) < env_int("MIN_REVIEWS_NEW", 15):
        return True
    return False


def ingest_new_game(
    session: requests.Session,
    app_id: int,
    gemini_key: str,
    model_name: str,
    skip_gemini: bool,
    gemini_state: dict[str, Any],
) -> dict[str, Any] | None:
    details = fetch_app_details(session, app_id)
    if not details:
        return None
    skip_reason = new_title_skip_reason(app_id, details)
    if skip_reason and app_id not in SEED_APP_IDS:
        LOG.info("skip ingest %s (%s): %s", app_id, details.get("name"), skip_reason)
        return None
    summary_all, _ = fetch_reviews(session, app_id, "all", limit=1)
    _, reviews_ja = fetch_reviews(session, app_id, "japanese", limit=15)
    _, reviews_en = fetch_reviews(session, app_id, "english", limit=15)
    ccu = fetch_ccu(session, app_id)
    game = assemble_game(app_id, details, summary_all, reviews_ja, ccu, None)
    live_reviews = int(game.get("total_reviews") or 0)
    if live_reviews < env_int("MIN_REVIEWS_NEW", 15) and app_id not in SEED_APP_IDS:
        LOG.info("skip ingest %s (%s): live review count %s", app_id, game["name"], live_reviews)
        return None
    if (not game.get("has_japanese")) and live_reviews < env_int("MIN_REVIEWS_EN_ONLY", 80) and app_id not in SEED_APP_IDS:
        LOG.info("skip ingest %s (%s): English-only with %s reviews", app_id, game["name"], live_reviews)
        return None
    summary = None
    source = "fallback"
    allow_gemini = (
        bool(gemini_key)
        and not skip_gemini
        and not gemini_state.get("blocked")
        and int(gemini_state.get("remaining") or 0) > 0
    )
    if allow_gemini:
        try:
            summary = gemini_summarize(game, reviews_ja, reviews_en, model_name, gemini_key)
            if summary:
                source = "gemini"
                gemini_state["remaining"] = int(gemini_state["remaining"]) - 1
                game["summarized_at"] = iso_now()
                LOG.info("gemini summary for %s; remaining=%s", game["name"], gemini_state["remaining"])
        except GeminiQuotaError as exc:
            LOG.warning("Gemini daily quota reached; remaining new titles use store fallback: %s", exc)
            gemini_state["blocked"] = True
            gemini_state["remaining"] = 0
    if not summary:
        summary = fallback_summary(game, reviews_ja + reviews_en)
        game["summarized_at"] = iso_now()
        LOG.info("fallback summary for %s", game["name"])
    game["summary"] = normalize_summary(summary, game, enforce_facts=True)
    game["summary_source"] = source
    if not skip_gemini and is_weak_fallback_article(game, source):
        LOG.info("skip publishing weak fallback article for %s", game["name"])
        return None
    return game


def trim_catalog(games: list[dict[str, Any]], max_games: int) -> list[dict[str, Any]]:
    if len(games) <= max_games:
        return games
    seeds = [g for g in games if int(g["app_id"]) in SEED_APP_IDS]
    others = [g for g in games if int(g["app_id"]) not in SEED_APP_IDS]
    others.sort(key=lambda g: g.get("added_at") or "", reverse=True)
    room = max(0, max_games - len(seeds))
    kept = seeds + others[:room]
    LOG.info("trimmed catalog %s -> %s (cap %s)", len(games), len(kept), max_games)
    return kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the IndieGem static catalog")
    parser.add_argument("--seed-only", action="store_true", help="Refresh existing titles only; do not add new games")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Rebuild HTML from data/games.json without fetching Steam or Gemini",
    )
    parser.add_argument("--skip-gemini", action="store_true", help="Skip Gemini and use fallback summaries")
    parser.add_argument("--max-games", type=int, default=None, help="Catalog cap (default MAX_GAMES or 100)")
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        help="Max new titles to add this run (default MAX_NEW_GAMES or 3)",
    )
    parser.add_argument(
        "--max-gemini",
        type=int,
        default=None,
        help="Max Gemini API summaries this run (default: same as --max-new)",
    )
    parser.add_argument("--app-ids", default="", help="Comma-separated extra AppIDs to consider as new candidates")
    parser.add_argument("--only-app-ids", default="", help="Add only these AppIDs as new titles this run")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    load_dotenv(ROOT / ".env")
    args = parse_args()
    steam_key = os.getenv("STEAM_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
    max_games = args.max_games if args.max_games is not None else env_int("MAX_GAMES", 100)
    max_new = args.max_new if args.max_new is not None else env_int("MAX_NEW_GAMES", 3)
    max_gemini = args.max_gemini if args.max_gemini is not None else env_int("MAX_GEMINI_PER_RUN", max_new)
    gemini_state = {"remaining": max(0, min(max_gemini, max_new)), "blocked": False}
    LOG.info("limits: catalog_cap=%s max_new=%s max_gemini=%s model=%s", max_games, max_new, gemini_state["remaining"], model_name)
    site_base_url = os.getenv("SITE_BASE_URL", "").strip().rstrip("/") or "https://example.github.io/IndieGem"
    extra_ids = [int(x) for x in args.app_ids.split(",") if x.strip().isdigit()]
    only_ids = [int(x) for x in args.only_app_ids.split(",") if x.strip().isdigit()]

    if args.render_only:
        existing = load_existing()
        collected = [ensure_added_at(game) for game in existing.values()]
        if not collected:
            LOG.error("no games in catalog")
            return 1
        collected = trim_catalog(collected, max_games)
        render_site(collected, site_base_url)
        LOG.info("render-only: wrote %s titles from %s", len(collected), DATA_PATH)
        return 0

    session = build_session()
    existing = load_existing()
    collected: list[dict[str, Any]] = []

    LOG.info("refreshing Steam metrics for %s existing titles (no Gemini)", len(existing))
    for app_id, previous in existing.items():
        if ADULT_NAME_RE.search(str(previous.get("name") or "")):
            LOG.info("drop adult title %s (%s)", app_id, previous.get("name"))
            continue
        try:
            game = refresh_existing_game(session, previous)
            collected.append(ensure_added_at(game))
            LOG.info("refreshed %s (%s) ccu=%s pos=%s", game["app_id"], game["name"], game["ccu"], game["positive_percent"])
        except Exception as exc:  # noqa: BLE001
            LOG.exception("failed refresh %s: %s", app_id, exc)
            collected.append(ensure_added_at(previous))

    dropped_adult = len(existing) - len(collected)
    if dropped_adult:
        LOG.info("removed %s adult titles from catalog", dropped_adult)

    collected_ids = {int(g["app_id"]) for g in collected}
    room = max(0, max_games - len(collected))
    new_limit = min(max_new, room)

    if args.seed_only:
        new_ids: list[int] = []
        LOG.info("seed-only: skip new title discovery")
    elif only_ids:
        new_ids = [app_id for app_id in only_ids if app_id not in collected_ids][:new_limit]
    else:
        candidates = discover_app_ids(session, steam_key, extra_ids, seed_only=False)
        new_ids = pick_new_app_ids(session, candidates, collected_ids, max(new_limit * 8, 12))

    added = 0
    for app_id in new_ids:
        if added >= new_limit or len(collected) >= max_games:
            break
        try:
            game = ingest_new_game(session, app_id, gemini_key, model_name, args.skip_gemini, gemini_state)
            if not game:
                continue
            collected.append(game)
            collected_ids.add(int(game["app_id"]))
            added += 1
            LOG.info("added %s (%s) ccu=%s pos=%s source=%s", game["app_id"], game["name"], game["ccu"], game["positive_percent"], game.get("summary_source"))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("failed new app %s: %s", app_id, exc)
            continue

    if not collected:
        LOG.error("no games collected")
        return 1

    collected = trim_catalog(collected, max_games)
    save_games(collected)
    render_site(collected, site_base_url)
    LOG.info("catalog %s titles (added %s new); wrote %s", len(collected), added, DATA_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
