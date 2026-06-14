from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Record,
    Video,
)

if TYPE_CHECKING:
    from .ottohub_adapter import OTTOhubPlatformAdapter


class OTTOhubPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: Any,
        platform_meta: Any,
        session_id: str,
        platform: OTTOhubPlatformAdapter,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.platform = platform

    @staticmethod
    def _segment_to_text(segment: BaseMessageComponent) -> str:
        if isinstance(segment, Plain):
            return segment.text
        if isinstance(segment, Image):
            return "[图片]"
        if isinstance(segment, File):
            return f"[文件:{segment.name}]"
        if isinstance(segment, Video):
            return "[视频]"
        if isinstance(segment, Record):
            return "[音频]"
        return "[消息]"

    @staticmethod
    def _build_plain_text(message: MessageChain) -> str:
        return "".join(
            OTTOhubPlatformEvent._segment_to_text(seg) for seg in message.chain
        )

    async def send(self, message: MessageChain) -> None:
        if not message.chain:
            return

        sender_id = self.get_sender_id()
        logger.info(f"[OTTOhub] Sending message to {sender_id}")

        for segment in message.chain:
            if isinstance(segment, Plain):
                text = segment.text
                chunks = [text[i : i + 200] for i in range(0, len(text), 200)]
                for idx, chunk in enumerate(chunks):
                    logger.info(f"[OTTOhub] Sending text: {chunk[:50]}...")
                    await self.platform.client.send_message(
                        receiver=sender_id,
                        message=chunk,
                    )
                    if idx < len(chunks) - 1:
                        await asyncio.sleep(3)
            elif isinstance(segment, Image):
                logger.info(f"[OTTOhub] Processing image, file: {segment.file}")
                image_path = await segment.convert_to_file_path()
                logger.info(f"[OTTOhub] Image path after convert: {image_path}")
                if image_path:
                    logger.info(f"[OTTOhub] Uploading image: {image_path}")
                    image_url = await self.platform.client.upload_image(image_path)
                    logger.info(f"[OTTOhub] Uploaded image URL: {image_url}")
                    if image_url:
                        markdown_msg = f"![image]({image_url})"
                        logger.info(f"[OTTOhub] Sending markdown: {markdown_msg}")
                        await self.platform.client.send_message(
                            receiver=sender_id,
                            message=markdown_msg,
                        )
                    else:
                        logger.warning("[OTTOhub] Image upload failed, no URL returned")
                else:
                    logger.warning("[OTTOhub] Image path is None")

        await super().send(message)
