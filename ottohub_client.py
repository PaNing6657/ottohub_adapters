import json
from pathlib import Path
from typing import Any

import aiohttp

from astrbot import logger


class OTTOhubClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.ottohub.cn",
        token: str | None = None,
        timeout: int = 30,
        upload_timeout: int = 60,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self._http_session: aiohttp.ClientSession | None = None

    async def ensure_http_session(self) -> None:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def login(self, uid_email: str, pw: str) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        url = f"{self.base_url}/api/auth/login"
        payload = {"uid_email": uid_email, "pw": pw}

        async with self._http_session.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"login failed: {resp.status} {text}")
            data = json.loads(text)
            if data.get("status") == "error":
                raise RuntimeError(f"login error: {data.get('message')}")
            return data

    async def request_get(
        self,
        module: str,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        query_params = {"module": module, "action": action}
        if self.token:
            query_params["token"] = self.token
        if params:
            query_params.update(params)

        url = self.base_url
        async with self._http_session.get(url, params=query_params) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(
                    f"request {module}.{action} failed: {resp.status} {text[:500]}"
                )
            result = json.loads(text)
            if result.get("status") == "error":
                raise RuntimeError(
                    f"request {module}.{action} error: {result.get('message')}"
                )
            return result

    async def get_unread_count(self) -> int:
        result = await self.request_get("im", "new_message_num")
        return result.get("new_message_num", 0)

    async def get_unread_messages(
        self, offset: int = 0, num: int = 20
    ) -> list[dict[str, Any]]:
        result = await self.request_get(
            "im", "unread_message_list", {"offset": offset, "num": num}
        )
        return result.get("unread_message_list", [])

    async def mark_message_read(self, msg_id: str) -> dict[str, Any]:
        return await self.request_get("im", "read_message", {"msg_id": msg_id})

    async def send_message(self, receiver: str, message: str) -> dict[str, Any]:
        return await self.request_get(
            "im", "send_message", {"receiver": receiver, "message": message}
        )

    async def get_friend_list(
        self, offset: int = 0, num: int = 20
    ) -> list[dict[str, Any]]:
        result = await self.request_get(
            "im", "friend_list", {"offset": offset, "num": num}
        )
        return result.get("user_list", [])

    async def get_friend_messages(
        self, friend_uid: str, offset: int = 0, num: int = 20
    ) -> list[dict[str, Any]]:
        result = await self.request_get(
            "im",
            "friend_message",
            {"friend_uid": friend_uid, "offset": offset, "num": num},
        )
        return result.get("message_list", [])

    async def get_user_profile(self) -> dict[str, Any]:
        return await self.request_get("profile", "user_profile")

    async def get_user_detail(self, uid: str) -> dict[str, Any]:
        result = await self.request_get("user", "get_user_detail", {"uid": uid})
        return result

    async def upload_image(self, file_path: str) -> str | None:
        await self.ensure_http_session()
        assert self._http_session is not None

        logger.info(f"[OTTOhub] upload_image called with: {file_path}")

        url = f"{self.base_url}/module/creator/submit_image.php"
        file_path_obj = Path(file_path)

        if not file_path_obj.exists():
            logger.warning(f"[OTTOhub] File not exists: {file_path}")
            return None

        logger.info(f"[OTTOhub] Uploading file: {file_path_obj.name}")

        with open(file_path_obj, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("action", "submit_image")
            if self.token:
                data.add_field("token", self.token)
            data.add_field(
                "file_img",
                f,
                filename=file_path_obj.name,
                content_type=self._guess_content_type(file_path_obj.suffix),
            )

            upload_timeout = aiohttp.ClientTimeout(total=self.upload_timeout)
            async with self._http_session.post(url, data=data, timeout=upload_timeout) as resp:
                text = await resp.text()
                logger.info(f"[OTTOhub] Upload response status: {resp.status}")
                logger.info(f"[OTTOhub] Upload response body: {text[:500]}")
                if resp.status >= 400:
                    return None
                try:
                    result = json.loads(text)
                    logger.info(f"[OTTOhub] Upload result: {result}")
                    return result.get("image_url")
                except json.JSONDecodeError as e:
                    logger.error(f"[OTTOhub] JSON decode error: {e}")
                    return None

    @staticmethod
    def _guess_content_type(ext: str) -> str:
        ext = ext.lower()
        mapping = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        return mapping.get(ext, "application/octet-stream")

    async def get_blog_detail(self, bid: str) -> dict[str, Any]:
        return await self.request_get("blog", "get_blog_detail", {"bid": bid})

    async def get_video_detail(self, vid: str) -> dict[str, Any]:
        return await self.request_get("video", "get_video_detail", {"vid": vid})

    async def get_blog_comments(
        self,
        bid: str,
        parent_bcid: str = "0",
        offset: int = 0,
        num: int = 10,
        cid_asc: int | None = None,
    ) -> dict[str, Any]:
        req_params = {
            "bid": bid,
            "parent_bcid": parent_bcid,
            "offset": offset,
            "num": num,
        }
        if cid_asc is not None:
            req_params["cid_asc"] = cid_asc
        return await self.request_get(
            "comment",
            "blog_comment_list",
            req_params,
        )

    async def get_video_comments(
        self, vid: str, parent_vcid: str = "0", offset: int = 0, num: int = 10
    ) -> dict[str, Any]:
        return await self.request_get(
            "comment",
            "video_comment_list",
            {"vid": vid, "parent_vcid": parent_vcid, "offset": offset, "num": num},
        )

    async def reply_comment(
        self, bid: str, parent_bcid: str, content: str
    ) -> dict[str, Any]:
        logger.info(
            f"[OTTOhub Client] reply_comment: bid={bid}, parent_bcid={parent_bcid}, "
            f"content_len={len(content)}"
        )
        return await self.request_get(
            "comment",
            "comment_blog",
            {"bid": bid, "parent_bcid": parent_bcid, "content": content},
        )

    async def reply_video_comment(
        self, vid: str, parent_vcid: str, content: str
    ) -> dict[str, Any]:
        return await self.request_get(
            "comment",
            "comment_video",
            {"vid": vid, "parent_vcid": parent_vcid, "content": content},
        )
