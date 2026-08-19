import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from bookgram.bookdata import BookMaterial, _extract_openbd_texts, _normalize_isbn
from bookgram.generate import _output_schema, _validate
from bookgram.publish import PublishError, build_caption, publish_carousel
from bookgram.render import _variant, build_card_contexts


def test_normalize_isbn_strips_separators():
    assert _normalize_isbn("978-4-309-22736-8") == "9784309227368"


def test_material_requires_substance():
    thin = BookMaterial(title="薄い本", description="短い")
    assert not thin.has_substance()
    thick = BookMaterial(title="厚い本", description="あ" * 100)
    assert thick.has_substance()


def test_prompt_block_contains_description():
    material = BookMaterial(
        title="テスト本", authors=["著者A"], description="内容紹介テキスト"
    )
    block = material.to_prompt_block()
    assert "テスト本" in block
    assert "著者A" in block
    assert "内容紹介テキスト" in block


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


def _fake_day(day_index=1, cards=5):
    return {
        "day_index": day_index,
        "theme": "テーマ",
        "cards": [
            {"kicker": "k", "headline": "h", "body": "b"} for _ in range(cards)
        ],
        "caption": "キャプション",
        "hashtags": ["#読書"],
        "grounding": ["内容紹介より"],
    }


def test_validate_rejects_wrong_day_count():
    with pytest.raises(ValueError, match="days"):
        _validate({"days": [_fake_day()]})


def test_validate_rejects_wrong_card_count():
    payload = {"days": [_fake_day(i, cards=5) for i in range(1, 6)]}
    payload["days"][2]["cards"] = payload["days"][2]["cards"][:3]
    with pytest.raises(ValueError, match="cards"):
        _validate(payload)


def test_validate_accepts_well_formed_payload():
    _validate({"days": [_fake_day(i) for i in range(1, 6)]})


def test_output_schema_marks_objects_closed():
    schema = _output_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["days"]["items"]["additionalProperties"] is False


def test_card_variants_by_position():
    assert _variant(1, 5) == "hook"
    assert _variant(3, 5) == "body"
    assert _variant(5, 5) == "outro"


def test_build_card_contexts_shapes_first_and_last():
    contexts = build_card_contexts(_fake_day(2), "書名", "著者", "一言コピー")
    assert len(contexts) == 5
    assert contexts[0]["variant"] == "hook"
    assert contexts[0]["kicker"].startswith("Day 2")
    assert contexts[0]["one_line"] == "一言コピー"
    assert contexts[-1]["variant"] == "outro"
    assert contexts[-1]["cta_title"]


def test_build_caption_prefixes_hashtags():
    caption = build_caption({"caption": "本文です", "hashtags": ["#読書", "書評"]})
    assert caption.startswith("本文です")
    assert "#読書" in caption
    assert "#書評" in caption


def test_publish_rejects_single_image():
    with pytest.raises(PublishError, match="2〜10枚"):
        publish_carousel(None, ["https://example.com/1.jpg"], "caption")
