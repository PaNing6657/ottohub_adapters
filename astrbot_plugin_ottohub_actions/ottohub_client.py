"""OTTOhub API 客户端(社交能力插件自包含版本)。

基于项目 api/ 目录下 REST API 文档实现:
- user_api.md      用户详情 / 用户搜索
- blog_api.md      发动态 / 动态详情 / 动态搜索 / 删除动态
- following_api.md 关注 / 取关 / 关注状态
- im_api.md        私信发送 / 会话列表 / 未读数

特性:
- 自动携带 token;收到 401(error_token)时通过 relogin_cb 重新登录并重试一次
- 写入接口(form)与 JSON 接口分别处理
"""

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

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
        relogin_cb: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.relogin_cb = relogin_cb
        self._http_session: aiohttp.ClientSession | None = None

    async def ensure_http_session(self) -> None:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # ---------------------------------------------------------------- 认证

    async def login(self, uid_email: str, pw: str) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        url = f"{self.base_url}/api/auth/login"
        payload = {"uid_email": uid_email, "pw": pw}

        async with self._http_session.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"login failed: {resp.status} {text[:500]}")
            data = json.loads(text)
            if data.get("status") == "error":
                raise RuntimeError(f"login error: {data.get('message')}")
            return data

    async def _try_relogin(self) -> bool:
        """token 失效时尝试重登录;成功返回 True。"""
        if self.relogin_cb is None:
            return False
        try:
            await self.relogin_cb()
            return bool(self.token)
        except Exception as e:
            logger.warning(f"[OTTOhub] relogin failed: {e}")
            return False

    @staticmethod
    def _merge_result(result: dict[str, Any]) -> dict[str, Any]:
        data = result.get("data", {})
        merged = dict(data) if isinstance(data, dict) else {}
        for k, v in result.items():
            if k not in ("status", "message", "data"):
                merged[k] = v
        return {"status": "success", **merged}

    # ------------------------------------------------------------ REST 基础

    async def _rest_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        all_params: dict[str, Any] = {}
        if self.token:
            all_params["token"] = self.token
        if params:
            all_params.update(params)

        url = f"{self.base_url}{path}"
        async with self._http_session.get(url, params=all_params) as resp:
            text = await resp.text()
            if resp.status == 401 and not _retried and await self._try_relogin():
                return await self._rest_get(path, params, _retried=True)
            if resp.status >= 400:
                raise RuntimeError(f"request {path} failed: {resp.status} {text[:500]}")
            result = json.loads(text)
            if result.get("status") == "error":
                raise RuntimeError(f"request {path} error: {result.get('message')}")
            return self._merge_result(result)

    async def _rest_post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        payload: dict[str, Any] = {}
        if self.token:
            payload["token"] = self.token
        if body:
            payload.update(body)

        url = f"{self.base_url}{path}"
        async with self._http_session.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status == 401 and not _retried and await self._try_relogin():
                return await self._rest_post(path, body, _retried=True)
            if resp.status >= 400:
                raise RuntimeError(f"request {path} failed: {resp.status} {text[:500]}")
            result = json.loads(text)
            if result.get("status") == "error":
                raise RuntimeError(f"request {path} error: {result.get('message')}")
            return self._merge_result(result)

    async def _rest_delete(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_http_session()
        assert self._http_session is not None

        payload: dict[str, Any] = {}
        if self.token:
            payload["token"] = self.token
        if body:
            payload.update(body)

        url = f"{self.base_url}{path}"
        async with self._http_session.delete(url, json=payload) as resp:
            text = await resp.text()
            if resp.status == 401 and not _retried and await self._try_relogin():
                return await self._rest_delete(path, body, _retried=True)
            if resp.status >= 400:
                raise RuntimeError(f"request {path} failed: {resp.status} {text[:500]}")
            result = json.loads(text)
            if result.get("status") == "error":
                raise RuntimeError(f"request {path} error: {result.get('message')}")
            return self._merge_result(result)

    async def _form_post(
        self,
        path: str,
        fields: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        """表单提交(用于 POST /api/blog/submit 等写入接口)。"""
        await self.ensure_http_session()
        assert self._http_session is not None

        data = aiohttp.FormData()
        if self.token:
            data.add_field("token", self.token)
        if fields:
            for k, v in fields.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    data.add_field(k, json.dumps(list(v), ensure_ascii=False))
                else:
                    data.add_field(k, str(v))

        url = f"{self.base_url}{path}"
        async with self._http_session.post(url, data=data) as resp:
            text = await resp.text()
            if resp.status == 401 and not _retried and await self._try_relogin():
                return await self._form_post(path, fields, _retried=True)
            if resp.status >= 400:
                raise RuntimeError(f"request {path} failed: {resp.status} {text[:500]}")
            result = json.loads(text)
            if result.get("status") == "error":
                raise RuntimeError(f"request {path} error: {result.get('message')}")
            return self._merge_result(result)

    # ------------------------------------------------------------ 用户模块

    async def get_user_detail(self, uid: str) -> dict[str, Any]:
        """GET /api/user/{uid} 用户详情。"""
        return await self._rest_get(f"/api/user/{uid}")

    async def search_users(
        self,
        search_term: str,
        offset: int = 0,
        num: int = 10,
    ) -> dict[str, Any]:
        """GET /api/user/search 搜索用户。"""
        return await self._rest_get(
            "/api/user/search",
            {"search_term": search_term, "offset": offset, "num": num},
        )

    # ------------------------------------------------------------ 动态模块

    async def submit_blog(
        self,
        title: str,
        content: str,
        *,
        tag: list[str] | None = None,
        copyright_type: int = 0,
        blog_type: int = 0,
        channel_id: int = 0,
        is_gore: int = 0,
        attached_vid: int = 0,
    ) -> dict[str, Any]:
        """POST /api/blog/submit 发布动态。"""
        fields: dict[str, Any] = {
            "title": title,
            "content": content,
            "channel_id": channel_id,
            "blog_type": blog_type,
            "copyright_type": copyright_type,
            "is_gore": is_gore,
        }
        if tag:
            fields["tag"] = tag
        if attached_vid:
            fields["attached_vid"] = attached_vid
        return await self._form_post("/api/blog/submit", fields)

    async def get_blog_detail(self, bid: str) -> dict[str, Any]:
        """GET /api/blog/{bid}/detail 动态详情。"""
        return await self._rest_get(f"/api/blog/{bid}/detail")

    async def get_blog_by_bid(self, bid: str) -> dict[str, Any] | None:
        """GET /api/blog/{bid} 指定动态;返回单条或 None。"""
        result = await self._rest_get(f"/api/blog/{bid}")
        blog_list = result.get("blog_list") or []
        return blog_list[0] if blog_list else None

    async def search_blogs(
        self,
        search_term: str,
        offset: int = 0,
        num: int = 10,
        *,
        uid: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/blog/search 搜索动态。

        sort 取值:bid_desc(最新) / view_count_desc(浏览) /
        like_count_desc(点赞) / favorite_count_desc(收藏);None 为默认相关度。
        """
        params: dict[str, Any] = {
            "search_term": search_term,
            "offset": offset,
            "num": num,
        }
        if uid:
            params["uid"] = uid
        if sort:
            params[sort] = 1
        return await self._rest_get("/api/blog/search", params)

    async def get_user_blogs(
        self,
        uid: str,
        offset: int = 0,
        num: int = 10,
    ) -> dict[str, Any]:
        """GET /api/blog/users/{uid}/blogs 指定用户的动态列表(按最新排序)。"""
        return await self._rest_get(
            f"/api/blog/users/{uid}/blogs",
            {"offset": offset, "num": num},
        )

    async def delete_blog(self, bid: str) -> dict[str, Any]:
        """DELETE /api/blog/{bid} 删除动态。"""
        return await self._rest_delete(f"/api/blog/{bid}")

    # ------------------------------------------------------------ 评论模块

    async def reply_blog_comment(
        self,
        bid: str,
        content: str,
        parent_bcid: str = "0",
    ) -> dict[str, Any]:
        """POST /api/comment/blogs/{bid} 发表动态评论/回复评论。

        parent_bcid 为 0 时评论动态本身;大于 0 时回复对应根评论。
        """
        return await self._rest_post(
            f"/api/comment/blogs/{bid}",
            {"parent_bcid": parent_bcid, "content": content},
        )

    # ------------------------------------------------------------ 关注模块

    async def follow_user(self, uid: str) -> dict[str, Any]:
        """POST /api/following/follow/{uid} 关注/取关(幂等切换)。"""
        return await self._rest_post(f"/api/following/follow/{uid}")

    async def get_follow_status(self, uid: str) -> dict[str, Any]:
        """GET /api/following/status/{uid} 关注状态。"""
        return await self._rest_get(f"/api/following/status/{uid}")

    # ------------------------------------------------------------ 私信模块

    async def send_message(self, receiver: str, message: str) -> dict[str, Any]:
        """POST /api/im/messages 发送私信,单条上限 222 字。"""
        return await self._rest_post(
            "/api/im/messages", {"receiver": receiver, "message": message}
        )

    async def get_unread_count(self) -> int:
        """GET /api/im/unread-count 未读私信数。"""
        result = await self._rest_get("/api/im/unread-count")
        return int(result.get("new_message_num", 0) or 0)

    async def get_conversations(
        self, offset: int = 0, num: int = 12
    ) -> list[dict[str, Any]]:
        """GET /api/im/conversations 会话列表。"""
        result = await self._rest_get(
            "/api/im/conversations", {"offset": offset, "num": num}
        )
        return result.get("user_list", [])

    # ------------------------------------------------------------ 图片上传

    async def upload_image(self, file_path: str) -> str | None:
        await self.ensure_http_session()
        assert self._http_session is not None

        url = f"{self.base_url}/api/image/upload"
        file_path_obj = Path(file_path)

        if not file_path_obj.exists():
            logger.warning(f"[OTTOhub] File not exists: {file_path}")
            return None

        with open(file_path_obj, "rb") as f:
            data = aiohttp.FormData()
            if self.token:
                data.add_field("token", self.token)
            data.add_field(
                "file_img",
                f,
                filename=file_path_obj.name,
                content_type=self._guess_content_type(file_path_obj.suffix),
            )

            upload_timeout = aiohttp.ClientTimeout(total=self.upload_timeout)
            async with self._http_session.post(
                url, data=data, timeout=upload_timeout
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    return None
                try:
                    result = json.loads(text)
                    return result.get("data", {}).get("image_url")
                except json.JSONDecodeError:
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
