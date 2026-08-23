"""コマンドライン入口。

  python -m bookgram generate    週次: 在庫が7日分に満たなければ本を消化して下書きを作る
  python -m bookgram post        毎日: その日の下書きを Instagram に投稿する
  python -m bookgram feature     週次: 特集をつくる（月=新刊 / 木=殿堂入り・小説）
  python -m bookgram reel        投稿済みのカードから縦動画を組み立てる
  python -m bookgram post-reel   組み立てた動画をリールとして投稿する
  python -m bookgram rerender    下書きJSONから画像を作り直す（JSON修正後に使う）
  python -m bookgram preview     プレビューページだけ作り直す
  python -m bookgram doctor      認証・トークン・キューの状態を点検する
  python -m bookgram whoami      アクセストークンから IG_USER_ID を調べる
  python -m bookgram fb-whoami   音源ライブラリ用のFacebookトークンを点検する
  python -m bookgram fb-refresh-token  音源用の長期トークンを延長する
  python -m bookgram stats       リーチ・再生数など届き方の数字を読み出す
  python -m bookgram cleanup     古い画像を削除する
  python -m bookgram refresh-token  長期アクセストークンを延長する
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from typing import Any

from . import insights
from . import queue as bookqueue
from .bookdata import fetch_material
from .config import (
    CARDS_PER_POST,
    FEATURE_WEEKDAYS,
    DRAFTS_DIR,
    IMAGE_RETENTION_DAYS,
    IMG_DIR,
    JST,
    OUTPUT_DIR,
    POSTED_LOG,
    QUEUE_LOW_THRESHOLD,
    FACEBOOK_API_HOST,
    feature_kind_for,
    load_secrets,
)
from .generate import generate_book_post
from .feature import SPECS, generate_feature_post, spec_for
from .newbooks import (
    NewBooksUnavailableError,
    diagnose_rakuten,
    fetch_classics,
    fetch_new_business_books,
    fetch_new_novels,
)
from .websearch import enrich_with_web_search
from .preview import (
    load_week_drafts,
    render_index,
    render_pr_body,
    render_week_preview,
)
from .publish import (
    InstagramClient,
    PublishError,
    build_caption,
    exchange_long_lived,
    token_expiry_days,
    publish_carousel,
    publish_reel,
    publish_story,
)
from .music import pick_audio, recent_audio_ids
from .reel import REEL_FILENAME, build_reel, reel_cards
from .render import (
    STORY_FILENAME,
    fetch_cover_data_uri,
    render_feature,
    render_fixed_text_cards,
    render_post,
    render_story,
)

TARGET_COVERAGE_DAYS = 7


def today_jst() -> date:
    return datetime.now(JST).date()


def week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


# --------------------------------------------------------------------------- generate


def cmd_generate(args: argparse.Namespace) -> int:
    secrets = load_secrets(require=("ANTHROPIC_API_KEY",))
    start = date.fromisoformat(args.start) if args.start else today_jst() + timedelta(days=1)

    data = bookqueue.load_queue()
    generated = 0

    target_days = args.days or TARGET_COVERAGE_DAYS
    slots = bookqueue.open_slots(start, target_days, skip_weekdays=FEATURE_WEEKDAYS)
    if not slots:
        print(f"[skip] {start} から{target_days}日間はすべて埋まっています。")
        _write_previews(start, secrets.pages_base_url)
        return 0

    for target in slots:
        if args.max_books and generated >= args.max_books:
            print(f"[stop] --max-books {args.max_books} に達したので終了します。")
            break
        book = bookqueue.take_next_book(data)
        if book is None:
            print("[warn] キューに未生成の本がありません。books/queue.yaml に本を追加してください。")
            break

        title = book["title"]
        print(f"[generate] {target} 『{title}』の根拠データを取得中…")
        material = fetch_material(
            title, book.get("isbn", ""), book.get("notes", ""), strict=False
        )

        if not material.has_substance():
            print("[generate] 書誌DBの根拠が不足。Web検索で補完します…")
            enrich_with_web_search(material, secrets.anthropic_api_key)

        if not material.has_substance():
            print(
                f"[error] 『{title}』の根拠データを集められませんでした"
                f"（ソース: {', '.join(material.sources) or 'なし'}）。"
                " books/queue.yaml の notes にメモを書いてください。",
                file=sys.stderr,
            )
            book["status"] = "needs_input"
            bookqueue.save_queue(data)
            return 1

        print(f"[generate] 原稿を生成中（sources={material.sources}）…")
        try:
            post = generate_book_post(material, secrets.anthropic_api_key)
        except (ValueError, RuntimeError) as error:
            # 1冊の失敗で残り全部を止めないよう、一度だけ作り直して先へ進む
            print(f"[warn] 生成に失敗しました: {error} / 作り直します")
            try:
                post = generate_book_post(material, secrets.anthropic_api_key)
            except (ValueError, RuntimeError) as retry_error:
                print(
                    f"::warning::『{title}』の生成に2回失敗しました: {retry_error}",
                    file=sys.stderr,
                )
                book["status"] = "needs_input"
                bookqueue.save_queue(data)
                continue
        # 発行日はAIに書かせず、書誌データの値をそのまま使う（日付まで入れるため）
        if material.published_date:
            post["published"] = material.published_date
        post["cover_url"] = material.cover_url
        post["cover_data_uri"] = fetch_cover_data_uri(material.cover_url)

        out_dir = IMG_DIR / target.isoformat()
        print(f"[render]   カード{len(render_post(post, out_dir))}枚")
        render_story(post, out_dir)
        print("[render]   ストーリー1枚")
        post.pop("cover_data_uri", None)
        bookqueue.save_draft(
            target,
            {
                "date": target.isoformat(),
                "kind": "book",
                "image_count": CARDS_PER_POST,
                **{k: v for k, v in post.items() if k != "_meta"},
                "status": "draft",
                "meta": post.get("_meta", {}),
            },
        )

        bookqueue.mark_generated(data, book)
        bookqueue.save_queue(data)
        generated += 1

    _write_previews(start, secrets.pages_base_url)

    remaining = len(bookqueue.pending_books(data))
    print(f"[done] 生成: {generated}冊 / キュー残: {remaining}冊（＝{remaining}日分）")
    if remaining < QUEUE_LOW_THRESHOLD:
        print(
            f"::warning::キューの残りが{remaining}冊（{remaining}日分）です。"
            " books/queue.yaml に本を追加してください。"
        )
    return 0


def _write_previews(start: date, pages_base_url: str) -> None:
    days = [start + timedelta(days=i) for i in range(14)]
    drafts = load_week_drafts(days)
    if drafts:
        print(f"[preview] {render_week_preview(week_label(start), drafts)}")
        repo = os.getenv("GITHUB_REPOSITORY", "wyattfuji1220/Instagram")
        branch = os.getenv("DRAFT_BRANCH", f"drafts/{week_label(start)}")
        print(f"[preview] {render_pr_body(week_label(start), drafts, repo, branch)}")
    for path in render_index(pages_base_url):
        print(f"[preview] {path}")


# ------------------------------------------------------------------------------- post


def draft_label(draft: dict[str, Any]) -> str:
    """書籍投稿と新刊特集のどちらでも使える表示名。"""
    if draft.get("kind") == "feature":
        name = draft.get("feature_name") or "特集"
        return f"{name} {draft.get('period_label', '')}"
    return draft.get("book_title", "(無題)")


def cmd_post(args: argparse.Namespace) -> int:
    required = () if args.dry_run else ("IG_USER_ID", "IG_ACCESS_TOKEN")
    secrets = load_secrets(require=required)

    day = date.fromisoformat(args.date) if args.date else today_jst()
    draft = bookqueue.load_draft(day)

    if draft is None:
        print(f"[skip] {day} の下書きがありません。投稿しません。")
        return 0
    if draft.get("status") == "posted":
        print(f"[skip] {day} は投稿済みです (media_id={draft.get('media_id')})。")
        return 0

    image_urls = [
        f"{secrets.pages_base_url}/img/{day.isoformat()}/{i:02d}.jpg"
        for i in range(1, draft.get("image_count", CARDS_PER_POST) + 1)
    ]
    caption = build_caption(draft)

    if args.dry_run:
        print(f"[dry-run] {day} 『{draft_label(draft)}』")
        for url in image_urls:
            print(f"  image: {url}")
        print(
            f"  story: {secrets.pages_base_url}/img/{day.isoformat()}/{STORY_FILENAME}"
        )
        print("--- caption ---")
        print(caption)
        return 0

    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    print(f"[post] {day} 『{draft_label(draft)}』 を投稿中…")
    try:
        media_id = publish_carousel(client, image_urls, caption)
    except PublishError as error:
        print(f"::error::投稿に失敗しました: {error}", file=sys.stderr)
        return 1

    draft["status"] = "posted"
    draft["media_id"] = media_id
    draft["posted_at"] = datetime.now(JST).isoformat()
    bookqueue.save_draft(day, draft)
    _append_log(
        {
            "date": day.isoformat(),
            "media_id": media_id,
            "title": draft_label(draft),
            "kind": draft.get("kind", "book"),
            "posted_at": draft["posted_at"],
        }
    )
    print(f"[done] 投稿しました: media_id={media_id}")

    story_url = f"{secrets.pages_base_url}/img/{day.isoformat()}/{STORY_FILENAME}"
    try:
        story_id = publish_story(client, story_url)
    except PublishError as error:
        # ストーリーはおまけなので、失敗してもフィード投稿は成功扱いにする
        print(f"::warning::ストーリーの投稿に失敗しました: {error}", file=sys.stderr)
    else:
        draft["story_media_id"] = story_id
        bookqueue.save_draft(day, draft)
        print(f"[done] ストーリーも投稿しました: media_id={story_id}")

    days_left = client.token_days_remaining()
    if days_left is not None and days_left <= 14:
        print(f"::warning::アクセストークンの残り有効期間が{days_left}日です。更新してください。")
    return 0


def _append_log(record: dict[str, Any]) -> None:
    with POSTED_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- rerender


def cmd_rerender(args: argparse.Namespace) -> int:
    """下書きJSONから画像を作り直す。JSONを手で修正したあとに使う。"""
    secrets = load_secrets(require=())
    if args.date:
        targets = [date.fromisoformat(args.date)]
    else:
        targets = sorted(
            date.fromisoformat(p.parent.name) for p in DRAFTS_DIR.glob("*/post.json")
        )

    rendered = 0
    for day in targets:
        draft = bookqueue.load_draft(day)
        if draft is None:
            print(f"[skip] {day} の下書きがありません。")
            continue
        if draft.get("status") == "posted" and not args.force:
            print(f"[skip] {day} は投稿済みです。作り直すには --force を付けてください。")
            continue
        post = dict(draft)
        post["cover_data_uri"] = fetch_cover_data_uri(draft.get("cover_url", ""))
        out_dir = IMG_DIR / day.isoformat()
        if args.fixed_only:
            # 特集は別テンプレートで、固定文言を載せていないので対象外
            if draft.get("kind") == "feature":
                continue
            render_fixed_text_cards(post, out_dir)
            render_story(post, out_dir)  # 表紙を埋め込んでいるので連動する
            print(f"[fixed] {day} 『{draft_label(draft)}』")
            rendered += 1
            continue
        if draft.get("kind") == "feature":
            for book in post["books"]:
                book["cover_data_uri"] = fetch_cover_data_uri(book.get("cover_url", ""))
            render_feature(post, out_dir)
        else:
            render_post(post, out_dir)
        render_story(post, out_dir)
        print(f"[render] {day} 『{draft_label(draft)}』")
        rendered += 1

    if rendered:
        _write_previews(min(targets), secrets.pages_base_url)
    print(f"[done] {rendered} 投稿分を再生成しました。")
    return 0


# --------------------------------------------------------------------------- feature


FEATURE_HORIZON_DAYS = 28


def next_feature_day(start: date, *, skip_filled: bool = False) -> date | None:
    """start 以降で特集が割り当たっている最初の日。

    skip_filled=True なら、既に下書きがある日は飛ばす。
    週次のまとめ生成で、空いている特集枠だけを順に埋めるのに使う。
    """
    for offset in range(FEATURE_HORIZON_DAYS):
        day = start + timedelta(days=offset)
        if not feature_kind_for(day):
            continue
        if skip_filled and bookqueue.load_draft(day):
            continue
        return day
    return None


def covered_books() -> tuple[set[str], set[str]]:
    """自分が既に扱った本の ISBN と書名。殿堂入りの重複を防ぐのに使う。"""
    isbns: set[str] = set()
    titles: set[str] = set()

    for book in bookqueue.load_queue()["books"]:
        if book.get("isbn"):
            isbns.add(str(book["isbn"]))
        if book.get("title"):
            titles.add(book["title"])

    for path in DRAFTS_DIR.glob("*/post.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        if draft.get("kind") == "feature":
            for book in draft.get("books", []):
                isbns.add(str(book.get("isbn", "")))
                titles.add(book.get("title", ""))
        elif draft.get("book_title"):
            titles.add(draft["book_title"])

    return isbns - {""}, titles - {""}


def collect_candidates(kind: str, target: date, limit: int):
    """特集の種類に応じて候補を集める。"""
    if kind == "business":
        return fetch_new_business_books(target, limit=limit)
    if kind == "novel":
        return fetch_new_novels(target, limit=limit)
    isbns, titles = covered_books()
    return fetch_classics(exclude_isbns=isbns, exclude_titles=titles, limit=limit)


def cmd_feature(args: argparse.Namespace) -> int:
    """空いている特集枠を、指定本数ぶん埋める。"""
    secrets = load_secrets(require=("ANTHROPIC_API_KEY",))

    if args.date:
        return build_feature(date.fromisoformat(args.date), args, secrets)

    cursor = today_jst() + timedelta(days=1)
    made = 0
    for _ in range(args.ahead):
        target = next_feature_day(cursor, skip_filled=True)
        if target is None:
            break
        if build_feature(target, args, secrets) != 0:
            return 1
        cursor = target + timedelta(days=1)
        made += 1

    if not made:
        print("[skip] 埋めるべき特集枠がありません。")
    return 0


def build_feature(
    target: date, args: argparse.Namespace, secrets: Any
) -> int:
    """特集を1本つくる（ビジネス書の新刊 / 小説の新刊 / 殿堂入り）。"""
    kind = args.kind or feature_kind_for(target) or "business"
    spec = spec_for(kind)

    if bookqueue.load_draft(target) and not args.force:
        print(f"[skip] {target} には既に下書きがあります。上書きするには --force を付けてください。")
        return 0

    print(f"[feature] {target} 向けに「{spec.name}」の候補を収集中…")
    try:
        candidates = collect_candidates(kind, target, args.candidates)
    except NewBooksUnavailableError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    print(f"[feature] 候補 {len(candidates)} 冊から選抜します…")

    post = generate_feature_post(candidates, secrets.anthropic_api_key, target, spec)
    for book in post["books"]:
        book["cover_data_uri"] = fetch_cover_data_uri(book["cover_url"])

    out_dir = IMG_DIR / target.isoformat()
    paths = render_feature(post, out_dir)
    render_story(post, out_dir)
    print(f"[render]   カード{len(paths)}枚 + ストーリー1枚")

    for book in post["books"]:
        book.pop("cover_data_uri", None)
    bookqueue.save_draft(
        target,
        {
            "date": target.isoformat(),
            "image_count": len(paths),
            **{k: v for k, v in post.items() if k != "_meta"},
            "status": "draft",
            "meta": post.get("_meta", {}),
        },
    )

    _write_previews(target, secrets.pages_base_url)
    print(f"[done] {target} の「{spec.name}」を作成しました。")
    return 0


# ------------------------------------------------------------------------------- reel


def _emit_step_output(key: str, value: str) -> None:
    """GitHub Actions のステップ出力に書く。CI 以外では何もしない。

    ステップ間の受け渡しを下書きファイル経由にすると、片方が書けなかった
    ときに後続が黙って別の日を指してしまう。対象日は明示的に渡す。
    """
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}" + chr(10))


def _sweep_orphan_reels() -> None:
    """投稿されないまま残った動画を消す。

    ここに来るのは「投稿待ちの動画は無い」と判断できたときだけ。
    それでも mp4 が残っているなら、投稿に至らなかった残骸なので片付ける。
    残しておくとリポジトリに積み上がる。
    """
    if not IMG_DIR.exists():
        return
    for path in IMG_DIR.glob(f"*/{REEL_FILENAME}"):
        try:
            day = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        draft = bookqueue.load_draft(day)
        if draft and (draft.get("reel") or {}).get("media_id"):
            continue
        path.unlink()
        print(f"[cleanup] 投稿されなかった動画を削除しました: {path.parent.name}")


def _draft_days() -> list[date]:
    days = []
    for path in DRAFTS_DIR.glob("*/post.json"):
        try:
            days.append(date.fromisoformat(path.parent.name))
        except ValueError:
            continue
    return sorted(days, reverse=True)


def pick_reel_source(*, built: bool) -> date | None:
    """リールの元にする投稿日を選ぶ。

    built=False なら、投稿済みでまだ動画にしていない日のうち最も新しい日。
    直後の投稿を動画化したほうが話題が繋がるため新しい順に選ぶ。
    built=True なら、動画はできているがまだリールを出していない日のうち
    最も古い日。作った順に捌く。
    """
    candidates = []
    for day in _draft_days():
        draft = bookqueue.load_draft(day)
        if not draft or draft.get("status") != "posted":
            continue
        reel = draft.get("reel") or {}
        if reel.get("media_id"):
            continue
        if built and not reel.get("built_at"):
            continue
        if not built and reel.get("built_at"):
            continue
        candidates.append(day)
    if not candidates:
        return None
    return min(candidates) if built else max(candidates)


def cmd_reel(args: argparse.Namespace) -> int:
    """投稿済みのカード画像から縦動画を組み立てる。"""
    if not args.date:
        # 未投稿の動画を積み上げない。1本ずつ作って、出してから次を作る。
        waiting = pick_reel_source(built=True)
        if waiting is not None:
            print(f"[skip] {waiting} の動画がまだ投稿待ちです。")
            _emit_step_output("skipped", "true")
            return 0

        _sweep_orphan_reels()

    day = (
        date.fromisoformat(args.date) if args.date else pick_reel_source(built=False)
    )
    if day is None:
        print("[skip] 動画にできる投稿済みの下書きがありません。")
        _emit_step_output("skipped", "true")
        return 0

    draft = bookqueue.load_draft(day)
    if draft is None:
        print(f"[error] {day} の下書きがありません。", file=sys.stderr)
        return 1

    image_dir = IMG_DIR / day.isoformat()
    cards = reel_cards(image_dir, draft.get("kind", "book"))
    if len(cards) < 3:
        print(
            f"[error] {day} のカード画像が足りません（{len(cards)}枚）。"
            " cleanup で消えている可能性があります。",
            file=sys.stderr,
        )
        return 1

    out_path = image_dir / REEL_FILENAME
    print(f"[reel] {day} 『{draft_label(draft)}』 カード{len(cards)}枚を動画にします…")
    build_reel(cards, out_path)
    size_mb = out_path.stat().st_size / 1_000_000

    draft["reel"] = {
        "built_at": datetime.now(JST).isoformat(),
        "cards": len(cards),
        "bytes": out_path.stat().st_size,
    }
    bookqueue.save_draft(day, draft)
    _emit_step_output("date", day.isoformat())
    print(f"[done] {out_path} ({size_mb:.1f}MB)")
    return 0


def audio_client(secrets: Any) -> InstagramClient | None:
    """音源ライブラリを引ける（Facebook ログイン方式の）クライアント。

    資格情報が無ければ None。呼び出し側は音源なしで進める。
    """
    if not (secrets.fb_access_token and secrets.fb_ig_user_id):
        return None
    return InstagramClient(
        secrets.fb_ig_user_id,
        secrets.fb_access_token,
        secrets.graph_api_version,
        FACEBOOK_API_HOST,
    )


def select_audio(
    client: InstagramClient, draft: dict[str, Any], secrets: Any
) -> dict[str, Any] | None:
    """本の雰囲気に合う音源を1つ選ぶ。引けなければ None。"""
    if not (secrets.fb_access_token and secrets.fb_ig_user_id):
        print("[reel] FB_ACCESS_TOKEN が未設定のため音源なしで投稿します。")
        return None

    recent = recent_audio_ids(
        [d for d in (bookqueue.load_draft(x) for x in _draft_days()) if d]
    )

    def search(query: str) -> list[dict[str, Any]]:
        try:
            return client.search_audio(query)
        except PublishError as error:
            # 音源が引けなくてもリール自体は出したい
            print(f"[warn] 音源の検索に失敗しました: {error}", file=sys.stderr)
            return []

    return pick_audio(search, draft, recent)


def cmd_post_reel(args: argparse.Namespace) -> int:
    """組み立て済みの動画をリールとして投稿する。"""
    required = () if args.dry_run else ("IG_USER_ID", "IG_ACCESS_TOKEN")
    secrets = load_secrets(require=required)

    day = date.fromisoformat(args.date) if args.date else pick_reel_source(built=True)
    if day is None:
        print("[skip] 投稿待ちのリールがありません。")
        return 0

    draft = bookqueue.load_draft(day)
    if draft is None:
        print(f"[error] {day} の下書きがありません。", file=sys.stderr)
        return 1

    video_url = f"{secrets.pages_base_url}/img/{day.isoformat()}/{REEL_FILENAME}"
    caption = build_caption(draft)

    # 音源ライブラリは Facebook ログイン方式でしか使えない。資格情報が
    # あればそちらで投稿し、無ければ従来どおり無音で出す。
    client = audio_client(secrets) or InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    track = select_audio(client, draft, secrets)
    configuration = None
    if track:
        print(f"[reel] 音源: {track['title']}／{track.get('display_artist', '')}"
              f"（{track['mood']} / 検索語「{track['query'] or 'トレンド'}」）")
        configuration = {
            "audio_id": track["audio_id"],
            "audio_volume": 100,
            # 動画側は無音なので明示的に切っておく
            "video_volume": 0,
        }

    if args.dry_run:
        print(f"[dry-run] リール {day} 『{draft_label(draft)}』")
        print(f"  video: {video_url}")
        print(f"  audio: {configuration or '（音源なし）'}")
        print("--- caption ---")
        print(caption)
        return 0

    print(f"[reel] {day} 『{draft_label(draft)}』 をリール投稿中…")
    try:
        media_id = publish_reel(client, video_url, caption, configuration)
    except PublishError as error:
        print(f"::error::リールの投稿に失敗しました: {error}", file=sys.stderr)
        return 1

    reel = draft.setdefault("reel", {})
    reel["media_id"] = media_id
    reel["posted_at"] = datetime.now(JST).isoformat()
    if track:
        reel["audio"] = {
            key: track.get(key)
            for key in ("audio_id", "title", "display_artist", "mood", "query")
        }
    bookqueue.save_draft(day, draft)
    _append_log(
        {
            "date": day.isoformat(),
            "media_id": media_id,
            "title": draft_label(draft),
            "kind": "reel",
            "posted_at": reel["posted_at"],
        }
    )
    print(f"[done] リールを投稿しました: media_id={media_id}")

    # 公開が済んだ動画はリポジトリに残さない。画像と違い1本2MB前後あり、
    # 週3本のペースで積み上がると履歴が膨らむ。
    video_path = IMG_DIR / day.isoformat() / REEL_FILENAME
    if video_path.exists():
        video_path.unlink()
        print(f"[cleanup] {video_path.name} を削除しました。")
    return 0


# ---------------------------------------------------------------------------- preview


def cmd_preview(args: argparse.Namespace) -> int:
    secrets = load_secrets(require=())
    start = date.fromisoformat(args.start) if args.start else today_jst()
    _write_previews(start, secrets.pages_base_url)
    return 0


# ----------------------------------------------------------------------------- doctor


def cmd_doctor(_: argparse.Namespace) -> int:
    problems = 0

    # 1つ足りないだけで全部の点検が止まると、原因が1回に1つしか分からない。
    # 個別に見て、可能なところまで進める。
    secrets = load_secrets(require=())
    for name in ("ANTHROPIC_API_KEY", "IG_USER_ID", "IG_ACCESS_TOKEN"):
        if os.getenv(name):
            print(f"[ok] {name} は設定されています。")
        else:
            print(f"[NG] {name} が未設定です。")
            problems += 1

    print(f"[--] IG_API_HOST: {secrets.api_host}")
    if "graph.instagram.com" not in secrets.api_host:
        print(
            "[NG] このアカウントは Instagram ログイン方式です。"
            " IG_API_HOST に https://graph.instagram.com を設定してください。"
        )
        problems += 1

    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    if secrets.ig_user_id and secrets.ig_access_token:
        try:
            info = client.account_info()
            print(
                f"[ok] Instagram アカウント: @{info.get('username')}"
                f" ({info.get('name', '')})"
            )
        except PublishError as error:
            print(f"[NG] Instagram アカウントに接続できません: {error}")
            problems += 1

    # 音源は「引けるかどうか」を実際に叩いて確かめる。設定の有無だけ見ても、
    # 権限やアカウント種別で引けないことがあるため当てにならない。
    if secrets.fb_access_token and secrets.fb_ig_user_id:
        audio = InstagramClient(
            secrets.fb_ig_user_id,
            secrets.fb_access_token,
            secrets.graph_api_version,
            FACEBOOK_API_HOST,
        )
        try:
            tracks = audio.search_audio("")
        except PublishError as error:
            print(f"[NG] 音源ライブラリを引けません: {error}")
            problems += 1
        else:
            if tracks:
                print(f"[ok] 音源ライブラリに接続できました（トレンド{len(tracks)}件）")
            else:
                print("[NG] 音源ライブラリが0件を返しました。リールは音源なしになります。")
                problems += 1

        if secrets.fb_app_id and secrets.fb_app_secret:
            fb_days = token_expiry_days(
                FACEBOOK_API_HOST,
                secrets.graph_api_version,
                secrets.fb_app_id,
                secrets.fb_app_secret,
                secrets.fb_access_token,
            )
            if fb_days is None:
                print("[--] 音源用トークンの有効期限は取得できませんでした。")
            elif fb_days <= 14:
                print(f"[NG] 音源用トークンの残りが{fb_days}日です。fb-refresh-token で延長してください。")
                problems += 1
            else:
                print(f"[ok] 音源用トークンの残り: {fb_days}日")
        else:
            print(
                "[--] FB_APP_ID / FB_APP_SECRET が未設定です。"
                " 音源は使えますが、トークンの残り日数は確認できません。"
            )
    else:
        print(
            "[--] FB_ACCESS_TOKEN / FB_IG_USER_ID が未設定のため、"
            "リールは音源なしで投稿されます。"
        )

    for level, message in diagnose_rakuten():
        print(f"[{level}] {message}")
        if level == "NG":
            problems += 1

    days_left = (
        client.token_days_remaining()
        if secrets.ig_user_id and secrets.ig_access_token
        else None
    )
    if days_left is None:
        print("[--] トークンの有効期限を取得できませんでした（無期限トークンの可能性があります）。")
    elif days_left <= 14:
        print(f"[NG] トークンの残り有効期間が{days_left}日です。更新してください。")
        problems += 1
    else:
        print(f"[ok] トークンの残り有効期間: {days_left}日")

    data = bookqueue.load_queue()
    pending = len(bookqueue.pending_books(data))
    coverage = bookqueue.coverage_days(today_jst())
    print(f"[--] キュー残: {pending}冊 / 投稿在庫: {coverage}日分")
    if pending < QUEUE_LOW_THRESHOLD:
        print(f"[NG] キューの残りが少なすぎます（{QUEUE_LOW_THRESHOLD}冊未満）。")
        problems += 1

    return 1 if problems else 0


# ------------------------------------------------------------------------------ whoami


def cmd_whoami(_: argparse.Namespace) -> int:
    """IG_ACCESS_TOKEN だけを使って IG_USER_ID を調べる（セットアップ用）。"""
    secrets = load_secrets(require=("IG_ACCESS_TOKEN",))
    client = InstagramClient(
        "me", secrets.ig_access_token, secrets.graph_api_version, secrets.api_host
    )
    try:
        info = client.whoami()
    except PublishError as error:
        print(f"[NG] {error}", file=sys.stderr)
        return 1
    user_id = info.get("user_id") or info.get("id", "")
    print(f"username : {info.get('username', '(取得できず)')}")
    print(f"IG_USER_ID: {user_id}")
    print("この IG_USER_ID を GitHub Secrets と .env に設定してください。")
    return 0


# -------------------------------------------------------------------------- fb-whoami


PAGE_FIELDS = "id,name,instagram_business_account{id,username}"


def _facebook_pages(client: InstagramClient) -> list[dict[str, Any]]:
    """このトークンから見える Facebook ページを集める。

    個人に紐づくページは /me/accounts で取れるが、ビジネスポートフォリオ
    配下のページはそこに出てこない。business_management があれば
    ビジネス経由でも辿れるので、両方を見る。
    """

    def fetch(path: str) -> list[dict[str, Any]]:
        try:
            return client._get(path, {"fields": PAGE_FIELDS}).get("data", [])
        except PublishError as error:
            print(f"[--] {path} は参照できません（{error}）")
            return []

    pages = {page["id"]: page for page in fetch("me/accounts")}
    if pages:
        return list(pages.values())

    try:
        businesses = client._get("me/businesses", {"fields": "id,name"}).get("data", [])
    except PublishError as error:
        print(f"[--] ビジネス一覧は参照できません（{error}）")
        return []

    for business in businesses:
        print(f"[--] ビジネス「{business.get('name')}」を確認します")
        for edge in ("owned_pages", "client_pages"):
            for page in fetch(f"{business['id']}/{edge}"):
                pages[page["id"]] = page
    return list(pages.values())


def cmd_fb_whoami(_: argparse.Namespace) -> int:
    """Facebook ログイン方式のトークンから FB_IG_USER_ID を調べる。

    音源ライブラリ(Audio API)を使うための下ごしらえ。付与された権限も
    出すので、足りないものがあればここで分かる。
    """
    secrets = load_secrets(require=("FB_ACCESS_TOKEN",))
    client = InstagramClient(
        "me", secrets.fb_access_token, secrets.graph_api_version, FACEBOOK_API_HOST
    )

    try:
        granted = client._get("me/permissions", {})
    except PublishError as error:
        print(f"[NG] トークンを確認できません: {error}", file=sys.stderr)
        return 1

    scopes = {
        row["permission"]
        for row in granted.get("data", [])
        if row.get("status") == "granted"
    }
    print("付与されている権限: " + (", ".join(sorted(scopes)) or "(なし)"))
    for needed in ("instagram_basic", "instagram_content_publish", "pages_show_list"):
        mark = "ok" if needed in scopes else "NG"
        print(f"[{mark}] {needed}")

    pages = _facebook_pages(client)
    found = False
    for page in pages:
        account = page.get("instagram_business_account") or {}
        label = f"@{account['username']}" if account.get("username") else "(未連携)"
        print(f"[--] ページ「{page.get('name')}」→ Instagram {label}")
        if account.get("id"):
            found = True
            print(f"FB_IG_USER_ID: {account['id']}")
    if not pages:
        print("[NG] このトークンからは Facebook ページが1件も見えません。", file=sys.stderr)

    if not found:
        print(
            "[NG] Instagram と連携済みの Facebook ページが見つかりません。"
            " ページを作成し、Instagram のプロアカウントと連携してください。",
            file=sys.stderr,
        )
        return 1
    print("この FB_IG_USER_ID を GitHub Secrets と .env に設定してください。")
    return 0


# ----------------------------------------------------------------------- refresh-token


def cmd_refresh_token(_: argparse.Namespace) -> int:
    secrets = load_secrets(require=("IG_USER_ID", "IG_ACCESS_TOKEN"))
    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    try:
        result = client.refresh_long_lived_token()
    except PublishError as error:
        print(f"[NG] {error}", file=sys.stderr)
        return 1
    days = int(result.get("expires_in", 0)) // 86400
    print("新しい長期アクセストークン（GitHub Secrets の IG_ACCESS_TOKEN を差し替えてください）:")
    print(result["access_token"])
    print(f"有効期間: 約{days}日")
    return 0


# ------------------------------------------------------------------ fb-refresh-token


def cmd_fb_refresh_token(_: argparse.Namespace) -> int:
    """音源ライブラリ用の長期トークンを延長する（60日ごと）。"""
    secrets = load_secrets(
        require=("FB_APP_ID", "FB_APP_SECRET", "FB_ACCESS_TOKEN")
    )
    try:
        token, days = exchange_long_lived(
            FACEBOOK_API_HOST,
            secrets.graph_api_version,
            secrets.fb_app_id,
            secrets.fb_app_secret,
            secrets.fb_access_token,
        )
    except PublishError as error:
        print(f"[NG] {error}", file=sys.stderr)
        print(
            "トークンが失効している場合は、認証URLからの取り直しが必要です。"
            " SETUP.md を参照してください。",
            file=sys.stderr,
        )
        return 1
    print("新しい長期トークン（GitHub Secrets の FB_ACCESS_TOKEN を差し替えてください）:")
    print(token)
    print(f"有効期間: 約{days}日")
    return 0


# ---------------------------------------------------------------------------- stats


def cmd_stats(args: argparse.Namespace) -> int:
    """届き方の数字を読み出して output/stats.md に残す。

    音源と同じく Facebook ログイン方式のトークンを使う。Instagram ログイン
    方式では insights が引けないため。
    """
    secrets = load_secrets(require=("IG_USER_ID", "IG_ACCESS_TOKEN"))
    # 音源と違い、insights は投稿用トークン（Instagram ログイン方式）で引ける。
    # Facebook ログイン方式だと instagram_manage_insights の追加申請が要る。
    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )

    try:
        profile = client._get(
            client.ig_user_id, {"fields": "username,followers_count,media_count"}
        )
        rows = insights.media_rows(client, limit=args.limit)
        account = insights.account_summary(client, today_jst())
    except PublishError as error:
        print(f"[NG] 読み出せません: {error}", file=sys.stderr)
        return 1

    # 下書きに残したカード枚数から動画の長さを逆算し、維持率を出せるようにする。
    # リールを出した日と素材になった投稿の日はずれるので、media_id で結ぶ。
    durations: dict[str, float] = {}
    for path in sorted(DRAFTS_DIR.glob("*/post.json")):
        reel = (json.loads(path.read_text(encoding="utf-8")).get("reel")) or {}
        if reel.get("media_id") and reel.get("cards"):
            durations[str(reel["media_id"])] = insights.reel_seconds(reel["cards"])

    report = insights.build_report(profile, account, rows, today_jst(), durations)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "stats.md"
    path.write_text(report, encoding="utf-8")
    print(report)

    if not account:
        print(
            "[--] アカウント全体の数字が取れませんでした。"
        )
    print(f"[done] {path}")
    return 0


# ---------------------------------------------------------------------------- cleanup


def cmd_cleanup(_: argparse.Namespace) -> int:
    if not IMG_DIR.exists():
        return 0
    cutoff = today_jst() - timedelta(days=IMAGE_RETENTION_DAYS)
    removed = 0
    for entry in IMG_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if entry_date < cutoff:
            shutil.rmtree(entry)
            removed += 1
    print(f"[cleanup] {removed} 日分の画像を削除しました（{IMAGE_RETENTION_DAYS}日より前）。")
    return 0


# -------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    # Windows のコンソールは既定が cp932 で、絵文字や一部の記号で落ちる。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="bookgram", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="週次の下書き生成")
    p_gen.add_argument("--start", help="生成開始日 (YYYY-MM-DD)。既定は明日。")
    p_gen.add_argument(
        "--max-books", type=int, default=0, help="生成する冊数の上限。0で無制限。"
    )
    p_gen.add_argument(
        "--days", type=int, default=0, help="確保する在庫日数。既定は7日。"
    )
    p_gen.set_defaults(func=cmd_generate)

    p_post = sub.add_parser("post", help="その日の下書きを投稿")
    p_post.add_argument("--date", help="投稿対象日 (YYYY-MM-DD)。既定は本日。")
    p_post.add_argument("--dry-run", action="store_true", help="投稿せず内容を表示する")
    p_post.set_defaults(func=cmd_post)

    p_prev = sub.add_parser("preview", help="プレビューページを作り直す")
    p_prev.add_argument("--start", help="表示開始日 (YYYY-MM-DD)。既定は本日。")
    p_prev.set_defaults(func=cmd_preview)

    p_feat = sub.add_parser("feature", help="特集をつくる")
    p_feat.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定は次の特集日。")
    p_feat.add_argument(
        "--kind",
        choices=sorted(SPECS),
        help="特集の種類。既定は曜日から自動で決まる。",
    )
    p_feat.add_argument(
        "--candidates", type=int, default=20, help="Claudeに渡す候補冊数"
    )
    p_feat.add_argument(
        "--ahead", type=int, default=1, help="まとめて埋める特集の本数。既定は1本。"
    )
    p_feat.add_argument("--force", action="store_true", help="既存の下書きを上書き")
    p_feat.set_defaults(func=cmd_feature)

    p_reel = sub.add_parser("reel", help="投稿済みのカードから縦動画を作る")
    p_reel.add_argument("--date", help="元にする投稿日 (YYYY-MM-DD)。既定は自動選択。")
    p_reel.set_defaults(func=cmd_reel)

    p_preel = sub.add_parser("post-reel", help="作った動画をリールとして投稿")
    p_preel.add_argument("--date", help="元にした投稿日 (YYYY-MM-DD)。既定は自動選択。")
    p_preel.add_argument("--dry-run", action="store_true", help="投稿せず内容を表示する")
    p_preel.set_defaults(func=cmd_post_reel)

    p_re = sub.add_parser("rerender", help="下書きJSONから画像を作り直す")
    p_re.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定は全下書き。")
    p_re.add_argument("--force", action="store_true", help="投稿済みも作り直す")
    p_re.add_argument(
        "--fixed-only",
        action="store_true",
        help="固定文言のカードだけ描き直す（cover_tag・アカウント名を変えたとき）",
    )
    p_re.set_defaults(func=cmd_rerender)

    sub.add_parser("doctor", help="設定と接続の点検").set_defaults(func=cmd_doctor)
    sub.add_parser("whoami", help="トークンから IG_USER_ID を調べる").set_defaults(
        func=cmd_whoami
    )
    sub.add_parser("refresh-token", help="長期トークンを延長する").set_defaults(
        func=cmd_refresh_token
    )
    sub.add_parser(
        "fb-whoami", help="Facebookログイン方式のトークンを点検する"
    ).set_defaults(func=cmd_fb_whoami)
    sub.add_parser(
        "fb-refresh-token", help="音源ライブラリ用の長期トークンを延長する"
    ).set_defaults(func=cmd_fb_refresh_token)
    p_stats = sub.add_parser("stats", help="届き方の数字を読み出す")
    p_stats.add_argument("--limit", type=int, default=12, help="見る投稿数")
    p_stats.set_defaults(func=cmd_stats)

    sub.add_parser("cleanup", help="古い画像を削除").set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
