from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
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
POSTED_LOG = ROOT / "posted.jsonl"

# 日本標準時。DSTが無いため固定オフセットで足り、tzdata への依存を避けられる。
JST = timezone(timedelta(hours=9), "JST")

MODEL = "claude-opus-5"
CARD_WIDTH = 1080
CARD_HEIGHT = 1350
CARDS_PER_POST = 5
DAYS_PER_BOOK = 5
QUEUE_LOW_THRESHOLD = 3
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
