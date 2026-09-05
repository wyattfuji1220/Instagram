"""特集投稿（新刊・殿堂入り）の原稿を生成する。

事実（書名・著者・発売日・レビュー件数・書影）は楽天のデータをそのまま使い、
Claude には「候補からどれを選ぶか」と「紹介文」だけを任せる。
これにより、未読の本について事実を捏造する余地をなくしている。

3種類の特集を FeatureSpec で切り替える。違いは表紙の文言・紹介文の
立ち位置・キャプションの書き出しだけで、生成の流れは共通。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import anthropic

from .config import MODEL, load_account
from .newbooks import NewBook, period_label, period_parts, to_prompt_blocks

MAX_TOKENS = 16000
FEATURE_BOOKS = 4


@dataclass(frozen=True)
class FeatureSpec:
    """特集1種類ぶんの設定。表示文言とプロンプトの差分だけを持つ。"""

    key: str
    name: str
    cover_main: str  # 表紙中央の大きい行
    count_word: str  # 「新刊」「殿堂入り」
    dated: bool  # 表紙に「26年8月後半」を出すか
    cover_top: str  # dated=False のときの上段
    lead: str  # 表紙の斜めの一言
    fact_label: str
    point_label: str
    stance: str  # 未読の本をどう扱うかの指示
    point_hint: str
    caption_opening: str
    story_hint: str
    hashtags: tuple[str, ...]


SPECS: dict[str, FeatureSpec] = {
    "business": FeatureSpec(
        key="business",
        name="ビジネス書 新刊特集",
        cover_main="ビジネス書",
        count_word="新刊",
        dated=True,
        cover_top="",
        lead="楽しみにしている",
        fact_label="発売日",
        point_label="私の注目ポイント",
        stance=(
            "**まだ読んでいない本**なので、読んだ感想を書かない。"
            "「面白かった」ではなく「面白そう」「気になる」という期待の形で書く。"
        ),
        point_hint=(
            "私の注目ポイント。なぜ気になるかが伝わる具体的な一言。"
            "未読なので期待の形で書く。"
        ),
        caption_opening="「◯月前半（後半）の気になる新刊をまとめました。」",
        story_hint="例:「今月の気になる4冊、選びました」",
        hashtags=("#新刊", "#新刊ビジネス書"),
    ),
    "novel": FeatureSpec(
        key="novel",
        name="小説 新刊特集",
        cover_main="小説",
        count_word="新刊",
        dated=True,
        cover_top="",
        lead="読んでみたい",
        fact_label="発売日",
        point_label="私が気になった理由",
        stance=(
            "**まだ読んでいない本**なので、読んだ感想を書かない。"
            "「面白かった」ではなく「読んでみたい」という期待の形で書く。"
            "また、物語の結末や仕掛けには触れない。"
        ),
        point_hint=(
            "私が気になった理由。どんな物語かが伝わる具体的な一言。"
            "未読なので期待の形で書き、結末には触れない。"
        ),
        caption_opening="「◯月前半（後半）に出た小説から、気になる4冊をまとめました。」",
        story_hint="例:「今月の気になる小説、4冊選びました」",
        hashtags=("#小説", "#新刊小説", "#小説好きな人と繋がりたい"),
    ),
    "classic": FeatureSpec(
        key="classic",
        name="殿堂入り書評",
        cover_main="ビジネス書",
        count_word="殿堂入り",
        dated=False,
        cover_top="今も読み継がれる",
        lead="何度も読み返したい",
        fact_label="読者の評価",
        point_label="読み継がれている理由",
        stance=(
            "自分が読んだ体験としては語らない。"
            "内容紹介と読者の評価という事実に基づいて、どんな本かを説明する。"
            "「私は◯◯だと感じた」ではなく「◯◯が書かれた本」と客観的に書く。"
        ),
        point_hint=(
            "この本が読み継がれている理由。何が書かれた本かを具体的に伝える。"
            "レビュー件数の多さそのものではなく、内容紹介から読み取れる中身を書く。"
        ),
        caption_opening="「今も読み継がれているビジネス書を4冊まとめました。」",
        story_hint="例:「何年経っても読まれ続ける4冊」",
        hashtags=("#名著", "#ビジネス書おすすめ"),
    ),
    # 「安い＝内容が薄い」ではない、という切り口。価格という新しい軸で、
    # 買う直前の判断材料になるため保存されやすい。誰も貶めない。
    "bargain": FeatureSpec(
        key="bargain",
        name="1000円以下の名著",
        cover_main="ビジネス書",
        count_word="1000円以下",
        dated=False,
        cover_top="安くても中身は濃い",
        lead="今日から始められる",
        fact_label="価格と評価",
        point_label="この値段で読める理由",
        stance=(
            "自分が読んだ体験としては語らない。"
            "内容紹介・価格・読者の評価という事実に基づいて、どんな本かを説明する。"
            "「安かろう悪かろう」を否定する書き方をするが、高い本を貶めない。"
            "値段の話だけで終わらせず、中身が何なのかを必ず書く。"
        ),
        point_hint=(
            "この値段で読めるのが意外だと思える中身。内容紹介から具体的に書く。"
            "「コスパが良い」のような曖昧な褒め方はしない。何が書かれているかを言う。"
        ),
        caption_opening="「1,000円以下で読める、評価の高いビジネス書を4冊まとめました。」",
        story_hint="例:「1,000円以下とは思えない4冊」",
        hashtags=("#ビジネス書おすすめ", "#コスパ最強"),
    ),
}


def spec_for(key: str) -> FeatureSpec:
    if key not in SPECS:
        raise ValueError(f"未知の特集種別です: {key} / {', '.join(SPECS)}")
    return SPECS[key]


SYSTEM_PROMPT = """あなたは読書アカウント「Anne（アン）月一冊から始めるビジネス書」の中の人です。
与えられた候補から注目の数冊を選び、Instagram の特集投稿を作ります。

## 選ぶ基準

- 一般の読者が読んで面白いと思える本を選ぶ。
- 資格試験の問題集、税務・法務の実務マニュアル、学術の教科書、
  特定業界の専門書は選ばない。
- 内容紹介が具体的で、何が書かれているか分かる本を優先する。
- 同じテーマに偏らないよう、できるだけ切り口を散らす。

## 絶対に守るルール

1. 与えられた内容紹介に書かれていないことを書かない。
   著者の経歴、章立て、数値、エピソードを勝手に足さない。
2. {stance}
3. 誇張表現（「必読」「人生が変わる」「衝撃の」）を使わない。

## 文体

- 一人称は「私」。読者に語りかける丁寧語。
- 紹介文は、なぜその本が気になるかが伝わる具体的な一言にする。
"""


def _output_schema(spec: FeatureSpec, candidate_count: int) -> dict[str, Any]:
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
                                f"{spec.point_hint} 50〜80文字。"
                                "意味の切れ目で改行を入れ2行にする。"
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
                    f"書き出しは{spec.caption_opening}。"
                    "続けて「特に◯冊目の『書名』は」と順番で示してから、"
                    "なぜ気になるのかを書く。"
                    "読者への問いかけやコメントの誘導は書かない"
                    "（投稿時にシステム側で付けるため）。"
                    "ハッシュタグや区切り線は含めない。"
                ),
            },
            "story_line": {
                "type": "string",
                "description": (
                    "ストーリーに載せる一言。20〜32文字。"
                    "この回の特集を一言で表し、思わずフィードを見たくなる文にする。"
                    f"{spec.story_hint}"
                ),
            },
            "grounding": {
                "type": "array",
                "items": {"type": "string"},
                "description": "各紹介文が内容紹介のどの部分に基づくかのメモ。",
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


def _build_user_prompt(spec: FeatureSpec, books: list[NewBook], label: str) -> str:
    top = label if spec.dated else spec.cover_top
    heading = f"{top} {spec.cover_main} {spec.count_word}{FEATURE_BOOKS}選"
    return chr(10).join(
        [
            f"「{heading}」の投稿を作ってください。",
            "",
            f"以下の候補{len(books)}冊から{FEATURE_BOOKS}冊を選び、",
            f"それぞれに「{spec.point_label}」を書いてください。",
            "",
            "## 候補",
            "",
            to_prompt_blocks(books),
        ]
    )


def _cover_parts(spec: FeatureSpec, today: date) -> dict[str, str]:
    """表紙の上段を組み立てる。数字だけ色を変えるため分解して返す。"""
    if spec.dated:
        return period_parts(today)
    return {"year": spec.cover_top, "month": "", "half": ""}


def generate_feature_post(
    books: list[NewBook],
    api_key: str,
    today: date | None = None,
    spec: FeatureSpec | str = "business",
) -> dict[str, Any]:
    """特集1本分の原稿を生成する。"""
    if isinstance(spec, str):
        spec = spec_for(spec)
    today = today or date.today()
    label = period_label(today)
    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT.format(stance=spec.stance),
        output_config={
            "effort": "high",
            "format": {
                "type": "json_schema",
                "schema": _output_schema(spec, len(books)),
            },
        },
        messages=[{"role": "user", "content": _build_user_prompt(spec, books, label)}],
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
                "review_label": book.review_label,
                # カード2つ目の見出しに出す値。特集の種類で中身が変わる。
                # 新刊は発売日、殿堂入りは評価、1000円以下は価格と評価。
                "fact_value": {
                    "classic": book.review_label,
                    "bargain": f"{book.price_label}　{book.review_label}".strip(),
                }.get(spec.key, book.sales_date_label),
                "isbn": book.isbn,
                "cover_url": book.cover_url,
                "point": entry["point"],
            }
        )

    return {
        "kind": "feature",
        "feature_kind": spec.key,
        "feature_name": spec.name,
        "period_label": label,
        "period_parts": _cover_parts(spec, today),
        "cover_main": spec.cover_main,
        "count_word": spec.count_word,
        "fact_label": spec.fact_label,
        "point_label": spec.point_label,
        "cover_lead": (
            load_account().get("feature_lead", spec.lead)
            if spec.key == "business"
            else spec.lead
        ),
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
        "hashtags": list(spec.hashtags),
        "grounding": payload.get("grounding", []),
        "_meta": {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "candidates": len(books),
        },
    }
