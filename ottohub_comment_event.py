from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot import logger
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
        self, cmt_type: str, object_id: str, parent_cid: str, text: str,
    ) -> None:
        chunks = [text[i : i + 400] for i in range(0, len(text), 400)]
        logger.info(f"[OTTOhub Cmt] Split {len(text)} chars into {len(chunks)} chunk(s)")
        for idx, chunk in enumerate(chunks):
            for attempt in range(3):
                try:
                    if cmt_type == "blog":
                        await self.platform.client.reply_comment(
                            object_id, parent_cid, chunk
                        )
                    elif cmt_type == "video":
                        await self.platform.client.reply_video_comment(
                            object_id, parent_cid, chunk
                        )
                    logger.info(f"[OTTOhub Cmt] Chunk {idx+1}/{len(chunks)} sent")
                    break
                except RuntimeError as e:
                    err_msg = str(e)
                    if "too_many_requests" in err_msg and attempt < 2:
                        wait = 15 * (attempt + 1)
                        logger.warning(
                            f"[OTTOhub Cmt] Rate limited, waiting {wait}s before retry"
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
            if idx < len(chunks) - 1:
                await asyncio.sleep(15)

    async def send(self, message: MessageChain) -> None:
        logger.info(
            f"[OTTOhub Cmt] event.send() ENTRY, session={self.session_id}, "
            f"chain_len={len(message.chain)}"
        )
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

        logger.info(
            f"[OTTOhub Cmt] send: session={self.session_id}, "
            f"bid/object_id={object_id}, parent_cid={parent_cid}, "
            f"author={comment_author}"
        )

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

        if comment_author:
            reply_text = f"@{comment_author} {reply_text}"

        logger.info(f"[OTTOhub Cmt] Replying to {cmt_type}:{object_id}, text_len={len(reply_text)}")
        await self._reply_with_retry(cmt_type, object_id, parent_cid, reply_text)

        await super().send(message)
