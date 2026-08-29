# 手持ちの書影を置く場所

楽天に取扱いが無く openBD にも書影が無い本は、API からは永久に取れません
（絶版・在庫切れ）。その場合はここに画像を置くと、そちらが使われます。

## 使い方

画像は `Image storage/` に放り込むだけで構いません。取り込みはこちらで
やります。

```bash
PYTHONPATH=src python -m bookgram covers --collect
```

ファイル名に ISBN13桁が入っていれば自動で振り分けます。スクリーンショットの
ような名前（image-1787989935907.png）の場合は、どの本か分からないので
一覧に出ます。その本の ISBN を指定してください。

```bash
PYTHONPATH=src python -m bookgram covers --collect \
  --assign "image-1787989935907.png=9784569841939"
```

取り込むとこの `books/covers/` に `{ISBN13}.{拡張子}` の名前で置かれます。
対応する拡張子: .jpg / .jpeg / .png / .webp

## 置いたあと

カードを描き直します。

```bash
PYTHONPATH=src python -m bookgram rerender --date YYYY-MM-DD
PYTHONPATH=src python -m bookgram covers          # 反映の確認
```

## 大きさ

カード上では 420×600px の枠に収まります。縦長で、幅600px以上あれば十分です。
小さすぎるとぼやけます。
