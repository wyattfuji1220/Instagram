"""リールに載せる音源を Instagram の音源ライブラリから選ぶ。

Instagram の Audio API を使うと、アプリ内と同じ音源（トレンド曲を含む）を
リールに付けられる。音を動画に焼き込むのと違い、音源ページに紐づくため
そこからの流入が期待できる。

  GET /ig_audio?audio_type=music&user_id=...&search_query=...

制約が2つある。

  - Facebook ログイン方式でしか使えない（Instagram ログインでは 400 になる）
  - 返るのは第三者利用が許諾された曲だけで、アプリ内で見える全曲ではない

本の題材から雰囲気を決め、それに沿った検索語で曲を探す。見つからなければ
検索語なし（＝トレンド）に落とす。直近で使った曲は避ける。
"""

from __future__ import annotations

import hashlib
from typing import Any

DEFAULT_MOOD = "静か"
# 直近で使った曲を避ける本数
AVOID_RECENT = 6

# 本の題材から雰囲気を当てる。原稿本文とハッシュタグを見る。
MOOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "静か": (
        "小説", "エッセイ", "物語", "人生", "生き方", "こころ", "心",
        "家族", "幸せ", "死", "老い", "日常", "旅",
    ),
    "前向き": (
        "習慣", "自己啓発", "仕事術", "時間術", "行動", "挑戦", "キャリア",
        "成長", "健康", "続ける", "やる気", "働き方",
    ),
    "思索的": (
        "思考", "哲学", "論理", "教養", "学び", "読書", "知性", "問い",
        "本質", "科学", "心理", "脳",
    ),
    "力強い": (
        "経営", "戦略", "経済", "組織", "マーケティング", "投資", "会社",
        "リーダー", "交渉", "営業", "起業", "競争",
    ),
}

# 雰囲気ごとの検索語。上から順に試し、当たったところで打ち切る。
# 最後に空文字を置いてあるので、当たらなければトレンドが返る。
MOOD_QUERIES: dict[str, tuple[str, ...]] = {
    "静か": ("calm piano", "lofi chill", "acoustic quiet", ""),
    "前向き": ("uplifting acoustic", "positive morning", "happy light", ""),
    "思索的": ("ambient thoughtful", "minimal piano", "cinematic calm", ""),
    "力強い": ("cinematic inspiring", "corporate motivation", "epic build", ""),
}


def _text_of(draft: dict[str, Any]) -> str:
    """雰囲気を当てるために見る文字列をまとめる。"""
    parts = [
        draft.get("book_title", ""),
        draft.get("caption", ""),
        " ".join(draft.get("hashtags", [])),
    ]
    for key in ("recommend", "points"):
        for item in draft.get(key) or []:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
    for key in ("cover", "question", "summary"):
        value = draft.get(key)
        if isinstance(value, dict):
            parts.append(value.get("text", ""))
    return " ".join(parts)


def mood_for(draft: dict[str, Any]) -> str:
    """本の題材から曲の雰囲気を決める。手掛かりが無ければ既定値。"""
    text = _text_of(draft)
    scores = {
        mood: sum(text.count(word) for word in words)
        for mood, words in MOOD_KEYWORDS.items()
    }
    best = max(scores, key=lambda mood: scores[mood])
    return best if scores[best] else DEFAULT_MOOD


def recent_audio_ids(drafts: list[dict[str, Any]], limit: int = AVOID_RECENT) -> list[str]:
    """直近で使った音源。新しい順に limit 件。"""
    used: list[str] = []
    for draft in drafts:
        audio_id = ((draft.get("reel") or {}).get("audio") or {}).get("audio_id")
        if audio_id and audio_id not in used:
            used.append(audio_id)
        if len(used) >= limit:
            break
    return used


def choose(
    tracks: list[dict[str, Any]], draft: dict[str, Any], recent: list[str]
) -> dict[str, Any] | None:
    """候補から1曲選ぶ。直近で使った曲は避ける。

    同じ本なら何度やり直しても同じ曲になるよう、書名から決める。
    """
    if not tracks:
        return None
    fresh = [t for t in tracks if t.get("audio_id") not in recent] or tracks
    seed = draft.get("book_title") or draft.get("period_label", "")
    offset = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    ordered = sorted(fresh, key=lambda t: str(t.get("audio_id", "")))
    return ordered[offset % len(ordered)]


def pick_audio(
    search, draft: dict[str, Any], recent: list[str] | None = None
) -> dict[str, Any] | None:
    """この投稿に載せる音源を選ぶ。

    search は検索語を受けて候補リストを返す呼び出し可能オブジェクト。
    テストで API を叩かずに差し替えられるよう、外から渡す。
    """
    recent = recent or []
    mood = mood_for(draft)

    for query in MOOD_QUERIES.get(mood, MOOD_QUERIES[DEFAULT_MOOD]):
        tracks = search(query)
        chosen = choose(tracks, draft, recent)
        if chosen:
            chosen = {**chosen, "mood": mood, "query": query}
            return chosen
    return None
