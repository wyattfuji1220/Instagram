"""Instagram Graph API へのカルーセル投稿。

手順は3段階:
  1. 画像ごとにアイテムコンテナを作る (is_carousel_item=true)
  2. それらを束ねたカルーセルコンテナを作る (media_type=CAROUSEL)
  3. コンテナを公開する (media_publish)

画像は公開HTTPS URLである必要があるため、GitHub Pages 上のURLを渡す。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_HOST = "https://graph.facebook.com"
TIMEOUT = 60
STATUS_POLL_INTERVAL = 5
STATUS_POLL_MAX = 24  # 最大2分待つ


class PublishError(RuntimeError):
    pass


@dataclass
class InstagramClient:
    ig_user_id: str
    access_token: str
    api_version: str = "v23.0"
    host: str = DEFAULT_HOST

    @property
    def base(self) -> str:
        return f"{self.host}/{self.api_version}"

    def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {**params, "access_token": self.access_token}
        response = requests.post(f"{self.base}/{path}", data=payload, timeout=TIMEOUT)
        return self._unwrap(response)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {**params, "access_token": self.access_token}
        response = requests.get(f"{self.base}/{path}", params=payload, timeout=TIMEOUT)
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise PublishError(
                f"Graph API から不正な応答: HTTP {response.status_code} {response.text[:200]}"
            ) from None
        if "error" in data:
            error = data["error"]
            raise PublishError(
                f"Graph API エラー [{error.get('code')}] {error.get('message')} "
                f"(type={error.get('type')})"
            )
        return data

    def create_carousel_item(self, image_url: str) -> str:
        result = self._post(
            f"{self.ig_user_id}/media",
            {"image_url": image_url, "is_carousel_item": "true"},
        )
        return result["id"]

    def create_carousel(self, children: list[str], caption: str) -> str:
        result = self._post(
            f"{self.ig_user_id}/media",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
            },
        )
        return result["id"]

    def wait_until_ready(self, creation_id: str) -> None:
        for _ in range(STATUS_POLL_MAX):
            status = self._get(creation_id, {"fields": "status_code,status"})
            code = status.get("status_code")
            if code == "FINISHED":
                return
            if code == "ERROR":
                raise PublishError(f"コンテナ処理に失敗: {status.get('status')}")
            time.sleep(STATUS_POLL_INTERVAL)
        raise PublishError(f"コンテナ {creation_id} が時間内に FINISHED になりませんでした")

    def publish(self, creation_id: str) -> str:
        result = self._post(
            f"{self.ig_user_id}/media_publish", {"creation_id": creation_id}
        )
        return result["id"]

    def account_info(self) -> dict[str, Any]:
        """接続確認用にアカウント名を取得する。"""
        return self._get(self.ig_user_id, {"fields": "username,name"})

    def refresh_long_lived_token(self) -> dict[str, Any]:
        """長期トークンを延長する（Instagram Login 方式のみ）。

        graph.instagram.com を使っている場合のみ有効。
        Facebook Login 方式では別の手順が必要なため PublishError を投げる。
        """
        if "graph.instagram.com" not in self.host:
            raise PublishError(
                "自動更新は Instagram Login 方式 (IG_API_HOST=https://graph.instagram.com) "
                "でのみ利用できます。SETUP.md を参照してください。"
            )
        response = requests.get(
            f"{self.host}/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": self.access_token,
            },
            timeout=TIMEOUT,
        )
        return self._unwrap(response)

    def token_days_remaining(self) -> int | None:
        """アクセストークンの残り有効日数。取得できなければ None。"""
        try:
            data = self._get(
                "debug_token", {"input_token": self.access_token}
            )
        except PublishError:
            return None
        expires_at = data.get("data", {}).get("expires_at")
        if not expires_at:
            return None
        return max(0, int((expires_at - time.time()) // 86400))


def verify_images_public(image_urls: list[str]) -> None:
    """画像が公開URLとして到達可能か確認する。

    GitHub Pages のデプロイが遅れていると Graph API 側が 404 を踏むため、
    投稿前にここで止める。
    """
    for url in image_urls:
        try:
            response = requests.head(url, timeout=30, allow_redirects=True)
        except requests.RequestException as error:
            raise PublishError(f"画像URLに到達できません: {url} ({error})") from error
        if response.status_code != 200:
            raise PublishError(
                f"画像URLが公開されていません: {url} (HTTP {response.status_code})。"
                "GitHub Pages のデプロイが完了しているか確認してください。"
            )


def publish_carousel(
    client: InstagramClient, image_urls: list[str], caption: str
) -> str:
    """カルーセルを投稿して media_id を返す。"""
    if not 2 <= len(image_urls) <= 10:
        raise PublishError(f"カルーセルは2〜10枚である必要があります: {len(image_urls)}枚")

    verify_images_public(image_urls)
    children = [client.create_carousel_item(url) for url in image_urls]
    creation_id = client.create_carousel(children, caption)
    client.wait_until_ready(creation_id)
    return client.publish(creation_id)


def build_caption(draft: dict[str, Any]) -> str:
    """本文とハッシュタグを Instagram のキャプション形式に組み立てる。"""
    hashtags = " ".join(
        tag if tag.startswith("#") else f"#{tag}" for tag in draft.get("hashtags", [])
    )
    return f"{draft['caption'].strip()}\n\n.\n.\n.\n{hashtags}"
