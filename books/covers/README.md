# 手持ちの書影を置く場所

楽天に取扱いが無く openBD にも書影が無い本は、API からは永久に取れません
（絶版・在庫切れ）。その場合はここに画像を置くと、そちらが使われます。

## 置き方

ファイル名を **ISBN13桁 + 拡張子** にしてください。ハイフンは入れません。

```
books/covers/9784569841939.jpg
books/covers/9784815602505.png
```

対応する拡張子: .jpg / .jpeg / .png / .webp

ISBN は `books/queue.yaml` の該当書に書いてあるものと同じにします。
置いたあとは `python -m bookgram rerender --date YYYY-MM-DD` でカードを
描き直してください。`python -m bookgram covers` で反映を確認できます。

## 大きさ

カード上では 420×600px の枠に収まります。縦長で、幅600px以上あれば十分です。
小さすぎるとぼやけます。
