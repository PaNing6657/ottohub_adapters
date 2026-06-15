from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain

if TYPE_CHECKING:
    from .ottohub_comment_adapter import OTTOhubCommentPlatformAdapter


class OTTOhubCommentPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: Any,
        platform_meta: Any,
        session_id: str,
        platform: OTTOhubCommentPlatformAdapter,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.platform = platform

    async def _reply_with_retry(
        self, cmt_type: str, object_id: str, parent_cid: str, text: str
    ) -> None:
        chunks = [text[i : i + 400] for i in range(0, len(text), 400)]
        for idx, chunk in enumerate(chunks):
            for attempt in range(2):
                try:
                    if cmt_type == "blog":
                        await self.platform.client.reply_comment(
                            object_id, parent_cid, chunk
                        )
                    elif cmt_type == "video":
                        await self.platform.client.reply_video_comment(
                            object_id, parent_cid, chunk
                        )
                    break
                except RuntimeError as e:
                    if "too_many_requests" in str(e) and attempt < 1:
                        await asyncio.sleep(10)
                    else:
                        raise
            if idx < len(chunks) - 1:
                await asyncio.sleep(10)

    async def send(self, message: MessageChain) -> None:
        if not message.chain:
            return

        parts = self.session_id.split(":")
        if len(parts) < 4:
            await super().send(message)
            return

        cmt_type = parts[1]
        object_id = parts[2]
        parent_cid = parts[3]

        raw_info = {}
        if hasattr(self, "message_obj") and self.message_obj:
            raw_info = getattr(self.message_obj, "raw_message", {}) or {}
        comment_author = raw_info.get("comment_author", "")

        reply_parts = []
        for segment in message.chain:
            if isinstance(segment, Plain):
                reply_parts.append(segment.text)
            elif isinstance(segment, Image):
                image_path = await segment.convert_to_file_path()
                if image_path:
                    image_url = await self.platform.client.upload_image(image_path)
                    if image_url:
                        reply_parts.append(f"![image]({image_url})")

        reply_text = "".join(reply_parts).strip()
        if not reply_text:
            await super().send(message)
            return

        is_sub = raw_info.get("is_sub", False)
        if comment_author and not is_sub:
            reply_text = f"@{comment_author} {reply_text}"

        await self._reply_with_retry(cmt_type, object_id, parent_cid, reply_text)

        await super().send(message)
