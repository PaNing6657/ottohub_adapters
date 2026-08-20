"""OTTOhub 社交能力插件。

让机器人拥有以下能力:
1. 查看用户详情      /用户详情 <uid>
2. 发布动态          /发动态 <标题>|<内容>
3. 查看动态详情      /动态详情 <bid>
4. 搜索动态          /搜索动态 <关键词> [数量]
5. 关注 / 取关用户   /关注 <uid>  /取关 <uid>
6. 主动发送私信      /发消息 <uid> <内容>

辅助命令:
- /关注状态 <uid>   查询关注状态
- /会话列表         最近联系人
- /ottohub帮助      命令帮助

配置:在插件配置中填写 Token(或 用户ID+密码)与管理员用户ID 列表。
写操作(发动态/关注/取关/发消息)仅管理员可用;读操作所有用户可用。
"""

import asyncio
from typing import Any

from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .ottohub_client import OTTOhubClient

# 所有命令名与别名(用于从消息文本中剥离命令前缀)
_ALL_COMMANDS = {
    "用户详情", "userinfo", "user", "userdetail", "查用户",
    "发动态", "post", "sendblog",
    "动态详情", "bloginfo", "blog", "blogdetail",
    "搜索动态", "searchblog", "findblog", "搜动态",
    "关注", "follow",
    "取关", "unfollow",
    "关注状态", "followstatus",
    "发消息", "sendmsg", "message", "私信",
    "会话列表", "conversations", "联系人",
    "ottohub帮助", "ohhelp", "ottohub", "oh",
}

HELP_TEXT = (
    "🅾️ OTTOhub 社交能力命令:\n"
    "👤 /用户详情 <uid> — 查看用户详情\n"
    "📢 /发动态 <标题>|<内容> — 发布动态\n"
    "📄 /动态详情 <bid> — 查看动态详情\n"
    "🔍 /搜索动态 <关键词> [数量] — 搜索动态\n"
    "➕ /关注 <uid> — 关注用户\n"
    "➖ /取关 <uid> — 取消关注\n"
    "📌 /关注状态 <uid> — 查询关注状态\n"
    "✉️ /发消息 <uid> <内容> — 主动发送私信\n"
    "📩 /会话列表 — 最近联系人\n"
    "🆘 /ottohub帮助 — 本帮助"
)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.client: OTTOhubClient | None = None
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------ 内部工具

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @staticmethod
    def _raw_args(event: AstrMessageEvent) -> str:
        """去除命令前缀与 @提及后,返回剩余参数文本。"""
        text = str(event.message_str or "").strip()
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        if not parts:
            return ""
        if parts[0].startswith("@"):
            parts = parts[1].split(maxsplit=1) if len(parts) > 1 else []
            if not parts:
                return ""
        head = parts[0].lstrip("/.!")
        rest = parts[1] if len(parts) > 1 else ""
        if head in _ALL_COMMANDS:
            return rest.strip()
        return text

    async def _ensure_client(self) -> str | None:
        """确保客户端已登录;返回 None 表示就绪,否则返回错误信息。"""
        if self.client is not None and self.client.token:
            return None
        async with self._login_lock:
            if self.client is not None and self.client.token:
                return None

            base_url = self._cfg("API地址", "https://api.ottohub.cn")
            token_cfg = self._cfg("token", "")

            if token_cfg:
                self.client = OTTOhubClient(base_url=base_url, token=token_cfg)
                self.client.relogin_cb = self._relogin
                return None

            uid = self._cfg("用户ID", "")
            pw = self._cfg("密码", "")
            if not uid or not pw:
                return "未配置 OTTOhub 账号:请在插件配置中填写 Token,或填写 用户ID+密码"

            client = OTTOhubClient(base_url=base_url)
            try:
                result = await client.login(uid, pw)
            except Exception as e:
                return f"OTTOhub 登录失败:{e}"
            client.token = result.get("token")
            client.relogin_cb = self._relogin
            self.client = client
            return None

    async def _relogin(self) -> None:
        """token 失效时重新登录(供客户端 401 重试调用)。"""
        if self.client is None:
            return
        token_cfg = self._cfg("token", "")
        if token_cfg:
            self.client.token = token_cfg
            return
        uid = self._cfg("用户ID", "")
        pw = self._cfg("密码", "")
        if uid and pw:
            result = await self.client.login(uid, pw)
            self.client.token = result.get("token")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        whitelist = self._cfg("管理员用户ID", []) or []
        if not whitelist:
            return True
        sender = getattr(event.message_obj.sender, "user_id", "")
        return str(sender) in {str(x) for x in whitelist}

    def _guard_write(self, event: AstrMessageEvent) -> str | None:
        """写操作权限校验;通过返回 None,否则返回错误提示。"""
        if not self._is_admin(event):
            return "⛔ 没有权限执行此操作(仅管理员可执行写操作,请在插件配置中添加你的用户ID)"
        return None

    @staticmethod
    def _first_token(args: str) -> str:
        return args.split()[0] if args.split() else ""

    # ------------------------------------------------------------ 格式化

    @staticmethod
    def _fmt_user(d: dict[str, Any]) -> str:
        lines: list[str] = []
        name = d.get("username") or "未知"
        lines.append(f"👤 {name} (UID {d.get('uid', '-')})")
        if d.get("intro"):
            lines.append(f"📝 {d['intro']}")
        info: list[str] = []
        if d.get("sex"):
            info.append(f"性别:{d['sex']}")
        if d.get("honour"):
            info.append(f"荣誉:{d['honour']}")
        if d.get("experience") is not None:
            info.append(f"经验:{d['experience']}")
        if info:
            lines.append(" | ".join(info))
        if d.get("time"):
            lines.append(f"🕐 注册:{d['time']}")
        counts = []
        for label, key in (
            ("视频", "video_num"), ("动态", "blog_num"),
            ("静画", "seiga_num"), ("媒体", "media_num"),
        ):
            if d.get(key) is not None:
                counts.append(f"{label}:{d[key]}")
        if counts:
            lines.append("🎬 " + "  ".join(counts))
        lines.append(
            f"👥 关注:{d.get('followings_count', 0)}   "
            f"🧑‍🤝‍🧑 粉丝:{d.get('fans_count', 0)}"
        )
        if d.get("avatar_url"):
            lines.append(f"🖼 {d['avatar_url']}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_blog_detail(d: dict[str, Any]) -> str:
        lines: list[str] = [f"📢 动态 #{d.get('bid', '-')}"]
        if d.get("title"):
            lines.append(f"标题:{d['title']}")
        author = f"{d.get('username', '')} (UID {d.get('uid', '-')})"
        lines.append(f"作者:{author}  🕐 {d.get('time', '')}")
        if d.get("content"):
            lines.append(f"内容:\n{d['content']}")
        stats = []
        if d.get("like_count") is not None:
            stats.append(f"👍 {d['like_count']}")
        if d.get("favorite_count") is not None:
            stats.append(f"🔖 {d['favorite_count']}")
        if d.get("view_count") is not None:
            stats.append(f"👁 {d['view_count']}")
        if d.get("comment_count") is not None:
            stats.append(f"💬 {d['comment_count']}")
        if stats:
            lines.append(" ".join(stats))
        if d.get("tag"):
            lines.append(f"标签:{', '.join(d['tag'])}")
        if d.get("thumbnails"):
            lines.append(f"🖼 {' '.join(d['thumbnails'])}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 用户详情

    @filter.command("用户详情", alias={"userinfo", "user", "userdetail", "查用户"})
    async def cmd_user_detail(self, event: AstrMessageEvent):
        uid = self._first_token(self._raw_args(event))
        if not uid.isdigit():
            yield event.plain_result("用法:/用户详情 <uid>\n例:/用户详情 123")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            data = await self.client.get_user_detail(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        if not data:
            yield event.plain_result("未找到该用户")
            return
        yield event.plain_result(self._fmt_user(data))

    # ------------------------------------------------------------ 发动态

    @filter.command("发动态", alias={"post", "sendblog"})
    async def cmd_submit_blog(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        args = self._raw_args(event)
        if not args:
            yield event.plain_result(
                "用法:/发动态 <标题>|<内容>\n例:/发动态 今日分享|今天天气真好~\n"
                "不写标题时,将取内容前 30 字作为标题。"
            )
            return
        title, sep, content = args.partition("|")
        title = title.strip()
        content = content.strip()
        if not content:
            content = args.strip()
            title = content[:30]
        if not content:
            yield event.plain_result("动态内容不能为空")
            return
        if len(content) > 10000:
            yield event.plain_result("动态正文不能超过 10000 字")
            return
        if len(title) > 100:
            title = title[:100]

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.submit_blog(title=title, content=content)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"发布失败:{e}")
            return
        msg = "✅ 动态发布成功!"
        if result.get("if_warn") == 1:
            msg += "\n⚠️ 内容触发审核,通过后将公开展示。"
        if result.get("if_add_experience") == 1:
            msg += "\n⭐ 获得经验值奖励。"
        yield event.plain_result(msg)

    # ------------------------------------------------------------ 动态详情

    @filter.command("动态详情", alias={"bloginfo", "blog", "blogdetail"})
    async def cmd_blog_detail(self, event: AstrMessageEvent):
        bid = self._first_token(self._raw_args(event))
        if not bid.isdigit():
            yield event.plain_result("用法:/动态详情 <bid>\n例:/动态详情 1001")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            data = await self.client.get_blog_detail(bid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        if not data:
            yield event.plain_result("未找到该动态")
            return
        yield event.plain_result(self._fmt_blog_detail(data))

    # ------------------------------------------------------------ 搜索动态

    @filter.command("搜索动态", alias={"searchblog", "findblog", "搜动态"})
    async def cmd_search_blog(self, event: AstrMessageEvent):
        parts = self._raw_args(event).split()
        if not parts:
            yield event.plain_result(
                "用法:/搜索动态 <关键词> [数量]\n例:/搜索动态 赛博朋克 5\n"
                "可附加排序:最新 / 浏览 / 点赞 / 收藏"
            )
            return
        keyword = parts[0]
        num = 5
        sort_map = {
            "最新": "bid_desc", "新": "bid_desc",
            "浏览": "view_count_desc", "看": "view_count_desc",
            "点赞": "like_count_desc", "赞": "like_count_desc",
            "收藏": "favorite_count_desc", "藏": "favorite_count_desc",
        }
        sort = None
        for token in parts[1:]:
            if token.isdigit():
                num = min(int(token), 24)
            elif token in sort_map:
                sort = sort_map[token]

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.search_blogs(  # type: ignore[union-attr]
                keyword, offset=0, num=num, sort=sort
            )
        except Exception as e:
            yield event.plain_result(f"搜索失败:{e}")
            return
        blog_list = result.get("blog_list", [])
        total = result.get("total_count", len(blog_list))
        if not blog_list:
            yield event.plain_result(f"🔍 未找到与“{keyword}”相关的动态(共 {total} 条)")
            return
        lines = [f"🔍 搜索“{keyword}”,共 {total} 条,显示前 {len(blog_list)} 条:"]
        for b in blog_list:
            title = (b.get("title") or "").strip() or "(无标题)"
            line = f"#{b.get('bid')} {title}"
            extra = []
            if b.get("username"):
                extra.append(b["username"])
            if b.get("like_count") is not None:
                extra.append(f"👍{b['like_count']}")
            if b.get("view_count") is not None:
                extra.append(f"👁{b['view_count']}")
            if extra:
                line += " (" + " ".join(extra) + ")"
            lines.append(line)
        lines.append("发送 /动态详情 <bid> 查看完整内容")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 关注/取关

    @filter.command("关注", alias={"follow"})
    async def cmd_follow(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        uid = self._first_token(self._raw_args(event))
        if not uid.isdigit():
            yield event.plain_result("用法:/关注 <uid>\n例:/关注 123")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") == 1:
                yield event.plain_result(
                    f"你已经关注了用户 {uid}。\n如需取消,请发送 /取关 {uid}"
                )
                return
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"关注失败:{e}")
            return
        fans = result.get("new_fans_count", "")
        msg = f"✅ 已关注用户 {uid}!"
        if fans != "":
            msg += f"\n👥 对方粉丝数:{fans}"
        yield event.plain_result(msg)

    @filter.command("取关", alias={"unfollow"})
    async def cmd_unfollow(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        uid = self._first_token(self._raw_args(event))
        if not uid.isdigit():
            yield event.plain_result("用法:/取关 <uid>\n例:/取关 123")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") != 1:
                yield event.plain_result(f"你尚未关注用户 {uid}。")
                return
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"取关失败:{e}")
            return
        fans = result.get("new_fans_count", "")
        msg = f"✅ 已取消关注用户 {uid}。"
        if fans != "":
            msg += f"\n👥 对方粉丝数:{fans}"
        yield event.plain_result(msg)

    @filter.command("关注状态", alias={"followstatus"})
    async def cmd_follow_status(self, event: AstrMessageEvent):
        uid = self._first_token(self._raw_args(event))
        if not uid.isdigit():
            yield event.plain_result("用法:/关注状态 <uid>\n例:/关注状态 123")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        follow_status = status.get("follow_status")
        text = "已关注 💚" if follow_status == 1 else "未关注 🤍"
        yield event.plain_result(f"📌 用户 {uid} 关注状态:{text}")

    # ------------------------------------------------------------ 主动发私信

    @filter.command("发消息", alias={"sendmsg", "message", "私信"})
    async def cmd_send_message(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        parts = self._raw_args(event).split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            yield event.plain_result(
                "用法:/发消息 <uid> <内容>\n例:/发消息 123 你好呀"
            )
            return
        receiver, content = parts[0], parts[1].strip()
        if not content:
            yield event.plain_result("消息内容不能为空")
            return

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return

        # 单条私信上限 222 字,超出自动分片(每片 200 字)
        chunks = [content[i:i + 200] for i in range(0, len(content), 200)]
        try:
            for chunk in chunks:
                await self.client.send_message(receiver, chunk)  # type: ignore[union-attr]
                await asyncio.sleep(0.5)
        except Exception as e:
            yield event.plain_result(f"发送失败:{e}")
            return
        yield event.plain_result(f"✅ 已发送 {len(chunks)} 条私信给用户 {receiver}")

    # ------------------------------------------------------------ 会话列表

    @filter.command("会话列表", alias={"conversations", "联系人"})
    async def cmd_conversations(self, event: AstrMessageEvent):
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            users = await self.client.get_conversations(0, 10)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"获取会话失败:{e}")
            return
        if not users:
            yield event.plain_result("暂无会话记录")
            return
        lines = ["📩 最近联系人:"]
        for u in users:
            unread = u.get("new_message_num", 0)
            mark = f" (未读{unread})" if unread else ""
            last = u.get("last_message") or ""
            lines.append(
                f"#{u.get('uid')} {u.get('username', '')}{mark} | {last[:30]}"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 帮助

    @filter.command("ottohub帮助", alias={"ohhelp", "ottohub", "oh"})
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------ LLM 工具
    #
    # 以下工具通过 @filter.llm_tool 注册为 LLM 函数调用工具。
    # 用户以自然语言对话时,LLM 会自动选择合适的工具并传参调用,
    # 无需命令前缀。参数 schema 由 docstring 的 Args 段解析,
    # 格式必须为: 参数名(类型): 描述。
    # 注:llm_tool 的调用者可能是任意用户,写操作仍走 _guard_write 权限校验。

    @filter.llm_tool(name="ottohub_get_user_detail")
    async def llm_get_user_detail(self, event: AstrMessageEvent, uid: str):
        """查看 OTTOhub 用户的详情,包括昵称、简介、性别、荣誉、作品数、粉丝数与关注数。

        Args:
            uid(string): 要查询的用户ID,纯数字,例如 "123"
        """
        if not str(uid).strip().isdigit():
            yield event.plain_result("用户ID格式不正确,应为纯数字。")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            data = await self.client.get_user_detail(str(uid).strip())  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        if not data:
            yield event.plain_result("未找到该用户")
            return
        yield event.plain_result(self._fmt_user(data))

    @filter.llm_tool(name="ottohub_post_blog")
    async def llm_post_blog(
        self, event: AstrMessageEvent, title: str, content: str
    ):
        """在 OTTOhub 发布一条新动态,需要提供标题与正文内容。

        Args:
            title(string): 动态标题,1-100字
            content(string): 动态正文,1-10000字
        """
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        title = str(title).strip()
        content = str(content).strip()
        if not content:
            yield event.plain_result("动态内容不能为空")
            return
        if len(content) > 10000:
            yield event.plain_result("动态正文不能超过 10000 字")
            return
        if len(title) > 100:
            title = title[:100]
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.submit_blog(title=title, content=content)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"发布失败:{e}")
            return
        msg = "✅ 动态发布成功!"
        if result.get("if_warn") == 1:
            msg += "\n⚠️ 内容触发审核,通过后将公开展示。"
        if result.get("if_add_experience") == 1:
            msg += "\n⭐ 获得经验值奖励。"
        yield event.plain_result(msg)

    @filter.llm_tool(name="ottohub_get_blog_detail")
    async def llm_get_blog_detail(self, event: AstrMessageEvent, bid: str):
        """通过动态ID(bid)查看 OTTOhub 某条动态的完整详情,包括标题、作者、正文、点赞与评论数。

        Args:
            bid(string): 动态ID,纯数字,例如 "1001"
        """
        if not str(bid).strip().isdigit():
            yield event.plain_result("动态ID格式不正确,应为纯数字。")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            data = await self.client.get_blog_detail(str(bid).strip())  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        if not data:
            yield event.plain_result("未找到该动态")
            return
        yield event.plain_result(self._fmt_blog_detail(data))

    @filter.llm_tool(name="ottohub_search_blogs")
    async def llm_search_blogs(
        self, event: AstrMessageEvent, keyword: str, num: str = "5"
    ):
        """在 OTTOhub 搜索动态,按关键词匹配标题与内容,返回动态列表摘要。

        Args:
            keyword(string): 搜索关键词,例如 "赛博朋克"
            num(string): 返回数量,最大24,默认5
        """
        keyword = str(keyword).strip()
        if not keyword:
            yield event.plain_result("搜索关键词不能为空")
            return
        try:
            count = max(1, min(int(num), 24))
        except (TypeError, ValueError):
            count = 5
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.search_blogs(keyword, offset=0, num=count)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"搜索失败:{e}")
            return
        blog_list = result.get("blog_list", [])
        total = result.get("total_count", len(blog_list))
        if not blog_list:
            yield event.plain_result(f"🔍 未找到与“{keyword}”相关的动态(共 {total} 条)")
            return
        lines = [f"🔍 搜索“{keyword}”,共 {total} 条,显示前 {len(blog_list)} 条:"]
        for b in blog_list:
            title = (b.get("title") or "").strip() or "(无标题)"
            line = f"#{b.get('bid')} {title}"
            extra = []
            if b.get("username"):
                extra.append(b["username"])
            if b.get("like_count") is not None:
                extra.append(f"👍{b['like_count']}")
            if b.get("view_count") is not None:
                extra.append(f"👁{b['view_count']}")
            if extra:
                line += " (" + " ".join(extra) + ")"
            lines.append(line)
        lines.append("发送 /动态详情 <bid> 查看完整内容")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="ottohub_follow_user")
    async def llm_follow_user(self, event: AstrMessageEvent, uid: str):
        """关注 OTTOhub 上的某个用户(若已关注则提示,不重复操作)。

        Args:
            uid(string): 要关注的用户ID,纯数字,例如 "123"
        """
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        uid = str(uid).strip()
        if not uid.isdigit():
            yield event.plain_result("用户ID格式不正确,应为纯数字。")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") == 1:
                yield event.plain_result(f"已经关注了用户 {uid},无需重复关注。")
                return
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"关注失败:{e}")
            return
        fans = result.get("new_fans_count", "")
        msg = f"✅ 已关注用户 {uid}!"
        if fans != "":
            msg += f"\n👥 对方粉丝数:{fans}"
        yield event.plain_result(msg)

    @filter.llm_tool(name="ottohub_unfollow_user")
    async def llm_unfollow_user(self, event: AstrMessageEvent, uid: str):
        """取消关注 OTTOhub 上的某个用户(若未关注则提示,不重复操作)。

        Args:
            uid(string): 要取消关注的用户ID,纯数字,例如 "123"
        """
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        uid = str(uid).strip()
        if not uid.isdigit():
            yield event.plain_result("用户ID格式不正确,应为纯数字。")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") != 1:
                yield event.plain_result(f"尚未关注用户 {uid}。")
                return
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"取关失败:{e}")
            return
        fans = result.get("new_fans_count", "")
        msg = f"✅ 已取消关注用户 {uid}。"
        if fans != "":
            msg += f"\n👥 对方粉丝数:{fans}"
        yield event.plain_result(msg)

    @filter.llm_tool(name="ottohub_send_message")
    async def llm_send_message(
        self, event: AstrMessageEvent, receiver: str, content: str
    ):
        """主动给 OTTOhub 上的某个用户发送私信消息。

        Args:
            receiver(string): 接收者用户ID,纯数字,例如 "123"
            content(string): 消息内容,1-222字,超长自动分片发送
        """
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        receiver = str(receiver).strip()
        content = str(content).strip()
        if not receiver.isdigit():
            yield event.plain_result("接收者用户ID格式不正确,应为纯数字。")
            return
        if not content:
            yield event.plain_result("消息内容不能为空")
            return
        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        chunks = [content[i:i + 200] for i in range(0, len(content), 200)]
        try:
            for chunk in chunks:
                await self.client.send_message(receiver, chunk)  # type: ignore[union-attr]
                await asyncio.sleep(0.5)
        except Exception as e:
            yield event.plain_result(f"发送失败:{e}")
            return
        yield event.plain_result(f"✅ 已发送 {len(chunks)} 条私信给用户 {receiver}")

    # ------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:
                logger.warning(f"[OTTOhub Actions] close client failed: {e}")
