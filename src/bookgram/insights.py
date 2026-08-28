"""投稿がどれだけ届いたかを Instagram から読み出してまとめる。

いいね数だけを見ていると判断を誤る。「いいね0」でも、300回再生されての0と、
5回しか配信されていない0とでは意味が正反対で、打つ手も変わる。

  再生が多くて反応が薄い → 届いてはいる。中身か締めの問題。
  そもそも再生が少ない   → 配信されていない。掴みか形式の問題。

この切り分けに要る数字だけを取る。音源と違い投稿用トークン（Instagram
ログイン方式）でそのまま引ける。Facebook ログイン方式で引こうとすると
instagram_manage_insights の追加申請が要るので、そちらは使わない。

結果は output/stats.md にも書く。GitHub Actions のジョブログは管理者しか
読めないため、リポジトリに残さないと後から確認できない。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .publish import InstagramClient, PublishError
from .reel import SECONDS_PER_CARD

# アカウント全体。期間で集計する指標。
ACCOUNT_METRICS = [
    "reach",
    "views",
    "profile_views",
    "accounts_engaged",
    "total_interactions",
]

# 投稿単位。リールは視聴時間まで取れる。
POST_METRICS = ["views", "reach", "likes", "comments", "saved", "shares"]
REEL_METRICS = POST_METRICS + [
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
]

# Graph API が一度に受け付ける期間の上限
MAX_WINDOW_DAYS = 30


def _unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def account_summary(
    client: InstagramClient, today: date, days: int = MAX_WINDOW_DAYS
) -> dict[str, int]:
    """直近 days 日のアカウント全体の数字。

    until は「その日の 0 時」として解釈されるため、今日を指定すると今日の
    分がまるごと落ちる。翌日 0 時までを窓にする（ここで一度、30日リーチが
    3 と出て実際の 58 と食い違った）。
    """
    days = min(days, MAX_WINDOW_DAYS)
    return client.insights(
        client.ig_user_id,
        ACCOUNT_METRICS,
        period="day",
        metric_type="total_value",
        since=_unix(today - timedelta(days=days - 1)),
        until=_unix(today + timedelta(days=1)),
    )


def is_reel(media: dict[str, Any]) -> bool:
    return media.get("media_product_type") == "REELS"


def media_rows(client: InstagramClient, limit: int = 12) -> list[dict[str, Any]]:
    """直近の投稿に、取れた数字を足して返す。"""
    rows: list[dict[str, Any]] = []
    for media in client.recent_media(limit=limit):
        metrics = REEL_METRICS if is_reel(media) else POST_METRICS
        try:
            stats = client.insights(media["id"], metrics)
        except PublishError:
            stats = {}
        rows.append({**media, "stats": stats})
    return rows


def _seconds(milliseconds: int | None) -> str:
    if not milliseconds:
        return "-"
    return f"{milliseconds / 1000:.1f}秒"


def reel_seconds(record: dict[str, Any]) -> float | None:
    """その動画の長さ。下書きに残した記録から取る。

    実尺が記録されていればそれを使う。枚数からの逆算は、構成を変えた
    あとに過去の動画の長さまで変えてしまう（1枚1.6秒だった頃の8枚を
    2.4秒で数え、維持率が23%から15%に化けた）。逆算は記録が無い分の
    保険にとどめる。
    """
    if record.get("seconds"):
        return float(record["seconds"])
    cards = record.get("cards")
    return cards * SECONDS_PER_CARD if cards else None


def retention(milliseconds: int | None, seconds: float | None) -> str:
    """平均視聴時間を、動画の長さに対する割合にする。

    リールの配信量はこの割合でほぼ決まる。秒数だけ見ても、12秒の動画の
    3秒と30秒の動画の3秒では意味が違うので、必ず割合に直して見る。
    """
    if not milliseconds or not seconds:
        return "-"
    return f"{milliseconds / 1000 / seconds * 100:.0f}%"


def _kind(media: dict[str, Any]) -> str:
    if is_reel(media):
        return "リール"
    return "カルーセル" if media.get("media_type") == "CAROUSEL_ALBUM" else "投稿"


def build_report(
    profile: dict[str, Any],
    account: dict[str, int],
    rows: list[dict[str, Any]],
    today: date,
    durations: dict[str, float] | None = None,
    variants: dict[str, str] | None = None,
) -> str:
    """人が読む形にまとめる。数字が取れていない欄は - にする。

    durations は media_id から動画の秒数への対応。リールを出した日と、
    動画の素材になった投稿の日はずれるので、日付では結べない。
    """
    durations = durations or {}
    variants = variants or {}
    out: list[str] = []
    add = out.append

    add("# 反応の記録")
    add("")
    add(f"作成: {today.isoformat()}")
    add("")
    add(
        f"@{profile.get('username', '?')} / "
        f"フォロワー {profile.get('followers_count', '?')}人 / "
        f"投稿 {profile.get('media_count', '?')}件"
    )
    add("")

    add(f"## アカウント全体（直近{MAX_WINDOW_DAYS}日）")
    add("")
    if account:
        labels = {
            "reach": "リーチ（届いた人数）",
            "views": "表示回数",
            "profile_views": "プロフィール訪問",
            "accounts_engaged": "反応したアカウント数",
            "total_interactions": "反応の総数",
        }
        add("| 指標 | 値 |")
        add("|---|---|")
        for key, label in labels.items():
            if key in account:
                add(f"| {label} | {account[key]:,} |")
    else:
        add("取得できませんでした。")
    add("")

    add("## 投稿ごと")
    add("")
    add("| 日付 | 種別 | 再生/表示 | リーチ | いいね | 保存 | 平均視聴 | 維持率 |")
    add("|---|---|---|---|---|---|---|---|")
    for media in rows:
        stats = media["stats"]
        views = stats.get("views")
        reach = stats.get("reach")
        saved = stats.get("saved")
        watch = stats.get("ig_reels_avg_watch_time")
        add(
            f"| {media.get('timestamp', '')[:10]} "
            f"| {_kind(media)} "
            f"| {views if views is not None else '-'} "
            f"| {reach if reach is not None else '-'} "
            f"| {media.get('like_count', 0)} "
            f"| {saved if saved is not None else '-'} "
            f"| {_seconds(watch)} "
            f"| {retention(watch, durations.get(media.get('id', '')))} |"
        )
    add("")
    add(
        "※ 再生/表示は後から作られた指標のため、古い投稿では 0 と返る。"
        "「見られていない」ではなく「計測されていない」。"
    )
    add("")

    reels = [m for m in rows if is_reel(m)]
    if reels:
        add("## リールだけを見る")
        add("")
        add(
            "リールは非フォロワーにも配信されるため、既存フォロワーの状態に"
            "左右されない。評価軸はいいねではなく視聴時間。"
        )
        add("")
        watched = [
            m["stats"]["ig_reels_avg_watch_time"]
            for m in reels
            if m["stats"].get("ig_reels_avg_watch_time")
        ]
        seen = [m["stats"]["views"] for m in reels if m["stats"].get("views")]
        if seen:
            add(f"- 平均再生数: {sum(seen) / len(seen):.0f}")
        if watched:
            add(f"- 平均視聴時間: {_seconds(int(sum(watched) / len(watched)))}")
        # 構成ごとに分けて見る。全部まとめると、何が効いたのか分からない。
        by_variant: dict[str, list[float]] = {}
        for m in reels:
            watch = m["stats"].get("ig_reels_avg_watch_time")
            seconds = durations.get(m.get("id", ""))
            if not (watch and seconds):
                continue
            name = variants.get(m.get("id", "")) or "（記録なし）"
            by_variant.setdefault(name, []).append(watch / 1000 / seconds * 100)
        if by_variant:
            add("| 構成 | 本数 | 平均維持率 |")
            add("|---|---|---|")
            for name, values in sorted(by_variant.items()):
                add(f"| {name} | {len(values)} | {sum(values) / len(values):.0f}% |")
            add("")
            add(
                "目安として、維持率が5割を超えると配信が伸び始める。"
                "2割台なら、離脱しているのは冒頭の数秒。"
            )
        if not (seen or watched):
            add("- まだ数字が取れていません。")
        add("")

    return "\n".join(out) + "\n"
