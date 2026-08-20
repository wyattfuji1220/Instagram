"""コマンドライン入口。

  python -m bookgram generate    週次: 在庫が7日分に満たなければ本を消化して下書きを作る
  python -m bookgram post        毎日: その日の下書きを Instagram に投稿する
  python -m bookgram feature     週次: ビジネス書の新刊特集をつくる（月曜枠）
  python -m bookgram rerender    下書きJSONから画像を作り直す（JSON修正後に使う）
  python -m bookgram preview     プレビューページだけ作り直す
  python -m bookgram doctor      認証・トークン・キューの状態を点検する
  python -m bookgram whoami      アクセストークンから IG_USER_ID を調べる
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

from . import queue as bookqueue
from .bookdata import fetch_material
from .config import (
    CARDS_PER_POST,
    FEATURE_WEEKDAY,
    DRAFTS_DIR,
    IMAGE_RETENTION_DAYS,
    IMG_DIR,
    JST,
    POSTED_LOG,
    QUEUE_LOW_THRESHOLD,
    load_secrets,
)
from .generate import generate_book_post
from .feature import generate_feature_post
from .newbooks import NewBooksUnavailableError, fetch_new_business_books
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
    publish_carousel,
    publish_story,
)
from .render import (
    STORY_FILENAME,
    fetch_cover_data_uri,
    render_feature,
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
    slots = bookqueue.open_slots(start, target_days, skip_weekday=FEATURE_WEEKDAY)
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
        post = generate_book_post(material, secrets.anthropic_api_key)
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
        return f"新刊特集 {draft.get('period_label', '')}"
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


def next_weekday(start: date, weekday: int) -> date:
    """start 以降で最初に該当曜日になる日付。"""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def cmd_feature(args: argparse.Namespace) -> int:
    """ビジネス書の新刊特集を1本つくる。"""
    secrets = load_secrets(require=("ANTHROPIC_API_KEY",))
    target = (
        date.fromisoformat(args.date)
        if args.date
        else next_weekday(today_jst() + timedelta(days=1), FEATURE_WEEKDAY)
    )

    if bookqueue.load_draft(target) and not args.force:
        print(f"[skip] {target} には既に下書きがあります。上書きするには --force を付けてください。")
        return 0

    print(f"[feature] {target} 向けの新刊を収集中…")
    try:
        candidates = fetch_new_business_books(target, limit=args.candidates)
    except NewBooksUnavailableError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    print(f"[feature] 候補 {len(candidates)} 冊から選抜します…")

    post = generate_feature_post(candidates, secrets.anthropic_api_key, target)
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
    print(f"[done] {target} の新刊特集を作成しました。")
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

    try:
        secrets = load_secrets()
        print("[ok] 環境変数はすべて設定されています。")
    except RuntimeError as error:
        print(f"[NG] {error}")
        return 1

    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    try:
        info = client.account_info()
        print(f"[ok] Instagram アカウント: @{info.get('username')} ({info.get('name', '')})")
    except PublishError as error:
        print(f"[NG] Instagram アカウントに接続できません: {error}")
        problems += 1

    days_left = client.token_days_remaining()
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

    p_feat = sub.add_parser("feature", help="ビジネス書の新刊特集をつくる")
    p_feat.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定は次の月曜。")
    p_feat.add_argument(
        "--candidates", type=int, default=20, help="Claudeに渡す候補冊数"
    )
    p_feat.add_argument("--force", action="store_true", help="既存の下書きを上書き")
    p_feat.set_defaults(func=cmd_feature)

    p_re = sub.add_parser("rerender", help="下書きJSONから画像を作り直す")
    p_re.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定は全下書き。")
    p_re.add_argument("--force", action="store_true", help="投稿済みも作り直す")
    p_re.set_defaults(func=cmd_rerender)

    sub.add_parser("doctor", help="設定と接続の点検").set_defaults(func=cmd_doctor)
    sub.add_parser("whoami", help="トークンから IG_USER_ID を調べる").set_defaults(
        func=cmd_whoami
    )
    sub.add_parser("refresh-token", help="長期トークンを延長する").set_defaults(
        func=cmd_refresh_token
    )
    sub.add_parser("cleanup", help="古い画像を削除").set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
