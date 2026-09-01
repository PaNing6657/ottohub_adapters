import asyncio
import json
import re
import sys
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


def _cfg_number(value: Any, default: float, minimum: float = 0.1) -> float:
    """把配置项安全转换为数字。

    配置面板/配置文件可能把数值保存为字符串(如 "30"),
    直接传给 asyncio.sleep 等会抛 TypeError。
    转换失败(空值/非法值)时回退默认值,并保证不低于 minimum。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return max(minimum, num)


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
            upload_timeout=_cfg_number(self.config.get("上传超时", 60), 60),
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
        poll_interval = _cfg_number(self.config.get("轮询间隔", 5), 5)
        while not self._shutdown_event.is_set():
            try:
                await self._check_and_process()
            except Exception as e:
                logger.error(f"[OTTOhub Cmt] Poll error: {e}", exc_info=True)
            await asyncio.sleep(poll_interval)

    async def _check_and_process(self) -> None:
        if not self.client:
            return

        mentions = await self.client.get_mentions(offset=0, num=10, is_read=0)

        for mention in mentions:
            mid = str(mention.get("mid", ""))
            if not mid or mid in self._processed_ids:
                continue

            content_type = int(mention.get("content_type") or 0)
            context_type = int(mention.get("context_type") or 0)
            content_id = str(mention.get("content_id", ""))
            source_comment_id = str(mention.get("source_comment_id", ""))
            sender_uid = str(mention.get("sender_uid", ""))
            excerpt = str(mention.get("excerpt", ""))
            author_hint = str(mention.get("sender_username", ""))

            logger.info(
                f"[OTTOhub Cmt] Mention mid={mid}, content_type={content_type}, "
                f"context_type={context_type}, content_id={content_id}, "
                f"source_comment_id={source_comment_id}, sender_uid={sender_uid}"
            )

            if (
                content_type not in (1, 2)
                or context_type == 1
                or not content_id
                or not sender_uid
            ):
                logger.info(
                    f"[OTTOhub Cmt] Skip mention mid={mid}: content_type={content_type}, "
                    f"context_type={context_type}"
                )
                self._processed_ids.add(mid)
                self._save_processed_ids()
                await self._mark_mention_read(mid)
                continue

            try:
                if content_type == 1:
                    await self._handle_video(
                        mid,
                        content_id,
                        sender_uid,
                        context_type,
                        source_comment_id,
                        excerpt,
                        author_hint,
                    )
                elif content_type == 2:
                    await self._handle_blog(
                        mid,
                        content_id,
                        sender_uid,
                        context_type,
                        source_comment_id,
                        excerpt,
                        author_hint,
                    )
            except Exception as e:
                logger.error(
                    f"[OTTOhub Cmt] Failed to process mention mid={mid}: {e}",
                    exc_info=True,
                )
                continue

            self._processed_ids.add(mid)
            self._save_processed_ids()
            await self._mark_mention_read(mid)

    async def _mark_mention_read(self, mid: str) -> None:
        if not self.client:
            return
        try:
            await self.client.mark_mention_read(mid)
        except Exception as e:
            logger.warning(f"[OTTOhub Cmt] Failed to mark mention {mid} read: {e}")

    async def _handle_blog(
        self,
        msg_id: str,
        bid: str,
        uid: str,
        context_type: int,
        source_comment_id: str,
        notification_text: str = "",
        author_hint: str = "",
    ) -> None:
        logger.info(
            f"[OTTOhub Cmt] Blog: BID={bid}, UID={uid}, context_type={context_type}, "
            f"source_comment_id={source_comment_id}"
        )

        blog = await self.client.get_blog_detail(bid)
        if blog.get("status") != "success":
            return

        blog_title = blog.get("title", "")
        blog_content = self._truncate(blog.get("content", ""), 1000)
        blog_author = blog.get("username", "")

        # 定位要回复的评论：取其上方的上下文评论（最多10条），并确定实际回复目标
        context_comments, reply_parent = await self._locate_blog_comment_context(
            bid, source_comment_id, context_type
        )

        is_sub = context_type == 3
        comment_text = notification_text or "（无内容）"
        comment_author = author_hint or "未知用户"
        context_str = self._format_context(context_comments)

        images = self._collect_images(
            [blog_content, comment_text]
            + [c.get("content", "") for c in context_comments]
        )

        target_label = "【他人回复】" if is_sub else "【他人评论】"

        message_str = (
            f"【动态原文】\n"
            f"作者：{blog_author}\n"
            f"标题：{blog_title}\n"
            f"内容：{blog_content}\n\n"
            f"【上方评论】\n"
            f"{context_str}\n\n"
            f"{target_label}\n"
            f"{comment_author}：{comment_text}\n\n"
            f"请针对以上评论输出回复。"
        )

        self._build_and_commit(
            msg_id,
            "blog",
            bid,
            reply_parent,
            comment_author,
            uid,
            message_str,
            {
                "type": "blog",
                "bid": bid,
                "parent_bcid": reply_parent,
                "is_sub": is_sub,
            },
            images,
        )

    async def _locate_blog_comment_context(
        self,
        bid: str,
        target_bcid: str,
        context_type: int,
        max_context: int = 10,
    ) -> tuple[list[dict], str]:
        """定位要回复的评论。

        返回 (上方上下文评论列表, 实际回复目标 parent_bcid)：
        - context_type==2（根评论）：上方=目标之前的根评论；回复目标=本条（target_bcid）
        - context_type==3（子评论）：上方=父根评论+目标之前的同父子评论；回复目标=父根评论 ID
        """
        page_size = 12
        if not target_bcid:
            return [], "0"

        if context_type == 2:
            seen: list[dict] = []
            offset = 0
            while True:
                result = await self.client.get_blog_comments(
                    bid,
                    parent_bcid="0",
                    offset=offset,
                    num=page_size,
                    cid_asc=1,
                )
                if result.get("status") != "success":
                    break
                comment_list = result.get("comment_list", [])
                if not comment_list:
                    break
                for c in comment_list:
                    if str(c.get("bcid", "")) == str(target_bcid):
                        return seen[-max_context:], str(target_bcid)
                    seen.append(c)
                if len(comment_list) < page_size:
                    break
                offset += page_size
            return seen[-max_context:], str(target_bcid)

        # context_type == 3: 子评论，需先找到其父根评论
        root_offset = 0
        while True:
            roots = await self.client.get_blog_comments(
                bid,
                parent_bcid="0",
                offset=root_offset,
                num=page_size,
                cid_asc=1,
            )
            if roots.get("status") != "success":
                break
            root_list = roots.get("comment_list", [])
            if not root_list:
                break
            for root in root_list:
                root_id = str(root.get("bcid", ""))
                # 父根评论视为上方第 1 条，其后为该根下的其他子评论
                sub_seen: list[dict] = [root]
                sub_offset = 0
                while True:
                    subs = await self.client.get_blog_comments(
                        bid,
                        parent_bcid=root_id,
                        offset=sub_offset,
                        num=page_size,
                        cid_asc=1,
                    )
                    if subs.get("status") != "success":
                        break
                    sub_list = subs.get("comment_list", [])
                    if not sub_list:
                        break
                    for sub in sub_list:
                        if str(sub.get("bcid", "")) == str(target_bcid):
                            return sub_seen[-max_context:], root_id
                        sub_seen.append(sub)
                    if len(sub_list) < page_size:
                        break
                    sub_offset += page_size
            if len(root_list) < page_size:
                break
            root_offset += page_size

        # 未找到目标评论：回退为回复动态本体（parent=0），内容仍带 @ 作者
        logger.warning(
            f"[OTTOhub Cmt] Blog comment {target_bcid} not found in tree, "
            f"fallback reply to blog root"
        )
        return [], "0"

    @staticmethod
    def _format_context(comments: list[dict]) -> str:
        if not comments:
            return "（无）"
        lines = []
        for c in comments[-10:]:
            author = (
                c.get("username")
                or c.get("sender_name")
                or "未知用户"
            )
            content = str(c.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"{author}：{content}")
        return "\n".join(lines) if lines else "（无）"

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
        context_type: int,
        source_comment_id: str,
        notification_text: str = "",
        author_hint: str = "",
    ) -> None:
        logger.info(
            f"[OTTOhub Cmt] Video: VID={vid}, UID={uid}, context_type={context_type}, "
            f"source_comment_id={source_comment_id}"
        )

        video = await self.client.get_video_detail(vid)
        if video.get("status") != "success":
            return

        video_title = video.get("title", "")
        video_intro = self._truncate(video.get("intro", ""), 1000)
        video_author = video.get("username", "")

        # 定位要回复的评论：取其上方的上下文评论（最多10条），并确定实际回复目标
        context_comments, reply_parent = await self._locate_video_comment_context(
            vid, source_comment_id, context_type
        )

        is_sub = context_type == 3
        comment_text = notification_text or "（无内容）"
        comment_author = author_hint or "未知用户"
        context_str = self._format_context(context_comments)

        images = self._collect_images(
            [video_intro, comment_text]
            + [c.get("content", "") for c in context_comments]
        )

        target_label = "【他人回复】" if is_sub else "【他人评论】"

        message_str = (
            f"【视频信息】\n"
            f"作者：{video_author}\n"
            f"标题：{video_title}\n"
            f"简介：{video_intro}\n\n"
            f"【上方评论】\n"
            f"{context_str}\n\n"
            f"{target_label}\n"
            f"{comment_author}：{comment_text}\n\n"
            f"请针对以上评论输出回复。"
        )

        self._build_and_commit(
            msg_id,
            "video",
            vid,
            reply_parent,
            comment_author,
            uid,
            message_str,
            {
                "type": "video",
                "vid": vid,
                "parent_vcid": reply_parent,
                "is_sub": is_sub,
            },
            images,
        )

    async def _locate_video_comment_context(
        self,
        vid: str,
        target_vcid: str,
        context_type: int,
        max_context: int = 10,
    ) -> tuple[list[dict], str]:
        """定位要回复的视频评论，语义与 _locate_blog_comment_context 一致。"""
        page_size = 12
        if not target_vcid:
            return [], "0"

        if context_type == 2:
            seen: list[dict] = []
            offset = 0
            while True:
                result = await self.client.get_video_comments(
                    vid,
                    parent_vcid="0",
                    offset=offset,
                    num=page_size,
                    cid_asc=1,
                )
                if result.get("status") != "success":
                    break
                comment_list = result.get("comment_list", [])
                if not comment_list:
                    break
                for c in comment_list:
                    if str(c.get("vcid", "")) == str(target_vcid):
                        return seen[-max_context:], str(target_vcid)
                    seen.append(c)
                if len(comment_list) < page_size:
                    break
                offset += page_size
            return seen[-max_context:], str(target_vcid)

        # context_type == 3: 子评论，需先找到其父根评论
        root_offset = 0
        while True:
            roots = await self.client.get_video_comments(
                vid,
                parent_vcid="0",
                offset=root_offset,
                num=page_size,
                cid_asc=1,
            )
            if roots.get("status") != "success":
                break
            root_list = roots.get("comment_list", [])
            if not root_list:
                break
            for root in root_list:
                root_id = str(root.get("vcid", ""))
                sub_seen: list[dict] = [root]
                sub_offset = 0
                while True:
                    subs = await self.client.get_video_comments(
                        vid,
                        parent_vcid=root_id,
                        offset=sub_offset,
                        num=page_size,
                        cid_asc=1,
                    )
                    if subs.get("status") != "success":
                        break
                    sub_list = subs.get("comment_list", [])
                    if not sub_list:
                        break
                    for sub in sub_list:
                        if str(sub.get("vcid", "")) == str(target_vcid):
                            return sub_seen[-max_context:], root_id
                        sub_seen.append(sub)
                    if len(sub_list) < page_size:
                        break
                    sub_offset += page_size
            if len(root_list) < page_size:
                break
            root_offset += page_size

        # 未找到目标评论：回退为回复视频本体（parent=0），内容仍带 @ 作者
        logger.warning(
            f"[OTTOhub Cmt] Video comment {target_vcid} not found in tree, "
            f"fallback reply to video root"
        )
        return [], "0"

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

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
