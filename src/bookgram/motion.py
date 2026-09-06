"""写真の上に文字を打ち出していく形式のリールを組み立てる。

既存の `reel.py` は、投稿済みのカード画像をそのまま流す。作り直す手間が
無い代わりに、1枚目から情報量が多く、動きは「ゆっくり寄る」しかない。
リールは最初の1〜2秒で送られるかどうかが決まるので、そこに動きが要る。

このモジュールは、以前このアカウントで反応が良かったリールの作りを再現する。
実物（2024年1月の4本）を見て取れた約束事は次の4つ。

  - 全面が写真。文字はその上に乗せる（カードを貼らない）
  - 文字は1文字ずつ打ち出され、打ち終わったら残る
  - 効かせたい語だけ色を変える。使う色は白と黄の2色だけ
  - 1画面に置く用件は1つ。つかみ → 書誌 → おすすめ → 導線

フレームは Pillow で組み、`reel.encode_frames` に渡して符号化する。
`reel.py` には手を入れていないので、現行の配信はこのモジュールと無関係に動く。
"""

from __future__ import annotations

import io
import re
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Sequence

import requests
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
    ImageStat,
)

from .config import (
    BACKGROUNDS_DIR,
    COVERS_DIR,
    IMG_DIR,
    STORY_HEIGHT,
    STORY_WIDTH,
    find_profile_icon,
    load_account,
)
from .reel import FPS, encode_frames

# 色。白と1色だけに絞る。3色以上を混ぜると、どこを読めばよいのか分からなくなる。
TEXT = (255, 255, 255)
ACCENT = (247, 199, 70)

# 文字を置いてよい横幅。左右に余白を残さないと、機種によっては端が切れる。
USABLE_WIDTH = 880
MARGIN_LEFT = (STORY_WIDTH - USABLE_WIDTH) // 2

# 1秒あたり何文字打つか。実物は9文字を約1秒で打っていた。少し速めにして、
# 読み終わってから次が出るまでの間を短くする。
CHARS_PER_SECOND = 20
# 打ち出しの先端。ここを硬く切ると、文字の影まで縦に切れて不自然になる。
TYPE_FEATHER = 22
# 文字以外（書影・アイコン）が浮かび上がる時間
FADE_IN_SECONDS = 0.34

# 場面のつなぎ目。長いと切り替わりが眠くなる。
FADE_SECONDS = 0.2
# 1枚の写真のあいだに何倍まで寄せるか
ZOOM = 0.10

# 背景の加工。文字を白で置くので、写真は暗く沈めてぼかす。
BLUR_RADIUS = 5
DIM = 0.44
# 書影の場面だけは書影が主役なので、背景をさらに落とす。
COVER_BLUR_RADIUS = 12
COVER_DIM = 0.30
# 暗くしたあとの明るさの上限。写真ごとに元の明るさが違うので、同じ倍率で
# 落とすと明るい1枚だけ白文字が読みにくくなる（閲覧室の写真で起きた）。
MAX_LUMA = 46

# 文字サイズ
HOOK_MAX_SIZE = 88
TITLE_MAX_SIZE = 80
BODY_SIZE = 46
LABEL_SIZE = 40
TAG_SIZE = 40
LINE_SPACING = 1.46

# 書影を置ける範囲。下端はリールのUI（キャプションやボタン）を避けた位置。
COVER_MAX_WIDTH = 640
COVER_MAX_HEIGHT = 980
COVER_BOTTOM = 1580

# チェック印
CHECK_SIZE = 42
CHECK_GAP = 24


# 太字のゴシック。CI（Ubuntu + fonts-noto-cjk）と手元（Windows）の両方で
# 見つかるものを順に試す。見つからないまま進むと Pillow が英字だけの既定
# フォントに落ち、日本語が全部四角になる（8/30 のリールで一度やった）。
FONT_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", None),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc", "Bold"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Bold"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", None),
    ("C:/Windows/Fonts/NotoSansJP-VF.ttf", "Bold"),
    ("C:/Windows/Fonts/YuGothB.ttc", None),
    ("C:/Windows/Fonts/meiryob.ttc", None),
)


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    for path, variation in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            font = ImageFont.truetype(path, size)
            if variation:
                font.set_variation_by_name(variation)
        except (OSError, ValueError):
            continue
        return font
    raise RuntimeError(
        "日本語の太字フォントが見つかりません。"
        "Ubuntu なら fonts-noto-cjk を入れてください。"
    )


def _pad(size: int) -> int:
    """影がにじむぶんの余白。文字の周りに取る。"""
    return max(18, size // 3)


Segment = tuple[str, tuple[int, int, int]]


def colored_lines(text: str, highlight: str) -> list[list[Segment]]:
    """改行済みの文を、行ごとの（文字列, 色）の並びにする。

    ハイライトは行をまたぐことがあるので、いったん全体での位置を出してから
    行に割り付ける。行ごとに部分一致を探すと、またいだ分を取りこぼす。
    """
    lines = text.split("\n")
    joined = "\n".join(lines)
    start = joined.find(highlight) if highlight else -1
    end = start + len(highlight)

    out: list[list[Segment]] = []
    at = 0
    for line in lines:
        left, right = at, at + len(line)
        if start < 0 or end <= left or start >= right:
            out.append([(line, TEXT)])
        else:
            a, b = max(start, left) - left, min(end, right) - left
            segments: list[Segment] = []
            if line[:a]:
                segments.append((line[:a], TEXT))
            if line[a:b]:
                segments.append((line[a:b], ACCENT))
            if line[b:]:
                segments.append((line[b:], TEXT))
            out.append(segments)
        at = right + 1
    return [line for line in out if "".join(t for t, _ in line).strip()]


def _plain(segments: Sequence[Segment]) -> str:
    return "".join(text for text, _ in segments)


def fit_size(lines: Sequence[Sequence[Segment]], max_size: int, usable: int) -> int:
    """一番長い行が幅に収まる最大の文字サイズ。"""
    longest = max((_plain(line) for line in lines), key=len, default="")
    size = max_size
    while size > 30 and _font(size).getlength(longest) > usable:
        size -= 2
    return size


def line_image(
    segments: Sequence[Segment], size: int
) -> tuple[Image.Image, tuple[int, ...]]:
    """1行を描いた透過画像と、文字ごとの右端の位置を返す。

    右端の位置は打ち出しに使う。文字を1枚ずつ描くのではなく、描き上げた
    1行を左から出していくので、字間が打ち出しの途中で動かない。

    白抜きだけだと、明るい写真に重なった瞬間に読めなくなる。縁取りではなく
    ぼかした影を敷くと、輪郭を硬くせずに背景から浮く。
    """
    font = _font(size)
    widths = [font.getlength(text) for text, _ in segments]
    ascent, descent = font.getmetrics()
    pad = _pad(size)
    canvas = Image.new(
        "RGBA",
        (int(sum(widths)) + pad * 2 + 2, ascent + descent + pad * 2),
        (0, 0, 0, 0),
    )

    shadow = Image.new("L", canvas.size, 0)
    drawer = ImageDraw.Draw(shadow)
    x = float(pad)
    for (text, _), width in zip(segments, widths):
        drawer.text((x, pad), text, font=font, fill=205)
        x += width
    shadow = shadow.filter(ImageFilter.GaussianBlur(size // 9 + 3))
    canvas.paste((0, 0, 0), (0, max(3, size // 18)), shadow)

    drawer = ImageDraw.Draw(canvas)
    x = float(pad)
    cuts = [pad]
    for (text, color), width in zip(segments, widths):
        drawer.text((x, pad), text, font=font, fill=color + (255,))
        cursor = x
        for character in text:
            cursor += font.getlength(character)
            # 影のぶん少し右まで見せないと、打ち終わった字の右肩が欠ける。
            cuts.append(int(cursor + size * 0.14))
        x += width
    return canvas, tuple(cuts)


@lru_cache(maxsize=1024)
def _ramp(width: int, height: int, cut: int) -> Image.Image:
    """左から cut までを見せる帯。先端だけ薄く落として硬い切り口を隠す。"""
    row = bytearray(width)
    for x in range(width):
        if x < cut - TYPE_FEATHER:
            row[x] = 255
        elif x < cut:
            row[x] = int(255 * (cut - x) / TYPE_FEATHER)
    return Image.frombytes("L", (width, 1), bytes(row)).resize((width, height))


@dataclass
class Element:
    """画面に置くひとつの部品と、それが出てくる時刻。

    cuts があるものは打ち出し、無いものは淡く浮かび上がらせる。
    """

    image: Image.Image
    x: int
    y: int
    at: float
    cuts: tuple[int, ...] = ()

    @property
    def ends_at(self) -> float:
        if self.cuts:
            return self.at + (len(self.cuts) - 1) / CHARS_PER_SECOND
        return self.at + FADE_IN_SECONDS

    def mask(self, now: float) -> Image.Image | None:
        alpha = self.image.getchannel("A")
        if self.cuts:
            shown = int((now - self.at) * CHARS_PER_SECOND)
            if shown <= 0:
                return None
            if shown >= len(self.cuts) - 1:
                return alpha
            return ImageChops.multiply(
                alpha, _ramp(self.image.width, self.image.height, self.cuts[shown])
            )
        progress = (now - self.at) / FADE_IN_SECONDS
        if progress <= 0:
            return None
        if progress >= 1:
            return alpha
        return alpha.point(lambda v, a=_ease(progress): int(v * a))


@dataclass
class Scene:
    background: Path
    seconds: float
    elements: list[Element] = field(default_factory=list)
    zoom_in: bool = True
    blur: int = BLUR_RADIUS
    dim: float = DIM


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


@lru_cache(maxsize=8)
def _prepared_background(path: str, blur: int, dim: float) -> Image.Image:
    """写真を 9:16 に切り、ぼかして暗くしたものを1枚だけ作る。

    寄りの動きは、この1枚から切り出す範囲を変えて作る。毎フレームぼかし
    直すと、10秒の動画でも数分かかる。
    """
    width = int(STORY_WIDTH * (1 + ZOOM))
    height = int(STORY_HEIGHT * (1 + ZOOM))
    with Image.open(path) as raw:
        image = raw.convert("RGB")
    scale = max(width / image.width, height / image.height)
    image = image.resize(
        (max(width, int(image.width * scale)), max(height, int(image.height * scale))),
        Image.LANCZOS,
    )
    left = (image.width - width) // 2
    top = (image.height - height) // 2
    image = image.crop((left, top, left + width, top + height))
    image = image.filter(ImageFilter.GaussianBlur(blur))
    black = Image.new("RGB", image.size, (8, 7, 10))
    image = Image.blend(black, image, dim)
    luma = ImageStat.Stat(image.convert("L")).mean[0]
    if luma > MAX_LUMA:
        image = Image.blend(black, image, MAX_LUMA / luma)
    return image


def _background_frame(base: Image.Image, progress: float, zoom_in: bool) -> Image.Image:
    """寄り（または引き）の途中の1枚を切り出す。

    背景はぼかしてあるので拡大は BILINEAR で足りる。LANCZOS にしても
    見た目は変わらず、書き出し時間だけが倍になる。
    """
    t = _ease(progress) if zoom_in else 1.0 - _ease(progress)
    factor = 1.0 - (ZOOM / (1 + ZOOM)) * t
    width = int(base.width * factor)
    height = int(base.height * factor)
    left = (base.width - width) // 2
    top = (base.height - height) // 2
    return base.crop((left, top, left + width, top + height)).resize(
        (STORY_WIDTH, STORY_HEIGHT), Image.BILINEAR
    )


def scene_frames(scene: Scene) -> Iterator[Image.Image]:
    """1場面ぶんのフレーム。部品を出現時刻の順に重ねていく。"""
    base = _prepared_background(str(scene.background), scene.blur, scene.dim)
    total = max(int(scene.seconds * FPS), 1)
    for index in range(total):
        now = index / FPS
        frame = _background_frame(base, index / max(total - 1, 1), scene.zoom_in)
        for element in scene.elements:
            mask = element.mask(now)
            if mask is None:
                continue
            frame.paste(element.image.convert("RGB"), (element.x, element.y), mask)
        yield frame


def iter_frames(scenes: Sequence[Scene]) -> Iterator[Image.Image]:
    """場面をつなぐ。境目は短くクロスフェードする。

    直前の場面の終わりだけを残して混ぜる。場面ぜんぶを配列に持つと、
    1080x1920 のフレームが100枚を超えて数百MBになる。
    """
    fade = max(int(FADE_SECONDS * FPS), 1)
    tail: deque[Image.Image] = deque(maxlen=fade)
    for number, scene in enumerate(scenes):
        previous = list(tail)
        current: deque[Image.Image] = deque(maxlen=fade)
        for index, frame in enumerate(scene_frames(scene)):
            if number and index < len(previous):
                frame = Image.blend(previous[index], frame, (index + 1) / fade)
            current.append(frame)
            yield frame
        tail = current


# ---- 部品づくり -----------------------------------------------------------


def _backgrounds() -> list[Path]:
    return sorted(p for p in BACKGROUNDS_DIR.glob("*.jpg"))


def _pick_backgrounds(seed: str, count: int) -> list[Path]:
    """場面ごとに違う写真を、書名から決まる順で選ぶ。

    同じ本なら何度作り直しても同じ絵になる。
    """
    images = _backgrounds()
    if not images:
        raise RuntimeError(f"{BACKGROUNDS_DIR} に背景画像がありません。")
    start = sum(ord(c) for c in seed) % len(images)
    return [images[(start + i) % len(images)] for i in range(count)]


def _cover_image(draft: dict) -> Image.Image | None:
    """書影をできるだけ大きい寸法で取る。

    カード用の URL は 200x200 に縮めてある。動画では画面の半分近くを占める
    ので、そのまま引き伸ばすと粗が見える。元の寸法を要求し直す。
    """
    isbn = str(draft.get("isbn") or "")
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        path = COVERS_DIR / f"{isbn}{suffix}"
        if path.exists():
            with Image.open(path) as raw:
                return raw.convert("RGB")

    url = draft.get("cover_url") or ""
    if not url:
        return None
    url = re.sub(r"_ex=\d+x\d+", "_ex=800x800", url)
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"[warn] 書影を取得できませんでした: {type(error).__name__}")
        return None
    with Image.open(io.BytesIO(response.content)) as raw:
        return raw.convert("RGB")


def _with_shadow(image: Image.Image, spread: int = 28) -> Image.Image:
    """写真の下に影を敷いて、背景から浮かせる。"""
    pad = spread * 2
    canvas = Image.new(
        "RGBA", (image.width + pad * 2, image.height + pad * 2), (0, 0, 0, 0)
    )
    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rectangle(
        (pad, pad + spread // 2, pad + image.width, pad + image.height + spread // 2),
        fill=195,
    )
    canvas.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(spread)))
    canvas.paste(image.convert("RGBA"), (pad, pad))
    return canvas


def _circle(image: Image.Image, size: int) -> Image.Image:
    """プロフィール画像を丸く切る。"""
    scale = size / min(image.width, image.height)
    image = image.resize(
        (max(size, int(image.width * scale)), max(size, int(image.height * scale))),
        Image.LANCZOS,
    )
    left = (image.width - size) // 2
    top = (image.height - size) // 2
    image = image.crop((left, top, left + size, top + size)).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring.paste(image, (0, 0), mask)
    ImageDraw.Draw(ring).ellipse(
        (2, 2, size - 3, size - 3), outline=(255, 255, 255, 220), width=4
    )
    return ring


def _check_mark() -> Image.Image:
    """おすすめ欄の ☑。四角に白の枠、中に黄のチェック。"""
    box = CHECK_SIZE
    canvas = Image.new("RGBA", (box + 12, box + 12), (0, 0, 0, 0))
    drawer = ImageDraw.Draw(canvas)
    drawer.rounded_rectangle(
        (6, 6, box + 2, box + 2), radius=6, outline=(255, 255, 255, 235), width=4
    )
    drawer.line(
        [
            (6 + box * 0.24, 6 + box * 0.52),
            (6 + box * 0.45, 6 + box * 0.76),
            (6 + box * 0.82, 6 + box * 0.22),
        ],
        fill=ACCENT + (255,),
        width=6,
        joint="curve",
    )
    return canvas


def _typed_block(
    lines: Sequence[Sequence[Segment]],
    size: int,
    top: int,
    start: float,
    *,
    left: int | None = None,
    gap: float = 0.14,
) -> tuple[list[Element], float]:
    """行を上から順に打ち出す。最後の行が打ち終わる時刻も返す。"""
    elements: list[Element] = []
    at = start
    step = int(size * LINE_SPACING)
    for index, segments in enumerate(lines):
        image, cuts = line_image(segments, size)
        x = left - _pad(size) if left is not None else (STORY_WIDTH - image.width) // 2
        element = Element(image, x, top + step * index - _pad(size), at, cuts)
        elements.append(element)
        at = element.ends_at + gap
    return elements, at - gap


# ---- 場面の組み立て -------------------------------------------------------


def _hook_scene(draft: dict, background: Path, account: dict) -> Scene:
    """つかみ。写真の上に、この本の結論を打ち出す。"""
    cover = draft.get("cover") or {}
    lines = colored_lines(
        cover.get("text") or draft.get("book_title", ""),
        cover.get("highlight", ""),
    )
    size = fit_size(lines, HOOK_MAX_SIZE, USABLE_WIDTH)

    elements: list[Element] = []
    at = 0.1
    tag = account.get("cover_tag", "")
    if tag:
        tag_elements, at = _typed_block([[(tag, TEXT)]], TAG_SIZE, 300, at)
        elements += tag_elements
        at += 0.2

    body, at = _typed_block(lines, size, 470, at)
    elements += body
    return Scene(background, at + 1.1, elements)


def _book_scene(draft: dict, background: Path) -> Scene:
    """書誌。どの本の話なのかを一度だけはっきり見せる。"""
    from .render import balanced_break

    title = balanced_break(draft.get("book_title", ""))
    lines = [[(text, ACCENT)] for text in title.split("\n") if text.strip()]
    size = fit_size(lines, TITLE_MAX_SIZE, USABLE_WIDTH)

    elements, at = _typed_block(lines, size, 230, 0.12)
    bottom = 230 + int(size * LINE_SPACING) * (len(lines) - 1) + size

    author = draft.get("book_author", "")
    if author:
        more, at = _typed_block([[(author, TEXT)]], BODY_SIZE + 10, bottom + 40, at + 0.1)
        elements += more
        bottom += 40 + BODY_SIZE + 10

    cover = _cover_image(draft)
    if cover is not None:
        # 書影は書名の下から始める。下端を固定して逆算すると、書名が2行に
        # なった投稿で著者名に重なる（『苦しかったときの話をしようか』で発生）。
        top = bottom + 60
        height = min(COVER_MAX_HEIGHT, COVER_BOTTOM - top)
        width = int(cover.width * height / cover.height)
        if width > COVER_MAX_WIDTH:
            width = COVER_MAX_WIDTH
            height = int(cover.height * width / cover.width)
        shaped = _with_shadow(cover.resize((width, height), Image.LANCZOS))
        elements.append(
            Element(
                shaped,
                (STORY_WIDTH - shaped.width) // 2,
                top + (COVER_BOTTOM - top - height) // 2 - (shaped.height - height) // 2,
                at + 0.15,
            )
        )
        at += 0.15 + FADE_IN_SECONDS
    return Scene(
        background,
        at + 1.5,
        elements,
        zoom_in=False,
        blur=COVER_BLUR_RADIUS,
        dim=COVER_DIM,
    )


def _recommend_scene(draft: dict, background: Path) -> Scene:
    """こんな方におすすめ。1項目ずつチェックが入っていく。"""
    elements, at = _typed_block(
        [[("こんな方におすすめ", TEXT)]], LABEL_SIZE, 560, 0.1, left=MARGIN_LEFT
    )

    check = _check_mark()
    text_left = MARGIN_LEFT + CHECK_SIZE + CHECK_GAP
    top = 700
    for item in draft.get("recommend") or []:
        lines = colored_lines(item.get("text", ""), item.get("highlight", ""))
        size = fit_size(lines, BODY_SIZE, USABLE_WIDTH - CHECK_SIZE - CHECK_GAP)
        at += 0.12
        ascent, descent = _font(size).getmetrics()
        elements.append(
            Element(
                check,
                MARGIN_LEFT - 6,
                top + (ascent + descent) // 2 - check.height // 2,
                at,
            )
        )
        block, at = _typed_block(lines, size, top, at, left=text_left, gap=0.08)
        elements += block
        top += int(size * LINE_SPACING) * len(lines) + 44
    return Scene(background, at + 1.0, elements)


def _recent_thumbnails(exclude: str, count: int = 3) -> list[Path]:
    """直近の投稿の1枚目。締めの「他のレビュー」に並べる。"""
    if not IMG_DIR.exists():
        return []
    days = sorted((p for p in IMG_DIR.iterdir() if p.is_dir()), reverse=True)
    picked = []
    for day in days:
        if day.name == exclude:
            continue
        first = day / "01.jpg"
        if first.exists():
            picked.append(first)
        if len(picked) == count:
            break
    return picked


def _outro_scene(draft: dict, background: Path, account: dict) -> Scene:
    """締め。プロフィールへ送る。

    ここは読ませる場面ではなく、行き先を示す場面。文字を順番待ちさせると
    それだけで4秒かかるので、打ち出すのは「詳細レビューはプロフィールから
    チェック」の一文だけにして、アイコンと過去投稿は重ねて浮かせる。
    """
    elements, at = _typed_block([[("詳細レビューは", TEXT)]], BODY_SIZE + 20, 430, 0.1)
    block, at = _typed_block(
        [[("プロフィール", ACCENT)]], HOOK_MAX_SIZE - 12, 528, at + 0.05
    )
    elements += block

    icon_path = find_profile_icon()
    handle = account.get("handle", "")
    top = 690
    if icon_path is not None and handle:
        with Image.open(icon_path) as raw:
            icon = _circle(raw.convert("RGB"), 140)
        handle_image, _ = line_image([(handle, TEXT)], BODY_SIZE)
        pad = _pad(BODY_SIZE)
        width = icon.width + 20 + handle_image.width - pad * 2
        left = (STORY_WIDTH - width) // 2
        ascent, descent = _font(BODY_SIZE).getmetrics()
        elements.append(Element(icon, left, top, at))
        elements.append(
            Element(
                handle_image,
                left + icon.width + 20 - pad,
                top + icon.height // 2 - (pad + (ascent + descent) // 2),
                at + 0.08,
            )
        )
        top += icon.height + 26

    block, at = _typed_block([[("からチェック", TEXT)]], BODY_SIZE + 20, top, at + 0.15)
    elements += block
    top += 106

    guide, _ = line_image([(("↓他のレビューはこちら↓"), TEXT)], LABEL_SIZE)
    elements.append(
        Element(guide, (STORY_WIDTH - guide.width) // 2, top - _pad(LABEL_SIZE), at + 0.1)
    )
    top += 84

    # 過去の投稿を並べて、プロフィールに何があるかを見せる。1枚目のカードを
    # そのまま縮めるので、追加の素材は要らない。
    for index, path in enumerate(_recent_thumbnails(draft.get("date", ""))):
        with Image.open(path) as raw:
            thumb = raw.convert("RGB").resize((250, 313), Image.LANCZOS)
        framed = ImageOps.expand(thumb, border=5, fill=(255, 255, 255))
        shaped = _with_shadow(framed, spread=18).rotate(
            (index - 1) * -5, expand=True, resample=Image.BICUBIC
        )
        elements.append(
            Element(
                shaped,
                STORY_WIDTH // 2 - shaped.width // 2 + (index - 1) * 224,
                top - (24 if index == 1 else 0),
                at + 0.2 + 0.1 * index,
            )
        )
        at = max(at, at + 0.2 + 0.1 * index)
    return Scene(background, at + 1.4, elements, zoom_in=False)


def build_scenes(draft: dict) -> list[Scene]:
    """下書きから場面を組み立てる。"""
    account = load_account()
    seed = draft.get("book_title", "") or draft.get("date", "")
    backgrounds = _pick_backgrounds(seed, 4)
    return [
        _hook_scene(draft, backgrounds[0], account),
        _book_scene(draft, backgrounds[1]),
        _recommend_scene(draft, backgrounds[2]),
        _outro_scene(draft, backgrounds[3], account),
    ]


def total_seconds(scenes: Sequence[Scene]) -> float:
    return sum(scene.seconds for scene in scenes)


def build_motion_reel(draft: dict, out_path: Path) -> Path:
    """下書きから、動きのあるリールを書き出す。"""
    scenes = build_scenes(draft)
    return encode_frames(lambda: iter_frames(scenes), out_path)
