import asyncio
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .ottohub_client import OTTOhubClient
from .ottohub_comment_event import OTTOhubCommentPlatformEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


@register_platform_adapter(
    "OTTOhub评论",
    "OTTOhub评论",
    default_config_tmpl={
        "enable": True,
        "用户ID": "",
        "密码": "",
        "API地址": "https://api.ottohub.cn",
        "轮询间隔": 5,
        "上传超时": 60,
    },
    logo_path="logo.png",
    support_streaming_message=False,
)
class OTTOhubCommentPlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings
        self.client: OTTOhubClient | None = None
        self.bot_self_id: str | None = None
        self._polling_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()
        self._processed_ids: set[str] = set()
        self._data_dir = Path(get_astrbot_data_path()) / "ottohub_comment"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._processed_file = self._data_dir / "processed_ids.json"
        self._load_processed_ids()
        self._session_meta: dict[str, dict[str, Any]] = {}

    def _load_processed_ids(self) -> None:
        if self._processed_file.exists():
            try:
                self._processed_ids = set(json.loads(self._processed_file.read_text()))
            except Exception:
                self._processed_ids = set()

    def _save_processed_ids(self) -> None:
        try:
            self._processed_file.write_text(json.dumps(list(self._processed_ids)))
        except Exception as e:
            logger.warning(f"[OTTOhub Cmt] Save ids failed: {e}")

    @override
    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            "OTTOhub评论",
            "OTTOhub评论",
            id=cast(str, self.config.get("id")),
            default_config_tmpl=self.config,
            support_proactive_message=False,
        )

    @override
    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        logger.info(
            f"[OTTOhub Cmt] send_by_session ENTRY, session={session.session_id}, "
            f"chain_len={len(message_chain.chain)}"
        )
        if not self.client:
            await super().send_by_session(session, message_chain)
            return

        parts = session.session_id.split(":")
        if len(parts) < 4 or parts[0] != "ottohub_cmt":
            await super().send_by_session(session, message_chain)
            return

        cmt_type = parts[1]
        object_id = parts[2]
        parent_cid = parts[3]

        reply_parts = []
        for segment in message_chain.chain:
            if isinstance(segment, Plain):
                reply_parts.append(segment.text)

        reply_text = "".join(reply_parts).strip()
        if not reply_text:
            await super().send_by_session(session, message_chain)
            return

        meta = getattr(self, "_session_meta", {}).pop(session.session_id, {})
        comment_author = meta.get("comment_author", "")
        if comment_author:
            reply_text = f"@{comment_author} {reply_text}"

        chunks = [reply_text[i : i + 400] for i in range(0, len(reply_text), 400)]
        logger.info(
            f"[OTTOhub Cmt] Split {len(reply_text)} chars into {len(chunks)} chunk(s)"
        )
        for idx, chunk in enumerate(chunks):
            for attempt in range(2):
                try:
                    if cmt_type == "blog":
                        await self.client.reply_comment(object_id, parent_cid, chunk)
                    elif cmt_type == "video":
                        await self.client.reply_video_comment(
                            object_id, parent_cid, chunk
                        )
                    logger.info(f"[OTTOhub Cmt] Chunk {idx+1}/{len(chunks)} sent")
                    break
                except RuntimeError as e:
                    if "too_many_requests" in str(e) and attempt < 1:
                        await asyncio.sleep(10)
                    else:
                        raise
            if idx < len(chunks) - 1:
                await asyncio.sleep(10)

        await super().send_by_session(session, message_chain)

    @override
    async def run(self) -> None:
        uid = self.config.get("用户ID")
        pw = self.config.get("密码")
        api_base_url = self.config.get("API地址", "https://api.ottohub.cn")

        if not uid or not pw:
            logger.error("[OTTOhub Cmt] 用户ID或密码未配置")
            return

        logger.info("[OTTOhub Cmt] Starting comment reply adapter...")
        self.client = OTTOhubClient(
            base_url=api_base_url,
            upload_timeout=self.config.get("上传超时", 60),
        )

        try:
            login_result = await self.client.login(str(uid), str(pw))
            self.client.token = login_result.get("token")
            logger.info(f"[OTTOhub Cmt] Login success: uid={login_result.get('uid')}")
            self.bot_self_id = str(login_result.get("uid"))
        except Exception as e:
            logger.error(f"[OTTOhub Cmt] Login failed: {e}")
            return

        self._polling_task = asyncio.create_task(self._polling_loop())
        await self._shutdown_event.wait()

    async def _polling_loop(self) -> None:
        poll_interval = self.config.get("轮询间隔", 5)
        while not self._shutdown_event.is_set():
            try:
                await self._check_and_process()
            except Exception as e:
                logger.error(f"[OTTOhub Cmt] Poll error: {e}", exc_info=True)
            await asyncio.sleep(poll_interval)

    async def _check_and_process(self) -> None:
        if not self.client:
            return

        messages = await self.client.get_friend_messages(
            friend_uid="0", offset=0, num=10
        )

        for msg in messages:
            content = msg.get("content", "")
            msg_id = str(msg.get("msg_id", ""))

            if msg_id in self._processed_ids:
                continue
            if "@了你" not in content and "@了" not in content:
                continue

            logger.info(f"[OTTOhub Cmt] @message: {content[:120]}")
            await self._process_notification(msg, content, msg_id)

    async def _process_notification(
        self, raw_msg: dict[str, Any], content: str, msg_id: str
    ) -> None:
        is_video = "视频(VID:" in content
        is_blog = "动态(BID:" in content
        is_sub = "评论(BCID:" in content or "评论(VCID:" in content

        logger.info(
            f"[OTTOhub Cmt] Processing msg_id={msg_id}, "
            f"is_video={is_video}, is_blog={is_blog}, is_sub={is_sub}"
        )

        if is_video:
            id_match = re.search(r"VID:(\d+)", content)
            uid_match = re.search(r"UID:(\d+)", content)
            if id_match and uid_match:
                self._processed_ids.add(msg_id)
                noti_time = raw_msg.get("time", "")
                await self._handle_video(
                    msg_id,
                    id_match[1],
                    uid_match[1],
                    is_sub,
                    content,
                )
            else:
                self._processed_ids.add(msg_id)
        elif is_blog:
            id_match = re.search(r"BID:(\d+)", content)
            uid_match = re.search(r"UID:(\d+)", content)
            if id_match and uid_match:
                self._processed_ids.add(msg_id)
                noti_time = raw_msg.get("time", "")
                await self._handle_blog(
                    msg_id,
                    id_match[1],
                    uid_match[1],
                    is_sub,
                    content,
                    noti_time,
                )
            else:
                self._processed_ids.add(msg_id)
        else:
            self._processed_ids.add(msg_id)
        self._save_processed_ids()

    async def _handle_blog(
        self,
        msg_id: str,
        bid: str,
        uid: str,
        is_sub: bool,
        notification_text: str = "",
        noti_time: str = "",
    ) -> None:
        logger.info(f"[OTTOhub Cmt] Blog: BID={bid}, UID={uid}, is_sub={is_sub}")

        blog = await self.client.get_blog_detail(bid)
        if blog.get("status") != "success":
            return

        blog_title = blog.get("title", "")
        blog_content = self._truncate(blog.get("content", ""), 1000)
        blog_author = blog.get("username", "")

        if is_sub:
            await self._handle_sub_comment(
                msg_id,
                bid,
                uid,
                blog_title,
                blog_content,
                blog_author,
                notification_text,
                noti_time,
            )
        else:
            await self._handle_main_comment(
                msg_id, bid, uid, blog_title, blog_content, blog_author, noti_time
            )

    async def _find_blog_comment_paginated(
        self,
        bid: str,
        predicate: Callable[[dict], bool],
        parent_bcid: str = "0",
        cid_asc: int | None = None,
        page_size: int = 10,
    ) -> list[dict]:
        offset = 0
        while True:
            result = await self.client.get_blog_comments(
                bid,
                parent_bcid=parent_bcid,
                offset=offset,
                num=page_size,
                cid_asc=cid_asc,
            )
            if result.get("status") != "success":
                return []
            comment_list = result.get("comment_list", [])
            if not comment_list:
                return []
            matches = [c for c in comment_list if predicate(c)]
            if matches:
                return matches
            if len(comment_list) < page_size:
                return []
            offset += page_size

    async def _find_video_comment_paginated(
        self,
        vid: str,
        predicate: Callable[[dict], bool],
        parent_vcid: str = "0",
        page_size: int = 10,
    ) -> list[dict]:
        offset = 0
        while True:
            result = await self.client.get_video_comments(
                vid,
                parent_vcid=parent_vcid,
                offset=offset,
                num=page_size,
            )
            if result.get("status") != "success":
                return []
            comment_list = result.get("comment_list", [])
            if not comment_list:
                return []
            matches = [c for c in comment_list if predicate(c)]
            if matches:
                return matches
            if len(comment_list) < page_size:
                return []
            offset += page_size

    def _select_best_candidate(self, candidates: list[dict], noti_time: str) -> dict:
        target = candidates[0]
        if noti_time and len(candidates) > 1:
            noti_dt = self._parse_time(noti_time)
            if noti_dt:
                min_diff = abs(
                    (self._parse_time(target.get("time", "")) or noti_dt) - noti_dt
                ).total_seconds()
                for c in candidates[1:]:
                    c_dt = self._parse_time(c.get("time", ""))
                    if c_dt:
                        diff = abs(c_dt - noti_dt).total_seconds()
                        if diff < min_diff:
                            min_diff = diff
                            target = c
        return target

    async def _find_sub_comment_by_time(
        self,
        bid: str,
        parent_bcid: str,
        noti_time: str,
        page_size: int = 10,
    ) -> dict | None:
        noti_dt = self._parse_time(noti_time)
        if not noti_dt:
            return None

        offset = 0
        while True:
            result = await self.client.get_blog_comments(
                bid,
                parent_bcid=parent_bcid,
                offset=offset,
                num=page_size,
                cid_asc=1,
            )
            if result.get("status") != "success":
                return None
            comment_list = result.get("comment_list", [])
            if not comment_list:
                return None

            best_in_page = None
            best_diff = 11.0
            for c in comment_list:
                c_dt = self._parse_time(c.get("time", ""))
                if c_dt:
                    diff = abs((c_dt - noti_dt).total_seconds())
                    if diff <= 10 and diff < best_diff:
                        best_diff = diff
                        best_in_page = c

            if best_in_page:
                return best_in_page
            if len(comment_list) < page_size:
                return None
            offset += page_size

    async def _handle_main_comment(
        self,
        msg_id: str,
        bid: str,
        uid: str,
        blog_title: str,
        blog_content: str,
        blog_author: str,
        noti_time: str = "",
    ) -> None:
        candidates = await self._find_blog_comment_paginated(
            bid,
            lambda c: (
                str(c.get("uid", "")) == uid and "@AICaoMei" in c.get("content", "")
            ),
        )
        if not candidates:
            return

        target = self._select_best_candidate(candidates, noti_time)

        comment_text = target.get("content", "")
        parent_bcid = target.get("bcid", 0)
        comment_author = (
            target.get("sender_name") or target.get("username") or "未知用户"
        )

        images = self._collect_images([blog_content, comment_text])

        self._build_and_commit(
            msg_id,
            "blog",
            bid,
            str(parent_bcid),
            comment_author,
            uid,
            (
                f"【动态原文】\n"
                f"作者：{blog_author}\n"
                f"标题：{blog_title}\n"
                f"内容：{blog_content}\n\n"
                f"【他人评论】\n"
                f"{comment_author}：{comment_text}\n\n"
                f"请针对以上评论输出回复。"
            ),
            {"type": "blog", "bid": bid, "parent_bcid": parent_bcid},
            images,
        )

    async def _handle_sub_comment(
        self,
        msg_id: str,
        bid: str,
        uid: str,
        blog_title: str,
        blog_content: str,
        blog_author: str,
        notification_text: str,
        noti_time: str = "",
    ) -> None:
        parent_bcid_match = re.search(r"BCID:(\d+)", notification_text)
        if not parent_bcid_match:
            return
        parent_bcid = parent_bcid_match[1]

        target = await self._find_sub_comment_by_time(
            bid,
            parent_bcid,
            noti_time,
        )
        if not target:
            return

        target_bcid = target.get("bcid", 0)
        comment_text = target.get("content", "")
        comment_author = (
            target.get("sender_name") or target.get("username") or "未知用户"
        )

        logger.info(
            f"[OTTOhub Cmt] _handle_sub_comment: bid={bid}, "
            f"parent_bcid(from_noti)={parent_bcid}, target_bcid={target_bcid}, "
            f"noti_time={noti_time}, target_keys={list(target.keys())[:10]}"
        )

        if not target_bcid:
            logger.warning(
                f"[OTTOhub Cmt] target_bcid is 0/empty, using parent_bcid={parent_bcid}"
            )
            reply_bcid = parent_bcid
            is_sub = False
        else:
            reply_bcid = str(target_bcid)
            is_sub = True

        all_contents = [blog_content, comment_text]
        images = self._collect_images(all_contents)

        self._build_and_commit(
            msg_id,
            "blog",
            bid,
            reply_bcid,
            comment_author,
            uid,
            comment_text,
            {
                "type": "blog",
                "bid": bid,
                "parent_bcid": reply_bcid,
                "main_bcid": parent_bcid,
                "is_sub": is_sub,
            },
            images,
        )

    def _build_and_commit(
        self,
        msg_id: str,
        cmt_type: str,
        object_id: str,
        parent_cid: str,
        comment_author: str,
        sender_uid: str,
        message_str: str,
        raw_info: dict[str, Any],
        image_urls: list[str] | None = None,
    ) -> None:
        session_id = f"ottohub_cmt:{cmt_type}:{object_id}:{parent_cid}"

        raw_info["comment_author"] = comment_author

        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.message_str = message_str
        abm.sender = MessageMember(user_id=sender_uid, nickname=comment_author)

        message_chain_items = [Plain(text=message_str)]
        if image_urls:
            for url in image_urls:
                message_chain_items.append(Image(file=url))
        abm.message = message_chain_items
        abm.raw_message = raw_info
        abm.self_id = cast(str, self.bot_self_id)
        abm.session_id = session_id
        abm.message_id = msg_id

        self._session_meta[session_id] = {
            "comment_author": comment_author,
            "is_sub": raw_info.get("is_sub", False),
        }

        event = OTTOhubCommentPlatformEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=session_id,
            platform=self,
        )
        self.commit_event(event)

    async def _handle_video(
        self,
        msg_id: str,
        vid: str,
        uid: str,
        is_sub: bool,
        notification_text: str = "",
    ) -> None:
        logger.info(f"[OTTOhub Cmt] Video: VID={vid}, UID={uid}, is_sub={is_sub}")

        video = await self.client.get_video_detail(vid)
        if video.get("status") != "success":
            return

        candidates = await self._find_video_comment_paginated(
            vid,
            lambda c: (
                str(c.get("uid", "")) == uid and "@AICaoMei" in c.get("content", "")
            ),
        )
        if not candidates:
            return

        target = candidates[0]

        video_title = video.get("title", "")
        video_intro = self._truncate(video.get("intro", ""), 1000)
        video_author = video.get("username", "")
        comment_text = target.get("content", "")
        parent_vcid = target.get("vcid", 0)
        comment_author = (
            target.get("sender_name") or target.get("username") or "未知用户"
        )

        images = self._collect_images([video_intro, comment_text])

        self._build_and_commit(
            msg_id,
            "video",
            vid,
            str(parent_vcid),
            comment_author,
            uid,
            (
                f"【视频信息】\n"
                f"作者：{video_author}\n"
                f"标题：{video_title}\n"
                f"简介：{video_intro}\n\n"
                f"【他人评论】\n"
                f"{comment_author}：{comment_text}\n\n"
                f"请针对以上评论输出回复。"
            ),
            {"type": "video", "vid": vid, "parent_vcid": parent_vcid},
            images,
        )

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    @staticmethod
    def _parse_time(time_str: str) -> datetime | None:
        if not time_str:
            return None
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                return datetime.fromtimestamp(int(time_str))
            except (ValueError, TypeError):
                return None

    @staticmethod
    def _extract_image_urls(text: str) -> list[str]:
        pattern = r"!\[.*?\]\((https?://[^\)]+)\)"
        urls = re.findall(pattern, text)
        urls += re.findall(
            r"(https?://[^\s\)\]]+\.(?:jpg|jpeg|png|gif|webp))", text, re.I
        )
        return list(dict.fromkeys(urls))

    @classmethod
    def _collect_images(cls, texts: list[str]) -> list[str]:
        seen = set()
        result = []
        for text in texts:
            for url in cls._extract_image_urls(text):
                if url not in seen:
                    seen.add(url)
                    result.append(url)
        return result

    @override
    async def terminate(self) -> None:
        logger.info("[OTTOhub Cmt] Shutting down...")
        self._shutdown_event.set()
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await asyncio.wait_for(self._polling_task, timeout=5)
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.close()
        logger.info("[OTTOhub Cmt] Shutdown complete.")
