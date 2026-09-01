import asyncio
import sys
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

from .ottohub_client import OTTOhubClient
from .ottohub_event import OTTOhubPlatformEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


def _cfg_number(value: Any, default: float, minimum: float = 0.1) -> float:
    """把配置项安全转换为数字。

    配置面板/配置文件可能把数值保存为字符串(如 "3"),
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
    "OTTOhub私信",
    "OTTOhub私信",
    default_config_tmpl={
        "enable": True,
        "用户ID": "",
        "密码": "",
        "API地址": "https://api.ottohub.cn",
        "轮询间隔": 3,
        "上传超时": 60,
    },
    logo_path="logo.png",
    support_streaming_message=False,
)
class OTTOhubPlatformAdapter(Platform):
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
        self._processed_msg_ids: set[str] = set()

    @override
    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            "OTTOhub私信",
            "OTTOhub私信",
            id=cast(str, self.config.get("id")),
            default_config_tmpl=self.config,
            support_proactive_message=True,
        )

    @override
    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        logger.info(f"[OTTOhub] send_by_session called, receiver={session.session_id}, chain_len={len(message_chain.chain)}")
        if not self.client:
            logger.warning("[OTTOhub] send_by_session: client is None, aborting")
            return

        receiver = session.session_id
        for segment in message_chain.chain:
            if isinstance(segment, Plain):
                text = segment.text
                chunks = [text[i : i + 200] for i in range(0, len(text), 200)]
                logger.info(
                    f"[OTTOhub] Split {len(text)} chars into {len(chunks)} chunk(s)"
                )
                for idx, chunk in enumerate(chunks):
                    logger.info(
                        f"[OTTOhub] send_by_session chunk {idx+1}/{len(chunks)}: "
                        f"{chunk[:50]}..."
                    )
                    try:
                        result = await self.client.send_message(
                            receiver=receiver,
                            message=chunk,
                        )
                        logger.info(f"[OTTOhub] send_by_session result: {result}")
                    except Exception as e:
                        logger.error(f"[OTTOhub] send_by_session failed: {e}")
                    if idx < len(chunks) - 1:
                        await asyncio.sleep(3)
            else:
                logger.info(
                    f"[OTTOhub] send_by_session skipping non-Plain segment: "
                    f"{type(segment).__name__}"
                )

        await super().send_by_session(session, message_chain)

    @override
    async def run(self) -> None:
        uid = self.config.get("用户ID")
        pw = self.config.get("密码")
        api_base_url = self.config.get("API地址", "https://api.ottohub.cn")

        if not uid or not pw:
            logger.error("[OTTOhub] 用户ID或密码未配置")
            return

        logger.info("[OTTOhub] Starting OTTOhub adapter...")

        self.client = OTTOhubClient(
            base_url=api_base_url,
            upload_timeout=_cfg_number(self.config.get("上传超时", 60), 60),
        )

        try:
            login_result = await self.client.login(uid, pw)
            logger.info(f"[OTTOhub] Login success: uid={login_result.get('uid')}")
            self.client.token = login_result.get("token")
            self.bot_self_id = str(login_result.get("uid"))
        except Exception as e:
            logger.error(f"[OTTOhub] Login failed: {e}")
            return

        self._polling_task = asyncio.create_task(self._polling_loop())
        await self._shutdown_event.wait()

    async def _polling_loop(self) -> None:
        poll_interval = _cfg_number(self.config.get("轮询间隔", 3), 3)

        while not self._shutdown_event.is_set():
            try:
                await self._check_and_process_messages()
            except Exception as e:
                logger.error(f"[OTTOhub] Polling error: {e}")

            await asyncio.sleep(poll_interval)

    async def _check_and_process_messages(self) -> None:
        if not self.client or not self.client.token:
            return

        try:
            unread_count = await self.client.get_unread_count()
            if int(unread_count) > 0:
                messages = await self.client.get_unread_messages(offset=0, num=20)
                for msg in messages:
                    await self._process_message(msg)
        except Exception as e:
            logger.error(f"[OTTOhub] Error checking messages: {e}")

    async def _process_message(self, msg: dict[str, Any]) -> None:
        msg_id = str(msg.get("msg_id", ""))
        if msg_id in self._processed_msg_ids:
            return

        sender = str(msg.get("sender", ""))
        # 忽略系统消息(UID 0)
        if sender == "0":
            self._processed_msg_ids.add(msg_id)
            return

        # 屏蔽机器人自身账号的消息
        if self.bot_self_id and sender == str(self.bot_self_id):
            self._processed_msg_ids.add(msg_id)
            if msg_id:
                try:
                    await self.client.mark_message_read(msg_id)
                except Exception as e:
                    logger.warning(f"[OTTOhub] Failed to mark self-message read: {e}")
            return

        self._processed_msg_ids.add(msg_id)

        abm = await self.convert_message(msg)
        await self.handle_msg(abm)

        if msg_id:
            try:
                await self.client.mark_message_read(msg_id) 
            except Exception as e:
                logger.warning(f"[OTTOhub] Failed to mark message read: {e}")

    async def convert_message(self, data: dict[str, Any]) -> AstrBotMessage:
        logger.info(f"[OTTOhub] Raw message data: {data}")

        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        content = data.get("content", "")
        abm.message_str = content
        abm.sender = MessageMember(
            user_id=str(data.get("sender", "")),
            nickname=data.get("sender_name", "Unknown"),
        )

        message_chain = []
        import re

        thumbnails = data.get("thumbnails", [])
        if thumbnails:
            for thumb_url in thumbnails:
                message_chain.append(Image(file=thumb_url))
                logger.info(f"[OTTOhub] Added image from thumbnails: {thumb_url}")

        img_pattern = r"https?://[^\s\)\]\>]+\.(?:jpg|jpeg|png|gif|webp|bmp)"
        img_matches = re.findall(img_pattern, content, re.IGNORECASE)

        for img_url in img_matches:
            message_chain.append(Image(file=img_url))
            logger.info(f"[OTTOhub] Added image from content: {img_url}")

        if content:
            message_chain.append(Plain(text=content))

        abm.message = message_chain if message_chain else [Plain(text="")]
        abm.raw_message = data
        abm.self_id = cast(str, self.bot_self_id)
        abm.session_id = str(data.get("sender", ""))
        abm.message_id = str(data.get("msg_id", ""))

        return abm

    async def handle_msg(self, message: AstrBotMessage) -> None:
        message_event = OTTOhubPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )
        self.commit_event(message_event)

    @override
    async def terminate(self) -> None:
        logger.info("[OTTOhub] Shutting down adapter...")
        self._shutdown_event.set()

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await asyncio.wait_for(self._polling_task, timeout=5)
            except asyncio.CancelledError:
                pass

        if self.client:
            await self.client.close()

        logger.info("[OTTOhub] Adapter shutdown complete.")
