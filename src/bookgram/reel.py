"""投稿済みのカード画像から、リール用の縦動画を組み立てる。

カルーセルは保存されやすいがフォロワー外へ届きにくい。リールは
その逆なので、同じ素材を動画として作り直して露出を増やす。
画像を使い回すため、生成AIの追加費用はかからない。

ただし全10枚をそのまま流しても伸びない。リールは最初の1〜2秒で
離脱が決まるため、表紙ではなく問いかけのカードから始め、
書誌情報のような静かな面は落として8枚に絞る。

フレームは Pillow で作り、ffmpeg に生のRGBを流し込んで符号化する。
ffmpeg の複雑なフィルタ式を書かずに、動きを完全に制御できる。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageFilter

from .config import STORY_HEIGHT, STORY_WIDTH

FPS = 30
SECONDS_PER_CARD = 1.6
FADE_SECONDS = 0.35
ZOOM_RANGE = 0.07  # 1枚のあいだに何倍まで寄せるか
BACKGROUND_BLUR = 28
BACKGROUND_DARKEN = 0.35
# リールの下端はキャプションやボタンのUIに覆われる。カードを少し上げて逃がす。
VERTICAL_SHIFT = 70
CRF = 30  # リールは何本も溜まるのでファイルを小さく保つ
REEL_FILENAME = "reel.mp4"
# 書籍投稿10枚のうち、動画で見せる並び（1始まり）。
# 4=問いかけ を先頭に置いて掴み、2=書誌情報 と 3=おすすめ は落とす。
BOOK_REEL_ORDER = (4, 1, 5, 6, 7, 8, 9, 10)


def _frame_count() -> int:
    return int(SECONDS_PER_CARD * FPS)


def _fade_frames() -> int:
    return int(FADE_SECONDS * FPS)


def _compose(card: Image.Image, scale: float) -> Image.Image:
    """1枚のカードを 9:16 の画面に配置する。背景は同じ絵をぼかして敷く。"""
    background = card.resize(
        (STORY_WIDTH, int(STORY_WIDTH * card.height / card.width)), Image.LANCZOS
    )
    # 画面を覆うまで拡大してから中央を切り出す
    if background.height < STORY_HEIGHT:
        ratio = STORY_HEIGHT / background.height
        background = background.resize(
            (int(background.width * ratio), STORY_HEIGHT), Image.LANCZOS
        )
    left = (background.width - STORY_WIDTH) // 2
    top = (background.height - STORY_HEIGHT) // 2
    background = background.crop(
        (left, top, left + STORY_WIDTH, top + STORY_HEIGHT)
    ).filter(ImageFilter.GaussianBlur(BACKGROUND_BLUR))
    background = Image.blend(
        background, Image.new("RGB", background.size, (8, 8, 10)), BACKGROUND_DARKEN
    )

    width = int(STORY_WIDTH * 0.92 * scale)
    height = int(width * card.height / card.width)
    foreground = card.resize((width, height), Image.LANCZOS)
    background.paste(
        foreground,
        ((STORY_WIDTH - width) // 2, (STORY_HEIGHT - height) // 2 - VERTICAL_SHIFT),
    )
    return background


def _card_frames(card: Image.Image) -> list[Image.Image]:
    total = _frame_count()
    return [
        _compose(card, 1.0 + ZOOM_RANGE * (i / max(total - 1, 1)))
        for i in range(total)
    ]


def iter_frames(card_paths: list[Path]):
    """カードをまたいでクロスフェードしながらフレームを吐く。"""
    fade = _fade_frames()
    previous_tail: list[Image.Image] = []

    for path in card_paths:
        with Image.open(path) as raw:
            frames = _card_frames(raw.convert("RGB"))

        for i, frame in enumerate(frames):
            if i < fade and previous_tail:
                blended = Image.blend(previous_tail[i], frame, (i + 1) / fade)
                yield blended
            elif i < fade:
                yield frame
            else:
                yield frame
        previous_tail = frames[-fade:] if fade else []


def build_reel(card_paths: list[Path], out_path: Path) -> Path:
    """カード画像から縦動画を書き出す。"""
    if not card_paths:
        raise ValueError("カード画像がありません")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{STORY_WIDTH}x{STORY_HEIGHT}",
        "-r", str(FPS),
        "-i", "-",
        # Instagram は無音でも受け付けるが、音声トラックが無いと弾かれることがある
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-shortest",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", "64k",
        str(out_path),
    ]

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    try:
        for frame in iter_frames(card_paths):
            process.stdin.write(frame.tobytes())
    finally:
        process.stdin.close()
        _, stderr = process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            "動画の書き出しに失敗しました: "
            + stderr.decode("utf-8", errors="replace")[-500:]
        )
    return out_path


def cards_in(image_dir: Path) -> list[Path]:
    """その投稿のカード画像を並び順で返す。"""
    return sorted(p for p in image_dir.glob("*.jpg") if p.stem.isdigit())


def reel_cards(image_dir: Path, kind: str = "book") -> list[Path]:
    """動画にするカードを、リール向けの並びで返す。

    書籍投稿だけ並べ替える。特集は枚数が少なく、順番自体が読み物なので
    そのまま流す。
    """
    cards = cards_in(image_dir)
    if kind != "book":
        return cards
    by_number = {int(p.stem): p for p in cards}
    picked = [by_number[n] for n in BOOK_REEL_ORDER if n in by_number]
    return picked or cards
