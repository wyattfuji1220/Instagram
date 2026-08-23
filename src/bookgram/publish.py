"""Instagram Graph API へのカルーセル投稿。

手順は3段階:
  1. 画像ごとにアイテムコンテナを作る (is_carousel_item=true)
  2. それらを束ねたカルーセルコンテナを作る (media_type=CAROUSEL)
  3. コンテナを公開する (media_publish)

画像は公開HTTPS URLである必要があるため、GitHub Pages 上のURLを渡す。
"""

from __future__ import annotations

import json
import time
from datetime import date
from dataclasses import dataclass
from typing import Any

import requests

NEWLINE = chr(10)
DEFAULT_HOST = "https://graph.facebook.com"
TIMEOUT = 60
STATUS_POLL_INTERVAL = 5
STATUS_POLL_MAX = 24  # 最大2分待つ
# 動画は変換が入るぶん待たされる。画像と同じ2分では足りない。
REEL_POLL_MAX = 72  # 最大6分待つ


# リールのサムネイルに使う位置（ミリ秒）。2枚目の表示中に当たる値。
REEL_THUMB_MS = 3000

# 投稿一覧で取る基本項目。insights と違い、ここは権限なしでも読める。
MEDIA_FIELDS = (
    "id,media_type,media_product_type,caption,permalink,"
    "like_count,comments_count,timestamp"
)


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

    def wait_until_ready(self, creation_id: str, max_polls: int = STATUS_POLL_MAX) -> None:
        for _ in range(max_polls):
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

    def search_audio(
        self, query: str = "", *, audio_type: str = "music", limit: int = 25
    ) -> list[dict[str, Any]]:
        """Instagram の音源ライブラリを検索する。

        検索語を省くとトレンドが返る。Facebook ログイン方式でしか使えない。
        返るのは第三者利用が許諾された曲だけで、アプリ内の全曲ではない。
        """
        params: dict[str, Any] = {
            "audio_type": audio_type,
            "user_id": self.ig_user_id,
            "limit": limit,
        }
        if query:
            params["search_query"] = query
        # 他のエッジと違い、一覧は data ではなく audio に入って返る
        return self._get("ig_audio", params).get("audio", [])

    def recent_media(
        self, limit: int = 25, fields: str = MEDIA_FIELDS
    ) -> list[dict[str, Any]]:
        """自分の投稿を新しい順に返す。"""
        return self._get(
            f"{self.ig_user_id}/media", {"fields": fields, "limit": limit}
        ).get("data", [])

    def insights(
        self, obj_id: str, metrics: list[str], **extra: Any
    ) -> dict[str, int]:
        """指標を取る。使えないものは黙って落とす。

        Graph API は指標名が1つでも無効だと呼び出し全体を蹴る。指標の
        提供状況はアカウント種別やAPIバージョンで変わるので、まとめて
        要求して駄目なら1つずつ試し、取れた分だけ返す。
        """
        try:
            return self._insight_rows(obj_id, metrics, extra)
        except PublishError:
            pass
        out: dict[str, int] = {}
        for metric in metrics:
            try:
                out.update(self._insight_rows(obj_id, [metric], extra))
            except PublishError:
                continue
        return out

    def _insight_rows(
        self, obj_id: str, metrics: list[str], extra: dict[str, Any]
    ) -> dict[str, int]:
        params = {"metric": ",".join(metrics), **extra}
        rows = self._get(f"{obj_id}/insights", params).get("data", [])
        out: dict[str, int] = {}
        for row in rows:
            name = row.get("name")
            if not name:
                continue
            # 期間指定の指標は values に、合計指定の指標は total_value に入る
            if "total_value" in row:
                value = row["total_value"].get("value")
            else:
                values = row.get("values") or [{}]
                value = values[-1].get("value")
            if isinstance(value, int):
                out[name] = value
        return out

    def create_reel(
        self,
        video_url: str,
        caption: str,
        audio_configuration: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            # フィードにも出す。リーチを取りに行くのがリールの目的なので、
            # プロフィールグリッドに並ぶことより露出を優先する。
            "share_to_feed": "true",
            # 動画は結論のカードから始めるが、それはカルーセルの1枚目と同じ絵
            # なので、先頭をサムネイルにするとプロフィールに同じ絵が2つ並ぶ。
            # 2枚目（問いかけ）の途中を選び、グリッド上で重複させない。
            "thumb_offset": str(REEL_THUMB_MS),
        }
        if audio_configuration:
            payload["audio_configuration"] = json.dumps(audio_configuration)
        result = self._post(f"{self.ig_user_id}/media", payload)
        return result["id"]

    def create_story(self, image_url: str) -> str:
        result = self._post(
            f"{self.ig_user_id}/media",
            {"image_url": image_url, "media_type": "STORIES"},
        )
        return result["id"]

    def account_info(self) -> dict[str, Any]:
        """接続確認用にアカウント名を取得する。"""
        return self._get(self.ig_user_id, {"fields": "username,name"})

    def whoami(self) -> dict[str, Any]:
        """トークンだけで自分のアカウント情報を引く（IG_USER_ID の取得用）。"""
        response = requests.get(
            f"{self.base}/me",
            params={
                "fields": "user_id,username",
                "access_token": self.access_token,
            },
            timeout=TIMEOUT,
        )
        return self._unwrap(response)

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


def exchange_long_lived(
    host: str, version: str, app_id: str, app_secret: str, token: str
) -> tuple[str, int]:
    """短期・長期どちらのユーザートークンでも、新しい長期トークンに引き換える。

    返り値は (トークン, 残り日数)。Facebook ログイン方式の長期トークンは
    60日で切れるので、切れる前に呼び直して延命する。
    """
    response = requests.get(
        f"{host}/{version}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        },
        timeout=TIMEOUT,
    )
    data = InstagramClient._unwrap(response)
    if "access_token" not in data:
        raise PublishError("長期トークンへの交換に失敗しました。")
    return data["access_token"], int(data.get("expires_in", 0)) // 86400


def token_expiry_days(
    host: str, version: str, app_id: str, app_secret: str, token: str
) -> int | None:
    """トークンの残り日数。取得できなければ None。"""
    response = requests.get(
        f"{host}/{version}/debug_token",
        params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
        timeout=TIMEOUT,
    )
    try:
        data = InstagramClient._unwrap(response).get("data", {})
    except PublishError:
        return None
    expires_at = data.get("expires_at")
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


def publish_reel(
    client: InstagramClient,
    video_url: str,
    caption: str,
    audio_configuration: dict[str, Any] | None = None,
) -> str:
    """リールを投稿して media_id を返す。"""
    verify_images_public([video_url])
    creation_id = client.create_reel(video_url, caption, audio_configuration)
    client.wait_until_ready(creation_id, REEL_POLL_MAX)
    return client.publish(creation_id)


def publish_story(client: InstagramClient, image_url: str) -> str:
    """ストーリーを投稿して media_id を返す。"""
    verify_images_public([image_url])
    creation_id = client.create_story(image_url)
    client.wait_until_ready(creation_id)
    return client.publish(creation_id)


def caption_for(draft: dict[str, Any]) -> str:
    """下書きの種別に応じたキャプションを組み立てる。"""
    return build_caption(draft)


# 生成側が書いていた定型の締め。問いかけはシステム側で付け直すので落とす。
BOILERPLATE_CLOSINGS = (
    "おすすめな本があれば",
    "おすすめの本があれば",
)


def strip_boilerplate_closing(body: str) -> str:
    """本文末尾の定型のコメント誘導を取り除く。"""
    trimmed = body.strip()
    for phrase in BOILERPLATE_CLOSINGS:
        index = trimmed.find(phrase)
        if index != -1:
            trimmed = trimmed[:index].rstrip()
    return trimmed


def closing_question(draft: dict[str, Any], account: dict[str, Any]) -> str:
    """締めに入れる問いかけ。日付で順に回すので連日同じにはならない。

    コメントはいいねよりアルゴリズムに効くが、答えるのに手間がかかる問いは
    返ってこない。1秒で答えが決まるものだけを account.yaml に並べてある。
    """
    questions = account.get("closing_questions") or []
    if not questions:
        return ""
    try:
        ordinal = date.fromisoformat(draft.get("date", "")).toordinal()
    except ValueError:
        ordinal = 0
    return questions[ordinal % len(questions)]


def genre_of(draft: dict[str, Any]) -> str:
    """この投稿のジャンル。ハッシュタグの出し分けに使う。

    特集は種別をそのまま持っている。日々の書籍投稿はキューがビジネス書の
    ジャンルから引いているので business とみなす。
    """
    return draft.get("feature_kind") or "business"


def build_caption(draft: dict[str, Any]) -> str:
    """本文・問いかけ・アカウント紹介・ハッシュタグを組み立てる。

    定型部分は account.yaml から読む。
    """
    from .config import load_account

    account = load_account()
    tags = list(account.get("fixed_hashtags", []))
    tags += account.get("genre_hashtags", {}).get(genre_of(draft), [])
    for tag in draft.get("hashtags", []):
        tag = tag if tag.startswith("#") else f"#{tag}"
        if tag not in tags:
            tags.append(tag)

    body = strip_boilerplate_closing(draft["caption"])
    question = closing_question(draft, account)

    return NEWLINE.join(
        [
            body,
            *(["", question] if question else []),
            "",
            "-------------------------------",
            "",
            # カード側は2行に割るが、キャプションでは1行に戻す
            account["name"].replace(NEWLINE, "　"),
            account["tagline"],
            "",
            "",
            " ".join(tags),
        ]
    )
