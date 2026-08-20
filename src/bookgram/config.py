from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
QUEUE_FILE = ROOT / "books" / "queue.yaml"
DRAFTS_DIR = ROOT / "drafts"
DOCS_DIR = ROOT / "docs"
IMG_DIR = DOCS_DIR / "img"
OUTPUT_DIR = ROOT / "output"
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
BACKGROUNDS_DIR = ASSETS_DIR / "backgrounds"
# アイコンは拡張子を問わず assets/profile.* を拾う
PROFILE_ICON_STEM = "profile"
ACCOUNT_FILE = ROOT / "account.yaml"
POSTED_LOG = ROOT / "posted.jsonl"

# 日本標準時。DSTが無いため固定オフセットで足り、tzdata への依存を避けられる。
JST = timezone(timedelta(hours=9), "JST")

MODEL = "claude-opus-5"
# Instagram のプロフィールグリッドは 4:5 で表示される。
# 正方形だと左右が切られてグリッドが崩れるため、最初から 4:5 で作る。
CARD_WIDTH = 1080
CARD_HEIGHT = 1350
# ストーリーは 9:16
STORY_WIDTH = 1080
STORY_HEIGHT = 1920
CARDS_PER_POST = 10
DAYS_PER_BOOK = 1
# 1冊1日消費なので、在庫警告はこの日数を基準にする
QUEUE_LOW_THRESHOLD = 14
# 特集の曜日割り（0=月曜）。この曜日には通常の書籍投稿を割り当てない。
# 木曜は殿堂入りと小説を隔週で入れ替え、ひと月に2本ずつ流す。
FEATURE_SCHEDULE: dict[int, tuple[str, ...]] = {
    0: ("business",),
    3: ("classic", "novel"),
}
FEATURE_WEEKDAYS = tuple(FEATURE_SCHEDULE)
# リールを出す曜日（月・水・金）。フィードとは別枠で、過去の投稿を動画化する。
REEL_WEEKDAYS = (0, 2, 4)
IMAGE_RETENTION_DAYS = 45
PAGES_PREVIEW_DIRNAME = "preview"


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str
    ig_user_id: str
    ig_access_token: str
    pages_base_url: str
    graph_api_version: str
    api_host: str


def load_secrets(
    *, require: tuple[str, ...] = ("ANTHROPIC_API_KEY", "IG_USER_ID", "IG_ACCESS_TOKEN")
) -> Secrets:
    """環境変数を読み込む。`require` に挙げた変数が未設定なら例外を投げる。"""
    missing = [name for name in require if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "環境変数が未設定です: " + ", ".join(missing)
            + " / .env または GitHub Secrets を確認してください。"
        )

    return Secrets(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        ig_user_id=os.getenv("IG_USER_ID", ""),
        ig_access_token=os.getenv("IG_ACCESS_TOKEN", ""),
        pages_base_url=os.getenv(
            "PAGES_BASE_URL", "https://wyattfuji1220.github.io/Instagram"
        ).rstrip("/"),
        graph_api_version=os.getenv("GRAPH_API_VERSION", "v23.0"),
        api_host=os.getenv("IG_API_HOST", "https://graph.facebook.com").rstrip("/"),
    )


def load_account() -> dict:
    """account.yaml を読み込む。カードとキャプションの定型部分に使う。"""
    import yaml

    if not ACCOUNT_FILE.exists():
        raise RuntimeError(f"{ACCOUNT_FILE} がありません。")
    return yaml.safe_load(ACCOUNT_FILE.read_text(encoding="utf-8")) or {}


def find_profile_icon() -> Path | None:
    """assets/profile.* を拡張子を問わず探す。見つからなければ None。"""
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = ASSETS_DIR / f"{PROFILE_ICON_STEM}{suffix}"
        if candidate.exists():
            return candidate
    return None


def feature_kind_for(day: date) -> str | None:
    """その日に出す特集の種別。特集の日でなければ None。

    複数種別が割り当たっている曜日は、ISO週番号で順番に切り替える。
    """
    kinds = FEATURE_SCHEDULE.get(day.weekday())
    if not kinds:
        return None
    return kinds[day.isocalendar()[1] % len(kinds)]
