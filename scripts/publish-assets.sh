#!/usr/bin/env bash
# docs/img の中身を公開用リポジトリ（Instagram-assets）へ写す。
#
# Instagram Graph API はローカルファイルを受け取らないので、カードも動画も
# 公開URLで渡す必要がある。本体リポジトリは読書メモや原稿を抱えていて公開
# できないため、画像だけを公開側へ写して GitHub Pages から配信する。
#
# push イベントで動く publish-assets.yml からも、動画を作った直後の
# reel-post.yml からも、同じ手順を使う。ワークフロー側に手順を書き分けると
# 片方だけ直して食い違う。
#
# reel-post.yml から直接呼ぶ理由: GITHUB_TOKEN による push は新しい
# ワークフローを起こさないという GitHub の仕様がある。リールの動画は
# ワークフロー自身が push するので、publish-assets.yml は動かない。
# 待っても公開されないまま10分でタイムアウトし、9/06 の配信がそれで
# 止まった（動画は出来ていたが、公開URLに現れなかった）。
set -euo pipefail

if [ -z "${TOKEN:-}" ]; then
  echo "::error::ASSETS_TOKEN が設定されていません。"
  exit 1
fi

# 置き場の名前は assets を避ける。assets/ はカードの背景画像が入る既存の
# フォルダで、そこへ clone しようとして一度失敗している。
rm -rf .assets-repo
git clone --depth 1 \
  "https://x-access-token:${TOKEN}@github.com/wyattfuji1220/Instagram-assets.git" \
  .assets-repo

# 消えた日付フォルダも反映させたいので --delete で丸ごと合わせる。
rsync -a --delete docs/img/ .assets-repo/img/

cd .assets-repo
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "変更はありません。"
  exit 0
fi
git commit -m "画像を更新 ($(date -u -d '+9 hours' +%Y-%m-%d\ %H:%M))"
git push origin main
