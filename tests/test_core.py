import sys
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
from bookgram.publish import PublishError, build_caption, publish_carousel
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


def test_open_slots_skips_feature_weekday(tmp_path, monkeypatch):
    """月曜は新刊特集の枠なので、通常投稿の割り当て対象から外れる。"""
    from datetime import date

    from bookgram import queue as bookqueue

    monkeypatch.setattr(bookqueue, "DRAFTS_DIR", tmp_path)
    monday = date(2026, 8, 24)
    assert monday.weekday() == 0

    slots = bookqueue.open_slots(date(2026, 8, 21), 7, skip_weekday=0)
    assert monday not in slots
    assert len(slots) == 6


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
    slots = bookqueue.open_slots(date(2026, 8, 21), 60, skip_weekday=0)
    assert len(slots) >= 50
