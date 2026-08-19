"""書誌データベースに根拠が無い本を、Web検索で補完する。

Claude のサーバーサイド Web検索ツールを使い、実際の検索結果を根拠として
本の要約を作らせる。モデルの記憶から書かせるのではなく、検索で得た情報だけを
書かせることで、書誌DBに載っていない本でも捏造を避けられる。

出典URLは material に記録され、レビュー画面の grounding 欄に並ぶ。
"""

from __future__ import annotations

from typing import Any

import anthropic

from .bookdata import BookMaterial
from .config import MODEL

MAX_SEARCHES = 5
MAX_TOKENS = 4000
MAX_CONTINUATIONS = 3

SYSTEM_PROMPT = """あなたは書誌調査の担当者です。指定された本について公開情報を検索し、
その本がどういう本かを事実ベースで要約します。

## 守るルール

1. 検索結果に書かれていたことだけを書く。記憶や推測で補わない。
2. 情報が見つからなかった項目は「情報が見つかりませんでした」と正直に書く。
   それらしい内容をでっち上げてはいけない。
3. 同名の別の本の情報を混ぜない。著者名と出版社が一致することを確認する。
4. 個人ブログの感想と、出版社・書店の公式な内容紹介は区別して書く。

## 出力形式

見出しを付けず、地の文で 400〜800 字にまとめてください。
以下が分かるように書きます。

- どんなテーマを扱った本か
- 中心となる主張や切り口
- 構成（章立てが分かる場合）
- 想定読者
"""


def _extract_text(response: Any) -> str:
    return "\n".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


def _extract_urls(response: Any) -> list[str]:
    urls: list[str] = []
    for block in response.content:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            continue  # エラーオブジェクトのときはリストにならない
        for result in content:
            url = getattr(result, "url", "")
            if url and url not in urls:
                urls.append(url)
    return urls


def research_book(material: BookMaterial, api_key: str) -> tuple[str, list[str]]:
    """本についてWeb検索し、要約テキストと出典URLを返す。"""
    client = anthropic.Anthropic(api_key=api_key)

    known = [f"書名: {material.title}"]
    if material.authors:
        known.append(f"著者: {', '.join(material.authors)}")
    if material.publisher:
        known.append(f"出版社: {material.publisher}")
    if material.isbn:
        known.append(f"ISBN: {material.isbn}")

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "次の本について検索し、どういう本かを要約してください。\n\n"
                + "\n".join(known)
            ),
        }
    ]

    response = None
    for _ in range(MAX_CONTINUATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": MAX_SEARCHES,
                }
            ],
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        # サーバー側ツールが反復上限に達した。会話を差し戻して続行させる。
        messages = messages[:1] + [
            {"role": "assistant", "content": response.content}
        ]

    if response is None or response.stop_reason == "refusal":
        return "", []
    return _extract_text(response), _extract_urls(response)


def enrich_with_web_search(material: BookMaterial, api_key: str) -> bool:
    """material に Web検索の要約を追記する。追記できたら True。"""
    summary, urls = research_book(material, api_key)
    if not summary:
        return False

    material.sources.append("web_search")
    material.web_sources = urls
    if material.description:
        material.description = (
            f"{material.description}\n\n"
            f"【Web検索で得られた補足情報】\n{summary}"
        )
    else:
        material.description = f"【Web検索で得られた情報】\n{summary}"
    return True
