"""Claude API で1冊分（5日 × カルーセル5枚）の原稿を生成する。

書誌データ(material)を唯一の根拠として渡し、そこに無い事実を書かせない。
出力は structured outputs でスキーマを強制する。
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from .bookdata import BookMaterial
from .config import CARDS_PER_POST, DAYS_PER_BOOK, MODEL

MAX_TOKENS = 32000

DAY_THEMES = [
    ("この本、こんな本", "本の概要・どんな人に向くか・基本情報を伝える導入回"),
    ("要点①", "本の中心となる主張を1つ深掘りする"),
    ("要点②", "印象に残る切り口・視点を1つ深掘りする"),
    ("キーワード3選", "本を理解する鍵になる用語や概念を3つ解説する"),
    ("総括", "全体の評価・誰におすすめか・読後に何が変わるか"),
]

SYSTEM_PROMPT = """あなたは読書アカウントの編集者です。与えられた書誌データだけを根拠に、
Instagram のカルーセル投稿原稿を書きます。

## 絶対に守るルール

1. 書誌データに書かれていない事実を書かない。
   具体的には、データに無い人名・地名・数値・年号・章タイトル・エピソードを一切書かない。
2. 本文からの引用を捏造しない。原文の再現が必要な表現は使わない。
3. 内容について述べるときは、出版社の内容紹介に基づく記述であることが分かる書き方をする。
   例:「〜と紹介されています」「〜がテーマとされています」
   自分が読んで確かめたかのような断定（「〜と書かれている」「著者はこう言い切る」）は避ける。
4. データが薄い項目については、無理に埋めず一般論に留める。

## 文体

- 一人称は使わず、読者に語りかける。丁寧語。
- 誇張表現（「人生が変わる」「衝撃の」）を使わない。
- 1枚のカードは1つのメッセージだけ扱う。
- カード本文は日本語で60〜110文字。長いと画像からはみ出す。

## 出力

指定されたJSONスキーマに厳密に従うこと。
"""


def _card_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kicker": {
                "type": "string",
                "description": "カード上部の小見出し。8文字以内。",
            },
            "headline": {
                "type": "string",
                "description": "カードの主見出し。15〜28文字。",
            },
            "body": {
                "type": "string",
                "description": "カード本文。60〜110文字。",
            },
        },
        "required": ["kicker", "headline", "body"],
        "additionalProperties": False,
    }


def _day_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "day_index": {"type": "integer", "description": "1から5"},
            "theme": {"type": "string", "description": "その日のテーマ名"},
            "cards": {
                "type": "array",
                "items": _card_schema(),
                "description": f"必ず{CARDS_PER_POST}枚。1枚目フック、2〜4枚目本文、5枚目まとめ。",
            },
            "caption": {
                "type": "string",
                "description": "Instagram のキャプション本文。200〜400文字。ハッシュタグは含めない。",
            },
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "#付きのハッシュタグを10〜15個。読書系の定番＋ジャンル固有を混ぜる。",
            },
            "grounding": {
                "type": "array",
                "items": {"type": "string"},
                "description": "この日の主要な記述が書誌データのどこに基づくかを1行ずつ記した根拠メモ。",
            },
        },
        "required": ["day_index", "theme", "cards", "caption", "hashtags", "grounding"],
        "additionalProperties": False,
    }


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "book_title": {"type": "string"},
            "book_author": {"type": "string"},
            "one_line": {
                "type": "string",
                "description": "この本を一言で表すコピー。20〜35文字。",
            },
            "days": {
                "type": "array",
                "items": _day_schema(),
                "description": f"必ず{DAYS_PER_BOOK}日分。",
            },
        },
        "required": ["book_title", "book_author", "one_line", "days"],
        "additionalProperties": False,
    }


def _build_user_prompt(material: BookMaterial, notes: str = "") -> str:
    theme_lines = "\n".join(
        f"  Day{i + 1} 【{name}】: {intent}" for i, (name, intent) in enumerate(DAY_THEMES)
    )
    parts = [
        "以下の書籍について、Instagram のカルーセル投稿を "
        f"{DAYS_PER_BOOK} 日分つくってください。",
        "",
        "## 書誌データ（これが唯一の根拠です）",
        "",
        material.to_prompt_block(),
        "",
        "## 各日の構成",
        "",
        theme_lines,
        "",
        f"## 各日のカルーセル（{CARDS_PER_POST}枚）",
        "",
        "  1枚目: フック。読み手が指を止める問いかけや切り口。",
        "  2〜4枚目: 本文。1枚1メッセージ。",
        f"  {CARDS_PER_POST}枚目: まとめ。次の投稿への引きとフォローの誘導。",
        "",
        "## grounding について",
        "",
        "各日ごとに、主要な記述が書誌データのどの部分に基づくかを grounding に列挙してください。",
        "根拠が薄い記述がある場合は「一般論（データに根拠なし）」と正直に書いてください。",
    ]
    if notes:
        parts += ["", "## 補足メモ（あなた自身の読書メモ。書誌データと同等に根拠として扱ってよい）", "", notes]
    return "\n".join(parts)


def _validate(payload: dict[str, Any]) -> None:
    days = payload.get("days", [])
    if len(days) != DAYS_PER_BOOK:
        raise ValueError(f"days が {DAYS_PER_BOOK} 件ではありません: {len(days)} 件")
    for day in days:
        cards = day.get("cards", [])
        if len(cards) != CARDS_PER_POST:
            raise ValueError(
                f"Day{day.get('day_index')} の cards が {CARDS_PER_POST} 枚ではありません: {len(cards)} 枚"
            )
        if not day.get("hashtags"):
            raise ValueError(f"Day{day.get('day_index')} の hashtags が空です")


def generate_book_posts(
    material: BookMaterial, api_key: str, notes: str = ""
) -> dict[str, Any]:
    """1冊分の原稿を生成して dict で返す。"""
    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _output_schema()},
        },
        messages=[{"role": "user", "content": _build_user_prompt(material, notes)}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"生成が拒否されました: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("出力が max_tokens に達しました。MAX_TOKENS を増やしてください。")

    text = next((b.text for b in response.content if b.type == "text"), "")
    payload = json.loads(text)
    _validate(payload)

    payload["_meta"] = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "sources": material.sources,
    }
    return payload
