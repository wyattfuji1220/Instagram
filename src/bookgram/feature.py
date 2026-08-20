"""週次「ビジネス書 新刊特集」の原稿を生成する。

事実（書名・著者・発売日・書影）は楽天のデータをそのまま使い、
Claude には「候補からどれを選ぶか」と「注目ポイントの文章」だけを任せる。
これにより、未読の本について事実を捏造する余地をなくしている。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import anthropic

from .config import MODEL
from .config import load_account
from .newbooks import (
    NewBook,
    period_label,
    period_parts,
    to_prompt_blocks,
)

MAX_TOKENS = 16000
FEATURE_BOOKS = 4

SYSTEM_PROMPT = """あなたは読書アカウント「Anne（アン）月一冊から始めるビジネス書」の中の人です。
これから発売される（発売されたばかりの）ビジネス書の中から注目の数冊を選び、
Instagram の特集投稿を作ります。

## 選ぶ基準

- 一般のビジネスパーソンが読んで面白いと思える本を選ぶ。
- 資格試験の問題集、税務・法務の実務マニュアル、学術の教科書、
  特定業界の専門書は選ばない。
- 内容紹介が具体的で、何が書かれているか分かる本を優先する。
- 同じテーマに偏らないよう、できるだけ切り口を散らす。

## 絶対に守るルール

1. 与えられた内容紹介に書かれていないことを書かない。
   著者の経歴、章立て、数値、エピソードを勝手に足さない。
2. **まだ読んでいない本**なので、読んだ感想を書かない。
   「面白かった」ではなく「面白そう」「気になる」という期待の形で書く。
3. 誇張表現（「必読」「人生が変わる」「衝撃の」）を使わない。

## 文体

- 一人称は「私」。読者に語りかける丁寧語。
- 注目ポイントは、なぜ気になるかが伝わる具体的な一言にする。
"""


def _output_schema(candidate_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "selected": {
                "type": "array",
                "description": f"必ず{FEATURE_BOOKS}冊。候補の中から選ぶ。",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_number": {
                            "type": "integer",
                            "description": f"候補の番号。1〜{candidate_count}。",
                        },
                        "point": {
                            "type": "string",
                            "description": (
                                "私の注目ポイント。50〜80文字。"
                                "意味の切れ目で改行を入れ2行にする。"
                                "未読なので期待の形で書く。"
                            ),
                        },
                    },
                    "required": ["candidate_number", "point"],
                    "additionalProperties": False,
                },
            },
            "highlight_number": {
                "type": "integer",
                "description": "選んだ中で特に気になる1冊の candidate_number。",
            },
            "caption": {
                "type": "string",
                "description": (
                    "キャプション本文。300〜500文字。"
                    "書き出しは「◯月前半（後半）の気になる新刊をまとめました。」。"
                    "続けて「特に◯冊目の『書名』は」と順番で示してから、"
                    "なぜ楽しみなのかを書く。"
                    "最後は「おすすめな本があればぜひコメントいただけると幸いです！」"
                    "のように、読者にコメントを促す一文で締める。"
                    "ハッシュタグや区切り線は含めない。"
                ),
            },
            "story_line": {
                "type": "string",
                "description": (
                    "ストーリーに載せる一言。20〜32文字。"
                    "この回の特集を一言で表し、思わずフィードを見たくなる文にする。"
                    "例:「今月の気になる4冊、選びました」"
                ),
            },
            "grounding": {
                "type": "array",
                "items": {"type": "string"},
                "description": "各注目ポイントが内容紹介のどの部分に基づくかのメモ。",
            },
        },
        "required": [
            "selected",
            "highlight_number",
            "caption",
            "story_line",
            "grounding",
        ],
        "additionalProperties": False,
    }


def _build_user_prompt(books: list[NewBook], label: str) -> str:
    return "\n".join(
        [
            f"「{label} ビジネス書 新刊{FEATURE_BOOKS}選」の投稿を作ってください。",
            "",
            f"以下の候補{len(books)}冊から{FEATURE_BOOKS}冊を選び、",
            "それぞれに「私の注目ポイント」を書いてください。",
            "",
            "## 候補",
            "",
            to_prompt_blocks(books),
        ]
    )


def generate_feature_post(
    books: list[NewBook], api_key: str, today: date | None = None
) -> dict[str, Any]:
    """新刊特集1本分の原稿を生成する。"""
    today = today or date.today()
    label = period_label(today)
    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _output_schema(len(books))},
        },
        messages=[{"role": "user", "content": _build_user_prompt(books, label)}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"生成が拒否されました: {response.stop_details}")

    text = next((b.text for b in response.content if b.type == "text"), "")
    payload = json.loads(text)

    selected = payload.get("selected", [])
    if len(selected) != FEATURE_BOOKS:
        raise ValueError(f"selected が {FEATURE_BOOKS} 冊ではありません: {len(selected)}")

    picked = []
    for entry in selected:
        index = entry["candidate_number"] - 1
        if not 0 <= index < len(books):
            raise ValueError(f"候補番号が範囲外です: {entry['candidate_number']}")
        book = books[index]
        picked.append(
            {
                "title": book.title,
                "author": book.author,
                "publisher": book.publisher,
                "sales_date": book.sales_date_label,
                "isbn": book.isbn,
                "cover_url": book.cover_url,
                "point": entry["point"],
            }
        )

    return {
        "kind": "feature",
        "period_label": label,
        "period_parts": period_parts(today),
        "cover_lead": load_account().get("feature_lead", "楽しみにしている"),
        "story_line": payload["story_line"],
        "books": picked,
        "highlight_index": next(
            (
                i
                for i, entry in enumerate(selected)
                if entry["candidate_number"] == payload.get("highlight_number")
            ),
            0,
        ),
        "caption": payload["caption"],
        "hashtags": ["#新刊"],
        "grounding": payload.get("grounding", []),
        "_meta": {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "candidates": len(books),
        },
    }
