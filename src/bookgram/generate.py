"""Claude API で1冊分（カルーセル10枚 + キャプション）の原稿を生成する。

根拠データ(material)を唯一の根拠として渡し、そこに無い事実を書かせない。
出力は structured outputs でスキーマを強制する。

カード構成は過去投稿に合わせた10枚:
  1 表紙 / 2 書誌情報 / 3 こんな方におすすめ / 4 問いかけ
  5-8 本文4枚 / 9 まとめ / 10 フォロー導線
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from .bookdata import BookMaterial
from .config import MODEL

MAX_TOKENS = 32000
POINT_SLIDES = 4
RECOMMEND_ITEMS = 3

SYSTEM_PROMPT = """あなたは読書アカウント「Anne（アン）月一冊から始めるビジネス書」の中の人です。
与えられた根拠データだけを根拠に、Instagram のカルーセル投稿とキャプションを書きます。

## 絶対に守るルール

1. 根拠データに書かれていない事実を書かない。
   データに無い人名・数値・年号・章タイトル・エピソードを一切書かない。
2. 本文からの引用を捏造しない。
3. 自分の実体験を創作しない。
   「打ち合わせで苦労した」「前職では」のような、根拠データに無い個人的な経験は書かない。
   ただし読書メモに書かれている所感（「個人的には〜と感じた」等）は、
   本人の記録なのでそのまま自分の言葉として使ってよい。
4. 出版社の内容紹介だけが根拠の記述は、そう分かる書き方をする。

## 文体

- 一人称は「私」。読者に語りかける丁寧語。
- カードの文言は短く言い切る。体言止めや問いかけを使う。
- 誇張表現（「人生が変わる」「衝撃の」「必読」）を使わない。
- カードのテキストは20〜45文字。これを超えると文字が画像からはみ出す。
- カードのテキストには改行を入れ、2〜4行に分ける。
  改行は必ず意味の切れ目に置く。文節の途中で切らない。
  例: 「毎日100%のメンタルで
仕事に向かい続けることは難しい」

## ハイライト

各カードには highlight を1つ指定する。
これは text の中に必ず存在する連続した部分文字列で、色を変えて強調する箇所。
文の要点になる語句を5〜12文字ぶん選ぶ。text 全体をそのまま指定してはいけない。

## キャプション

- 冒頭で「今回は〜」のようにジャンルや切り口に触れる。
- 本の中身のうち、自分が面白いと思った点を2〜3個、地の文で書く。
- 最後は読者への一言で締める。
- 300〜600文字。ハッシュタグや区切り線は含めない（システムが後ろに付ける）。
"""


def _slide_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    f"{description} 全体で20〜45文字。"
                    "意味の切れ目で改行を入れ、2〜4行に分ける。1行は12〜22文字。"
                    "単語や文節の途中で改行しないこと。"
                ),
            },
            "highlight": {
                "type": "string",
                "description": "text に含まれる連続した部分文字列。5〜12文字。",
            },
        },
        "required": ["text", "highlight"],
        "additionalProperties": False,
    }


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "book_title": {
                "type": "string",
                "description": "根拠データの「書名」をそのまま使う。副題は付けない。",
            },
            "book_author": {"type": "string", "description": "著者名"},
            "published": {
                "type": "string",
                "description": "発行日。「2023年5月」の形式。不明なら空文字。",
            },
            "cover": _slide_schema(
                "表紙の見出し。本のテーマを一言で表す。"
                "表紙は幅が狭いので1行10文字以内・2〜3行に必ず収めること。"
            ),
            "recommend": {
                "type": "array",
                "items": _slide_schema("「こんな方におすすめ」の1項目。"),
                "description": f"必ず{RECOMMEND_ITEMS}項目。「〜したい方」の形で揃える。",
            },
            "question": _slide_schema("読者への問いかけ。本が答えようとしている問い。"),
            "points": {
                "type": "array",
                "items": _slide_schema("本の要点を1つ。"),
                "description": f"必ず{POINT_SLIDES}枚。1枚1メッセージ。",
            },
            "summary": _slide_schema("まとめ。この本がどういう一冊かを言い切る。"),
            "caption": {
                "type": "string",
                "description": "キャプション本文。300〜600文字。ハッシュタグを含めない。",
            },
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "この本に固有のハッシュタグを3〜6個。#付き。書名・著者名・ジャンルなど。",
            },
            "grounding": {
                "type": "array",
                "items": {"type": "string"},
                "description": "主要な記述が根拠データのどこに基づくかを1行ずつ記した根拠メモ。",
            },
        },
        "required": [
            "book_title",
            "book_author",
            "published",
            "cover",
            "recommend",
            "question",
            "points",
            "summary",
            "caption",
            "hashtags",
            "grounding",
        ],
        "additionalProperties": False,
    }


def _build_user_prompt(material: BookMaterial) -> str:
    return "\n".join(
        [
            "以下の書籍について、Instagram のカルーセル投稿1本ぶんの原稿を作ってください。",
            "",
            "## 根拠データ（これが唯一の根拠です）",
            "",
            material.to_prompt_block(),
            "",
            "## カード構成（全10枚）",
            "",
            "  1枚目  表紙        cover",
            "  2枚目  書誌情報    （システムが自動生成。原稿不要）",
            f"  3枚目  こんな方におすすめ  recommend（{RECOMMEND_ITEMS}項目）",
            "  4枚目  問いかけ    question",
            f"  5〜8枚目  本文     points（{POINT_SLIDES}枚）",
            "  9枚目  まとめ      summary",
            "  10枚目 フォロー導線（システムが自動生成。原稿不要）",
            "",
            "## grounding について",
            "",
            "主要な記述が根拠データのどの部分に基づくかを列挙してください。",
            "根拠が薄い記述は「一般論（データに根拠なし）」と正直に書いてください。",
        ]
    )


def _validate(payload: dict[str, Any]) -> None:
    if len(payload.get("recommend", [])) != RECOMMEND_ITEMS:
        raise ValueError(
            f"recommend が {RECOMMEND_ITEMS} 項目ではありません: "
            f"{len(payload.get('recommend', []))}"
        )
    if len(payload.get("points", [])) != POINT_SLIDES:
        raise ValueError(
            f"points が {POINT_SLIDES} 枚ではありません: {len(payload.get('points', []))}"
        )
    if not payload.get("hashtags"):
        raise ValueError("hashtags が空です")

    slides = [payload["cover"], payload["question"], payload["summary"]]
    slides += payload["recommend"] + payload["points"]
    for slide in slides:
        # ハイライトは表示上の飾りなので、外れていても投稿は止めず無効化するだけにする
        if slide["highlight"] and slide["highlight"] not in slide["text"]:
            slide["highlight"] = ""


def generate_book_post(material: BookMaterial, api_key: str) -> dict[str, Any]:
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
        messages=[{"role": "user", "content": _build_user_prompt(material)}],
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
