import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
import requests

from bookgram import bookdata
from bookgram.bookdata import (
    BookMaterial,
    _extract_openbd_texts,
    _ndl_item_fields,
    _normalize_isbn,
    _safe_params,
)
from bookgram.generate import (
    POINT_SLIDES,
    RECOMMEND_ITEMS,
    _build_user_prompt,
    _output_schema,
    _validate,
)
from bookgram.insights import build_report
from bookgram.publish import (
    InstagramClient,
    PublishError,
    build_caption,
    publish_carousel,
)
from bookgram.render import _highlighted, build_card_contexts

# --------------------------------------------------------------------- 書誌データ


def test_normalize_isbn_strips_separators():
    assert _normalize_isbn("978-4-309-22736-8") == "9784309227368"


def test_material_requires_substance():
    assert not BookMaterial(title="薄い本", description="短い").has_substance()
    assert BookMaterial(title="厚い本", description="あ" * 100).has_substance()


def test_notes_alone_can_satisfy_substance():
    """APIが全滅しても、読書メモがあれば生成できる。"""
    material = BookMaterial(title="本", personal_notes="め" * 80)
    assert material.has_substance()
    assert "読書メモ" in material.to_prompt_block()


def test_substance_counts_all_grounding_sources():
    material = BookMaterial(
        title="本",
        description="あ" * 30,
        table_of_contents="い" * 30,
        personal_notes="う" * 30,
    )
    assert material.substance_chars() == 90


def test_official_title_shown_separately_from_display_title():
    material = BookMaterial(
        title="すごい左利き",
        official_title="1万人の脳を見た名医が教えるすごい左利き",
        description="内容",
    )
    block = material.to_prompt_block()
    assert "書名: すごい左利き" in block
    assert "正式書名" in block


def test_web_sources_appear_in_prompt_block():
    material = BookMaterial(title="本", description="要約", web_sources=["https://example.com/x"])
    assert "https://example.com/x" in material.to_prompt_block()


def test_extract_openbd_texts_picks_longest_per_type():
    record = {
        "onix": {
            "CollateralDetail": {
                "TextContent": [
                    {"TextType": "03", "Text": "短い紹介"},
                    {"TextType": "03", "Text": "こちらのほうが長い内容紹介です"},
                    {"TextType": "04", "Text": "第1章 / 第2章"},
                ]
            }
        }
    }
    texts = _extract_openbd_texts(record)
    assert texts["description"] == "こちらのほうが長い内容紹介です"
    assert texts["table_of_contents"] == "第1章 / 第2章"


def test_ndl_item_fields_extracts_isbn_and_normalizes_author():
    import xml.etree.ElementTree as ET

    xml = """<item xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <dc:title>行動経済学が最強の学問である</dc:title>
      <dc:creator>相良, 奈美香</dc:creator>
      <dc:publisher>SBクリエイティブ</dc:publisher>
      <dc:identifier xsi:type="dcndl:ISBN">978-4-8156-1950-3</dc:identifier>
    </item>"""
    fields = _ndl_item_fields(ET.fromstring(xml))
    assert fields["isbn"] == "9784815619503"
    assert fields["authors"] == ["相良奈美香"]


def test_safe_params_masks_credentials():
    masked = _safe_params({"applicationId": "secret-value", "title": "本"})
    assert masked["applicationId"] == "***"
    assert masked["title"] == "本"


def test_request_error_does_not_leak_credentials(monkeypatch):
    class _Resp:
        status_code = 400
        text = ""

    monkeypatch.setattr(bookdata.requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(requests.HTTPError) as excinfo:
        bookdata._request("https://example.com/api", {"applicationId": "SECRET123"})
    assert "SECRET123" not in str(excinfo.value)


# ------------------------------------------------------------------------ 生成


def _slide(text="これはカードに載せる短い文章です", highlight="カード"):
    return {"text": text, "highlight": highlight}


def _fake_post():
    return {
        "book_title": "テスト本",
        "book_author": "著者名",
        "published": "2023年6月",
        "cover": _slide(),
        "recommend": [_slide() for _ in range(RECOMMEND_ITEMS)],
        "question": _slide(),
        "points": [_slide() for _ in range(POINT_SLIDES)],
        "summary": _slide(),
        "caption": "キャプション本文です。",
        "hashtags": ["#心理学"],
        "grounding": ["内容紹介より"],
    }


def test_validate_accepts_well_formed_payload():
    _validate(_fake_post())


def test_validate_rejects_wrong_recommend_count():
    post = _fake_post()
    post["recommend"] = post["recommend"][:2]
    with pytest.raises(ValueError, match="recommend"):
        _validate(post)


def test_validate_rejects_wrong_point_count():
    post = _fake_post()
    post["points"] = post["points"][:2]
    with pytest.raises(ValueError, match="points"):
        _validate(post)


def test_validate_drops_highlight_not_present_in_text():
    post = _fake_post()
    post["cover"] = {"text": "本文", "highlight": "存在しない語"}
    _validate(post)
    assert post["cover"]["highlight"] == ""


def test_output_schema_marks_objects_closed():
    schema = _output_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["points"]["items"]["additionalProperties"] is False


def test_build_user_prompt_omits_empty_sections():
    prompt = _build_user_prompt(BookMaterial(title="本", description="内容紹介"))
    assert "内容紹介" in prompt
    assert "読書メモ" not in prompt


# ------------------------------------------------------------------ レンダリング


def test_highlight_wraps_only_the_target():
    markup = str(_highlighted("あいうえお", "うえ"))
    assert markup == 'あい<span class="hl">うえ</span>お'


def test_highlight_escapes_html():
    assert "&lt;script&gt;" in str(_highlighted("<script>", ""))


def test_highlight_ignores_missing_target():
    assert str(_highlighted("あいうえお", "かき")) == "あいうえお"


def test_build_card_contexts_produces_ten_slides_in_order():
    contexts = build_card_contexts(_fake_post())
    assert [c["variant"] for c in contexts] == [
        "cover",
        "biblio",
        "recommend",
        "text",
        "text",
        "text",
        "text",
        "text",
        "text",
        "outro",
    ]


def test_recommend_slide_carries_three_items():
    contexts = build_card_contexts(_fake_post())
    assert len(contexts[2]["items"]) == RECOMMEND_ITEMS


def test_same_book_gets_stable_backgrounds():
    first = [c["bg"] for c in build_card_contexts(_fake_post())]
    second = [c["bg"] for c in build_card_contexts(_fake_post())]
    assert first == second


# ---------------------------------------------------------------------- 投稿


def test_build_caption_appends_fixed_footer_and_tags():
    caption = build_caption({"caption": "本文です", "hashtags": ["#心理学", "書評"]})
    assert caption.startswith("本文です")
    assert "-------------------------------" in caption
    assert "#読了" in caption      # account.yaml の固定タグ
    assert "#心理学" in caption    # 書籍固有タグ
    assert "#書評" in caption      # # が無いタグも補完される


def test_publish_rejects_single_image():
    with pytest.raises(PublishError, match="2〜10枚"):
        publish_carousel(None, ["https://example.com/1.jpg"], "caption")


def test_open_slots_skips_feature_weekdays(tmp_path, monkeypatch):
    """特集の曜日は通常投稿の割り当て対象から外れる。"""
    from datetime import date

    from bookgram import queue as bookqueue

    monkeypatch.setattr(bookqueue, "DRAFTS_DIR", tmp_path)
    monday = date(2026, 8, 24)
    assert monday.weekday() == 0

    slots = bookqueue.open_slots(date(2026, 8, 21), 7, skip_weekdays=(0, 3))
    assert monday not in slots
    assert date(2026, 8, 27) not in slots  # 木曜も特集枠
    assert len(slots) == 5


def test_open_slots_skips_dates_that_already_have_drafts(tmp_path, monkeypatch):
    from datetime import date

    from bookgram import queue as bookqueue

    monkeypatch.setattr(bookqueue, "DRAFTS_DIR", tmp_path)
    taken = date(2026, 8, 22)
    (tmp_path / taken.isoformat()).mkdir(parents=True)
    (tmp_path / taken.isoformat() / "post.json").write_text("{}", encoding="utf-8")

    slots = bookqueue.open_slots(date(2026, 8, 21), 5)
    assert taken not in slots
    assert len(slots) == 4


def test_open_slots_reaches_far_dates(tmp_path, monkeypatch):
    """探索窓が狭いと途中で打ち切られるバグの回帰テスト。"""
    from datetime import date

    from bookgram import queue as bookqueue

    monkeypatch.setattr(bookqueue, "DRAFTS_DIR", tmp_path)
    slots = bookqueue.open_slots(date(2026, 8, 21), 60, skip_weekdays=(0, 3))
    # 60日から特集の2曜日（週2日）を除いた日数がそのまま取れる
    assert len(slots) == 43


def test_simplify_title_drops_subtitle_and_parenthetical():
    from bookgram.bookdata import simplify_title

    assert simplify_title("論語と算盤（上 自己修養篇）") == "論語と算盤"
    assert simplify_title("キーエンス解剖 最強希望のメカニズム") == "キーエンス解剖"


def test_simplify_title_keeps_plain_titles():
    from bookgram.bookdata import simplify_title

    assert simplify_title("すごい左利き") == "すごい左利き"
    assert simplify_title("2030半導体の地政学") == "2030半導体の地政学"


def test_validate_trims_extra_items_instead_of_failing():
    """枚数が1つ多いだけで生成全体を止めない。"""
    from bookgram.generate import POINT_SLIDES, RECOMMEND_ITEMS

    post = _fake_post()
    post["recommend"].append(_slide())
    post["points"].append(_slide())
    _validate(post)
    assert len(post["recommend"]) == RECOMMEND_ITEMS
    assert len(post["points"]) == POINT_SLIDES


def test_validate_still_rejects_too_few_items():
    post = _fake_post()
    post["points"] = post["points"][:2]
    with pytest.raises(ValueError, match="points"):
        _validate(post)


def test_fit_font_shrinks_for_long_lines():
    from bookgram.render import _fit_font

    short = _fit_font("短い行\nもう一行", 82, 900)
    long = _fit_font("これはとても長い一行で枠からはみ出してしまいます", 82, 900)
    assert short == 82
    assert long < short


def test_fit_font_never_goes_below_floor():
    from bookgram.render import _fit_font

    assert _fit_font("あ" * 200, 82, 900) == 32


# ------------------------------------------------------------------------- 特集


def test_feature_kind_rotates_between_weeks():
    """木曜は殿堂入りと小説が週ごとに入れ替わる。"""
    from datetime import date, timedelta

    from bookgram.config import feature_kind_for

    assert feature_kind_for(date(2026, 8, 24)) == "business"  # 月曜
    assert feature_kind_for(date(2026, 8, 25)) is None  # 火曜は通常投稿

    thursday = date(2026, 8, 27)
    assert feature_kind_for(thursday) != feature_kind_for(thursday + timedelta(days=7))
    assert {
        feature_kind_for(thursday),
        feature_kind_for(thursday + timedelta(days=7)),
    } == {"classic", "novel"}


def test_classic_cover_has_no_month_number():
    """殿堂入りの表紙は日付ではなく「今も読み継がれる」を出す。"""
    from datetime import date

    from bookgram.feature import _cover_parts, spec_for

    parts = _cover_parts(spec_for("classic"), date(2026, 8, 24))
    assert parts["month"] == ""
    assert parts["year"] == "今も読み継がれる"


def test_volume_titles_are_excluded_from_novel_feature():
    """続刊や資料集は1冊で読み切れないため特集に載せない。"""
    from bookgram.newbooks import is_standalone_title

    assert is_standalone_title("永遠の記憶")
    assert not is_standalone_title("後宮の棘（6）")
    assert not is_standalone_title("白鳥とコウモリ（上）")
    assert not is_standalone_title("シェスールの冒険者たち　設定資料集")
    assert not is_standalone_title("これは経費で落ちません！ 14 〜経理部の森若さん〜")
    assert not is_standalone_title("ブラッディダイスの殺人 上")
    assert not is_standalone_title("るるぶ■■版 蓋ヶ瀬")  # 仮題
    # 巻数に見える数字を含んでいても、区切られていなければ残す
    assert is_standalone_title("52ヘルツのクジラたち")
    assert is_standalone_title("777 トリプルセブン")
    assert is_standalone_title("文庫版 書楼弔堂 待宵")


def test_review_label_formats_count_and_average():
    from datetime import date

    from bookgram.newbooks import NewBook

    book = NewBook(
        title="嫌われる勇気",
        author="岸見一郎",
        publisher="ダイヤモンド社",
        sales_date=date(2013, 12, 13),
        sales_date_label="2013年12月13日",
        isbn="9784478025819",
        cover_url="https://example.com/x.jpg",
        caption="アドラー心理学の入門書。",
        review_count=3987,
        review_average=4.3,
    )
    assert book.review_label == "レビュー3,987件　★4.3"
    assert NewBook(
        title="", author="", publisher="", sales_date=date(2020, 1, 1),
        sales_date_label="", isbn="", cover_url="", caption="",
    ).review_label == ""


# ------------------------------------------------------------------------- リール


def test_reel_order_starts_with_the_question_card(tmp_path):
    """リールは表紙ではなく問いかけから始め、書誌情報は落とす。"""
    from bookgram.reel import reel_cards

    for i in range(1, 11):
        (tmp_path / f"{i:02d}.jpg").write_bytes(b"")
    (tmp_path / "story.jpg").write_bytes(b"")

    cards = [p.name for p in reel_cards(tmp_path, "book")]
    assert cards[0] == "04.jpg"
    assert cards[1] == "01.jpg"
    assert "02.jpg" not in cards
    assert "story.jpg" not in cards
    assert len(cards) == 8


def test_reel_uses_every_card_for_features(tmp_path):
    """特集は枚数が少なく順番自体が読み物なので、並べ替えない。"""
    from bookgram.reel import reel_cards

    for i in range(1, 7):
        (tmp_path / f"{i:02d}.jpg").write_bytes(b"")

    cards = [p.name for p in reel_cards(tmp_path, "feature")]
    assert cards == [f"{i:02d}.jpg" for i in range(1, 7)]


def test_broken_video_is_rejected_before_upload(tmp_path):
    """途中で切れた動画を公開すると Instagram 側で理由の分からない
    ERROR になる。手元で弾けることを確かめる。"""
    from bookgram.reel import _is_playable

    assert not _is_playable(tmp_path / "missing.mp4")

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert not _is_playable(empty)

    truncated = tmp_path / "truncated.mp4"
    # ftyp だけあって moov atom が無い、書き出し途中のファイルを模す
    truncated.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096)
    assert not _is_playable(truncated)


# ------------------------------------------------------------------------- 音源


def test_mood_follows_the_book_theme():
    from bookgram.music import mood_for

    business = {
        "book_title": "経営戦略の要諦",
        "hashtags": ["#経営", "#戦略"],
        "summary": {"text": "組織をどう動かすかという経営の話です。"},
    }
    assert mood_for(business) == "力強い"

    novel = {
        "book_title": "月の立つ林で",
        "hashtags": ["#小説"],
        "summary": {"text": "人生と家族をめぐる物語です。"},
    }
    assert mood_for(novel) == "静か"

    # 手掛かりが無ければ既定値に落ちる
    assert mood_for({"book_title": "無題"}) == "静か"


def test_audio_search_falls_back_to_trending():
    """雰囲気に合う曲が無ければ、検索語なし（＝トレンド）まで落ちる。"""
    from bookgram.music import pick_audio

    asked = []

    def search(query):
        asked.append(query)
        return [{"audio_id": "111", "title": "Trending"}] if query == "" else []

    draft = {"book_title": "経営戦略の要諦", "hashtags": ["#経営"]}
    chosen = pick_audio(search, draft, [])

    assert chosen["audio_id"] == "111"
    assert asked[-1] == ""  # 最後にトレンドを引いている
    assert len(asked) > 1  # その前に雰囲気つきの検索も試している


def test_audio_avoids_recently_used_tracks():
    from bookgram.music import pick_audio

    tracks = [
        {"audio_id": "a", "title": "A"},
        {"audio_id": "b", "title": "B"},
    ]
    draft = {"book_title": "月の立つ林で"}

    first = pick_audio(lambda q: tracks, draft, [])
    second = pick_audio(lambda q: tracks, draft, [first["audio_id"]])
    assert second["audio_id"] != first["audio_id"]

    # 同じ本・同じ条件なら毎回同じ曲になる（作り直しても揺れない）
    assert pick_audio(lambda q: tracks, draft, [])["audio_id"] == first["audio_id"]


def test_recent_audio_ids_reads_reel_records():
    from bookgram.music import recent_audio_ids

    drafts = [
        {"reel": {"audio": {"audio_id": "new"}}},
        {"reel": {}},
        {"reel": {"audio": {"audio_id": "old"}}},
    ]
    assert recent_audio_ids(drafts) == ["new", "old"]


# ---------------------------------------------------------------- insights


class _FakeClient(InstagramClient):
    """_get だけ差し替える。ネットワークは触らない。"""

    def __init__(self, responses):
        super().__init__("1", "token")
        object.__setattr__(self, "responses", responses)
        object.__setattr__(self, "asked", [])

    def _get(self, path, params):
        self.asked.append((path, params.get("metric")))
        rows = []
        for name in params.get("metric", "").split(","):
            if name not in self.responses:
                raise PublishError(f"(#100) invalid metric {name}")
            rows.append({"name": name, "total_value": {"value": self.responses[name]}})
        return {"data": rows}


def test_insights_drops_unsupported_metrics_instead_of_failing():
    """指標が1つでも無効だと Graph API は全体を蹴る。取れた分だけ残す。"""
    client = _FakeClient({"reach": 120, "views": 300})
    assert client.insights("1", ["reach", "views", "profile_views"]) == {
        "reach": 120,
        "views": 300,
    }
    # まとめて要求して蹴られた後、1つずつ試し直している
    assert client.asked[0][1] == "reach,views,profile_views"
    assert len(client.asked) == 4


def test_insights_reads_both_value_shapes():
    """期間指定は values、合計指定は total_value に入る。"""

    class C(_FakeClient):
        def _get(self, path, params):
            return {
                "data": [
                    {"name": "reach", "values": [{"value": 1}, {"value": 7}]},
                    {"name": "views", "total_value": {"value": 42}},
                ]
            }

    assert C({}).insights("1", ["reach", "views"]) == {"reach": 7, "views": 42}


def _reel_row(stats):
    return {
        "id": "m1",
        "timestamp": "2026-08-22T10:00:00+0000",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "like_count": 0,
        "stats": stats,
    }


def test_report_marks_missing_numbers_rather_than_showing_zero():
    """権限が無くて取れない値を 0 と書くと判断を誤らせる。- で出す。"""
    report = build_report({"username": "x"}, {}, [_reel_row({})], date(2026, 8, 22))
    assert "| リール | - | - | 0 | - | - | - |" in report
    assert "取得できませんでした。" in report


def test_report_shows_reel_watch_time_in_seconds():
    row = _reel_row({"views": 300, "reach": 280, "ig_reels_avg_watch_time": 4200})
    report = build_report({"username": "x"}, {"reach": 280}, [row], date(2026, 8, 22))
    assert "4.2秒" in report
    assert "平均再生数: 300" in report


def test_retention_turns_watch_time_into_a_share_of_the_video():
    """12.8秒の3秒と30秒の3秒は意味が違う。必ず割合に直す。"""
    from bookgram.insights import retention, reel_seconds

    assert reel_seconds(8) == pytest.approx(12.8)
    assert retention(2654, 12.8) == "21%"
    assert retention(2654, None) == "-"
    assert retention(None, 12.8) == "-"


def test_retention_is_keyed_by_media_id_not_date():
    """リールを出した日と、素材になった投稿の日はずれる。日付では結べない。"""
    row = _reel_row({"ig_reels_avg_watch_time": 2654})
    report = build_report(
        {"username": "x"}, {}, [row], date(2026, 8, 22), {"2026-08-22": 12.8}
    )
    assert "| 2.7秒 | - |" in report


def test_report_shows_retention_when_the_reel_length_is_known():
    row = _reel_row({"views": 57, "reach": 53, "ig_reels_avg_watch_time": 2654})
    report = build_report(
        {"username": "x"}, {}, [row], date(2026, 8, 22), {"m1": 12.8}
    )
    assert "| 2.7秒 | 21% |" in report
    assert "視聴維持率: 21%" in report
