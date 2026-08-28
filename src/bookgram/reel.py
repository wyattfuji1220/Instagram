"""投稿済みのカード画像から、リール用の縦動画を組み立てる。

カルーセルは保存されやすいがフォロワー外へ届きにくい。リールは
その逆なので、同じ素材を動画として作り直して露出を増やす。
画像を使い回すため、生成AIの追加費用はかからない。

ただし全10枚をそのまま流しても伸びない。実測すると平均視聴は2.6秒で、
12.8秒の動画に対して維持率21%だった。1枚1.6秒に対し文面は25文字前後あり、
表示時間内に読み終われないのが原因（日本語の黙読は速くて10文字/秒程度）。
1枚あたりを読み切れる長さにし、そのぶん枚数を絞る。

フレームは Pillow で作り、ffmpeg に生のRGBを流し込んで符号化する。
ffmpeg の複雑なフィルタ式を書かずに、動きを完全に制御できる。
"""

from __future__ import annotations

import subprocess
import tempfile
from datetime import date
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

from .config import STORY_HEIGHT, STORY_WIDTH

FPS = 30
SECONDS_PER_CARD = 2.4  # 25文字前後を読み切れる長さ
FADE_SECONDS = 0.35
ZOOM_RANGE = 0.07  # 1枚のあいだに何倍まで寄せるか
# カードの外側を埋める色。以前は同じ絵のぼかしを敷いていたが、カードの枠線が
# 見えて「投稿画像を貼っただけ」に見えるため、黒で落ち着かせる。
BACKDROP = (10, 9, 12)
# リールの下端はキャプションやボタンのUIに覆われる。カードを少し上げて逃がす。
VERTICAL_SHIFT = 40
CRF = 30  # リールは何本も溜まるのでファイルを小さく保つ
REEL_FILENAME = "reel.mp4"
# 書籍投稿10枚のうち、動画で見せる並び（1始まり）。
# 2=書誌情報（書影・著者・発行日）は締めの直前。ここが無いと、最後まで見ても
# 「どの本の話だったのか」が分からないまま終わる。
# 3=おすすめ と要点の後半は落とし、読み切れる枚数に絞る。
#
# 冒頭だけ違う2種類を交互に出す。全部同じ作りだと、維持率が動いたときに
# 何が効いたのか分からない。1=結論 は唯一フォントが大きく設計された面
# （render の COVER_MAX_FONT）で、4=問いかけ は答えを待たせる面。
BOOK_REEL_VARIANTS = {
    "conclusion": (1, 4, 5, 6, 2, 10),
    "question": (4, 1, 5, 6, 2, 10),
}
VARIANT_NAMES = tuple(sorted(BOOK_REEL_VARIANTS))
BOOK_REEL_ORDER = BOOK_REEL_VARIANTS["conclusion"]


def variant_for(day: date) -> str:
    """その日の構成。日付で決めるので、作り直しても同じものが出る。"""
    return VARIANT_NAMES[day.toordinal() % len(VARIANT_NAMES)]


# カルーセルの1枚目と同じ絵（=カード1）は、プロフィールのグリッドで重複して
# 見えるのでサムネイルに使わない。最初にカード1以外が出る面の途中を選ぶ。
COVER_CARD = 1


def thumb_offset_ms(variant: str) -> int:
    """グリッドに出す静止画の位置（ミリ秒）。"""
    order = BOOK_REEL_VARIANTS.get(variant, BOOK_REEL_ORDER)
    position = next(
        (i for i, number in enumerate(order) if number != COVER_CARD), 0
    )
    # その面の真ん中。切り替わり際のフェード中に当たらないようにする。
    return int((position + 0.5) * SECONDS_PER_CARD * 1000)


def _frame_count() -> int:
    return int(SECONDS_PER_CARD * FPS)


def _fade_frames() -> int:
    return int(FADE_SECONDS * FPS)


def _is_vertical(card: Image.Image) -> bool:
    """すでに 9:16 で描かれた面かどうか。"""
    return card.height / card.width >= STORY_HEIGHT / STORY_WIDTH - 0.01


def _compose(card: Image.Image, scale: float) -> Image.Image:
    """1枚のカードを 9:16 の画面に配置する。

    リール用に描いた 9:16 の面は、画面を覆うまで広げて余りを切る。文字は
    中央の 4:5 に収めてあるので、端が数%欠けても本文には当たらない。

    カルーセルの 4:5 を流用する場合は、横幅いっぱいで止めて余白を黒で埋める。
    カードが端近くまで文字を置いているため、これ以上寄せると文字が切れる
    （1.24倍で試したら「理解」の理と「脳」が欠けた）。
    """
    if _is_vertical(card):
        height = int(STORY_HEIGHT * scale)
        width = int(height * card.width / card.height)
        big = card.resize((max(width, STORY_WIDTH), height), Image.LANCZOS)
        left = (big.width - STORY_WIDTH) // 2
        top = (big.height - STORY_HEIGHT) // 2
        return big.crop((left, top, left + STORY_WIDTH, top + STORY_HEIGHT))

    frame = Image.new("RGB", (STORY_WIDTH, STORY_HEIGHT), BACKDROP)
    width = int(STORY_WIDTH * scale / (1 + ZOOM_RANGE))
    height = int(width * card.height / card.width)
    foreground = card.resize((width, height), Image.LANCZOS)
    frame.paste(
        foreground,
        ((STORY_WIDTH - width) // 2, (STORY_HEIGHT - height) // 2 - VERTICAL_SHIFT),
    )
    return frame


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


def _encode_command(out_path: Path, *, silent_audio: bool) -> list[str]:
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        # 進捗行を止める。stderr が溢れると ffmpeg が書き込みで止まり、
        # こちらの書き込みも道連れになる。
        "-loglevel", "warning",
        "-nostats",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{STORY_WIDTH}x{STORY_HEIGHT}",
        "-r", str(FPS),
        "-i", "-",
    ]
    if silent_audio:
        # Instagram は無音の動画も受け付けるが、音声トラックがあるほうが
        # 扱いが素直。lavfi を持たない ffmpeg では失敗するので必須にしない。
        command += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
        ]
    command += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if silent_audio:
        command += ["-c:a", "aac", "-b:a", "64k"]
    command.append(str(out_path))
    return command


def _run_encode(card_paths: list[Path], out_path: Path, *, silent_audio: bool) -> None:
    """ffmpeg に生フレームを流し込む。失敗したら stderr を添えて投げる。

    stderr はパイプではなく一時ファイルに逃がす。パイプのままだと
    バッファが埋まった時点で ffmpeg が止まり、こちらの書き込みも
    ブロックして進まなくなる。
    """
    with tempfile.TemporaryFile() as errfile:
        process = subprocess.Popen(
            _encode_command(out_path, silent_audio=silent_audio),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errfile,
        )
        assert process.stdin is not None
        broken = False
        try:
            for frame in iter_frames(card_paths):
                process.stdin.write(frame.tobytes())
        except BrokenPipeError:
            # ffmpeg が先に死んだ。理由は stderr 側にある。
            broken = True
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                broken = True
            process.wait()

        if process.returncode != 0 or broken:
            errfile.seek(0)
            detail = errfile.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg が異常終了しました (code={process.returncode}): "
                + (detail[-800:] or "(出力なし)")
            )


def _is_playable(path: Path) -> bool:
    """書き出した動画がコンテナとして読めるか確かめる。

    書き出しが途中で切れると moov atom が無い壊れたファイルが残る。
    Instagram はそれを受け取ってから処理中に ERROR にするだけで、
    理由を返してくれない。手元で弾く。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_reel(card_paths: list[Path], out_path: Path) -> Path:
    """カード画像から縦動画を書き出す。"""
    if not card_paths:
        raise ValueError("カード画像がありません")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    for silent_audio in (True, False):
        try:
            _run_encode(card_paths, out_path, silent_audio=silent_audio)
        except RuntimeError as error:
            problems.append(str(error))
        else:
            if _is_playable(out_path):
                return out_path
            problems.append("書き出したファイルが再生できませんでした（moov atom 欠落）")
        if silent_audio:
            print("[warn] 無音トラック付きの書き出しに失敗しました。音声なしで作り直します。")

    # 壊れたファイルを残すと、それが公開されて Instagram 側で ERROR になる。
    out_path.unlink(missing_ok=True)
    raise RuntimeError("動画を書き出せませんでした: " + " / ".join(problems))


def cards_in(image_dir: Path) -> list[Path]:
    """その投稿のカード画像を並び順で返す。"""
    return sorted(p for p in image_dir.glob("*.jpg") if p.stem.isdigit())


def reel_cards(
    image_dir: Path, kind: str = "book", variant: str | None = None
) -> list[Path]:
    """動画にするカードを、リール向けの並びで返す。

    書籍投稿だけ並べ替える。特集は枚数が少なく、順番自体が読み物なので
    そのまま流す。
    """
    cards = cards_in(image_dir)
    if kind != "book":
        return cards
    order = BOOK_REEL_VARIANTS.get(variant or "", BOOK_REEL_ORDER)
    by_number = {int(p.stem): p for p in cards}
    picked = [by_number[n] for n in order if n in by_number]
    return picked or cards
