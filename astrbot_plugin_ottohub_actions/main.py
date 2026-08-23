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
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .ottohub_client import OTTOhubClient
from .ottohub_qa import QuestionStore

# 所有命令名与别名(用于从消息文本中剥离命令前缀)
_ALL_COMMANDS = {
    "用户详情", "userinfo", "user", "userdetail", "查用户",
    "发动态", "post", "sendblog",
    "动态详情", "bloginfo", "blog", "blogdetail",
    "搜索动态", "searchblog", "findblog", "搜动态",
    "用户动态", "userblogs", "用户动态列表", "userblog",
    "评论", "comment", "评论动态",
    "回复", "reply",
    "关注", "follow",
    "取关", "unfollow",
    "关注状态", "followstatus",
    "发消息", "sendmsg", "message", "私信",
    "会话列表", "conversations", "联系人",
    "q", "匿问", "匿名提问", "anonask", "提问",
    "a", "匿答", "回答匿问", "anonanswer",
    "匿问状态", "匿问查询", "qastatus", "anonstatus",
    "ottohub帮助", "ohhelp", "ottohub", "oh",
}

HELP_TEXT = (
    "🅾️ OTTOhub 社交能力命令:\n"
    "👤 /用户详情 <uid> — 查看用户详情\n"
    "📢 /发动态 <标题>|<内容> — 发布动态(自动署名)\n"
    "📄 /动态详情 <bid> — 查看动态详情\n"
    "🔍 /搜索动态 <关键词> [数量] — 搜索动态\n"
    "📋 /用户动态 <uid> [数量] — 查看指定用户最近动态列表\n"
    "💬 /评论 <bid> <内容> — 评论动态(自动添加转达前缀)\n"
    "💬 /回复 <bid> <bcid> <内容> — 回复动态下的评论\n"
    "➕ /关注 <uid> — 关注用户\n"
    "➖ /取关 <uid> — 取消关注\n"
    "📌 /关注状态 <uid> — 查询关注状态\n"
    "✉️ /发消息 <uid> <内容> — 主动发送私信\n"
    "🙈 /q <uid> <问题> — 匿名提问(LLM审核,对方用 /a 回答)\n"
    "🔛 /q on / off — 开启/关闭接收匿问\n"
    "💬 /a <编号> <回答> — 回答匿问(收到匿名提问时使用)\n"
    "🗂 /匿问状态 [编号] — 查询匿问记录(我发起的/我收到的)与回答\n"
    "📩 /会话列表 — 最近联系人\n"
    "🆘 /ottohub帮助 — 本帮助\n"
    "\n以上能力同样支持自然语言对话(LLM 自动调用工具)。"
)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.client: OTTOhubClient | None = None
        self._login_lock = asyncio.Lock()
        # 匿问我答记录存储(插件数据目录,重启不丢失)
        data_dir = Path(__file__).resolve().parent / "data"
        self.qa_store = QuestionStore(data_dir / "ottohub_qa.json")

    # ------------------------------------------------------------ 内部工具

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取配置,兼容嵌套分组(_conf_schema.json 的 connection/permission)与扁平结构。

        AstrBot 对带分组的 _conf_schema.json 可能将配置保存为
        config["connection"]["token"] 的形式;此处先查顶层,再遍历分组查找。
        """
        cfg = self.config
        if not isinstance(cfg, dict):
            return default
        if key in cfg:
            return cfg[key]
        for value in cfg.values():
            if isinstance(value, dict) and key in value:
                return value[key]
        return default

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

    def _sender_name(self, event: AstrMessageEvent) -> str:
        """获取触发命令/工具的用户昵称,用于动态署名与评论转达前缀。"""
        name = ""
        try:
            name = event.get_sender_name()
        except Exception:
            name = ""
        if not name:
            sender = getattr(event.message_obj.sender, "nickname", "") or ""
            name = str(sender)
        if not name:
            name = str(getattr(event.message_obj.sender, "user_id", "未知用户"))
        return name

    def _relay_content(
        self, event: AstrMessageEvent, content: str, max_len: int = 459
    ) -> tuple[str | None, str]:
        """为评论内容添加转达前缀「XXX让我转达:」;内容超长时截断正文以保留前缀。

        返回 (错误信息, 最终内容);错误信息为 None 表示成功。
        """
        prefix = f"{self._sender_name(event)}让我转达:"
        if len(prefix) >= max_len:
            return "发送者昵称过长,无法添加转达前缀", ""
        remain = max_len - len(prefix)
        return None, prefix + str(content).strip()[:remain]

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

    @staticmethod
    def _fmt_qa_records(records: list[dict[str, Any]]) -> str:
        """格式化匿问记录列表,供 LLM 工具 / 命令返回。

        注意:此处只返回问题与回答内容,不包含提问者身份
        (asker_origin/asker_name),避免被问者视角泄露提问者。
        """
        if not records:
            return "暂无匿问记录"
        lines: list[str] = []
        for r in records:
            status = "🟡 待回答" if r.get("status") == "awaiting" else "✅ 已回答"
            answer = r.get("answer")
            answerer = r.get("answerer_name") or ""
            lines.append(f"#{r.get('qa_id')} [{status}]")
            lines.append(f"问题:{r.get('question', '')}")
            if r.get("status") == "answered":
                if answerer:
                    lines.append(f"回答({answerer}):{answer or ''}")
                else:
                    lines.append(f"回答:{answer or ''}")
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

        # 结尾自动追加署名,说明是谁让发布的;内容超长时截断正文以保留署名
        sign = f"\n\n—— 由「{self._sender_name(event)}」让我代发"
        if len(sign) > 10000:
            yield event.plain_result("发送者昵称过长,无法添加署名")
            return
        content = (content + sign)[:10000]
        if len(content) > 10000:
            content = content[:10000 - len(sign)] + sign
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

    # ------------------------------------------------------------ 用户动态列表

    @filter.command("用户动态", alias={"userblogs", "用户动态列表", "userblog"})
    async def cmd_user_blogs(self, event: AstrMessageEvent):
        parts = self._raw_args(event).split()
        if not parts or not parts[0].isdigit():
            yield event.plain_result(
                "用法:/用户动态 <uid> [数量]\n例:/用户动态 123 10"
            )
            return
        uid = parts[0]
        num = 10
        if len(parts) > 1 and parts[1].isdigit():
            num = min(max(int(parts[1]), 1), 24)

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.get_user_blogs(uid, offset=0, num=num)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"查询失败:{e}")
            return
        blog_list = result.get("blog_list", [])
        if not blog_list:
            yield event.plain_result(f"用户 {uid} 暂无动态")
            return
        lines = [f"📋 用户 {uid} 最近动态(显示前 {len(blog_list)} 条):"]
        for b in blog_list:
            title = (b.get("title") or "").strip() or "(无标题)"
            line = f"#{b.get('bid')} {title}"
            extra = []
            if b.get("time"):
                extra.append(b["time"])
            if b.get("like_count") is not None:
                extra.append(f"👍{b['like_count']}")
            if b.get("view_count") is not None:
                extra.append(f"👁{b['view_count']}")
            if b.get("comment_count") is not None:
                extra.append(f"💬{b['comment_count']}")
            if extra:
                line += " | " + " ".join(extra)
            lines.append(line)
        lines.append("发送 /动态详情 <bid> 查看完整内容")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 评论/回复

    @filter.command("评论", alias={"comment", "评论动态"})
    async def cmd_comment(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        parts = self._raw_args(event).split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            yield event.plain_result(
                "用法:/评论 <bid> <内容>\n例:/评论 1001 写得太好了"
            )
            return
        bid, content = parts[0], parts[1].strip()
        if not content:
            yield event.plain_result("评论内容不能为空")
            return
        err_msg, content = self._relay_content(event, content, max_len=459)
        if err_msg:
            yield event.plain_result(err_msg)
            return

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.reply_blog_comment(bid, content)  # type: ignore[union-attr]
        except Exception as e:
            yield event.plain_result(f"评论失败:{e}")
            return
        msg = "✅ 评论发表成功!"
        if result.get("if_warn") == 1:
            msg += "\n⚠️ 内容触发审核,通过后将公开展示。"
        yield event.plain_result(msg)

    @filter.command("回复", alias={"reply"})
    async def cmd_reply(self, event: AstrMessageEvent):
        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        parts = self._raw_args(event).split(maxsplit=2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            yield event.plain_result(
                "用法:/回复 <bid> <bcid> <内容>\n例:/回复 1001 55 同意楼上"
            )
            return
        bid, bcid, content = parts[0], parts[1], parts[2].strip()
        if not content:
            yield event.plain_result("回复内容不能为空")
            return
        err_msg, content = self._relay_content(event, content, max_len=459)
        if err_msg:
            yield event.plain_result(err_msg)
            return

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        try:
            result = await self.client.reply_blog_comment(  # type: ignore[union-attr]
                bid, content, parent_bcid=bcid
            )
        except Exception as e:
            yield event.plain_result(f"回复失败:{e}")
            return
        msg = "✅ 回复发表成功!"
        if result.get("if_warn") == 1:
            msg += "\n⚠️ 内容触发审核,通过后将公开展示。"
        yield event.plain_result(msg)

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

        # 私信开头自动添加转达前缀
        content = f"帮{self._sender_name(event)}转达:{content}"

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

    # ------------------------------------------------------------ 匿问我答(纯命令,不经 LLM)

    @filter.command("q", alias={"匿问", "匿名提问", "anonask", "提问"})
    async def cmd_anonymous_ask(self, event: AstrMessageEvent):
        """匿名提问 / 接收开关。

        - /q <uid> <问题>:匿名提问(LLM 审核通过后发送)
        - /q on | /q off:设置当前用户是否接收匿名提问
        """
        args = self._raw_args(event).strip()
        if not args:
            yield event.plain_result(
                "用法:\n"
                "/q <uid> <问题> — 匿名提问\n"
                "/q on — 开启接收匿问\n"
                "/q off — 关闭接收匿问"
            )
            return
        # 接收开关:on / off 不要求管理员权限
        head = args.split(maxsplit=1)[0].lower()
        if head in ("on", "off"):
            yield event.plain_result(await self._set_receive(event, head == "on"))
            return

        guard = self._guard_write(event)
        if guard:
            yield event.plain_result(guard)
            return
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            yield event.plain_result(
                "用法:/q <uid> <问题>\n例:/q 123 你最近在忙什么?\n"
                "开关:/q on 接收匿问  /q off 关闭接收"
            )
            return
        target_uid, question = parts[0], parts[1].strip()
        if not question:
            yield event.plain_result("问题内容不能为空")
            return
        question = question[:200]

        # 1. 对方是否接收匿问
        if not self.qa_store.receives(target_uid):
            yield event.plain_result(
                f"❌ 用户 {target_uid} 已关闭匿问接收,暂时无法提问。"
            )
            return

        # 2. LLM 审核:仅允许询问类问题,拒绝敏感内容
        err = await self._llm_review_question(event, question)
        if err:
            yield event.plain_result(err)
            return

        err = await self._ensure_client()
        if err:
            yield event.plain_result(err)
            return
        record = self.qa_store.create(
            question=question,
            target_uid=target_uid,
            asker_origin=event.unified_msg_origin,
            asker_name=self._sender_name(event),
        )
        qa_id = record["qa_id"]
        text = (
            f"📮 收到一条匿名提问(编号 {qa_id})\n\n"
            f"{question}\n\n"
            f"回复 /a {qa_id} <你的回答> 即可回答"
        )
        chunks = [text[i:i + 200] for i in range(0, len(text), 200)]
        try:
            for chunk in chunks:
                await self.client.send_message(target_uid, chunk)  # type: ignore[union-attr]
                await asyncio.sleep(0.5)
        except Exception as e:
            yield event.plain_result(f"匿名提问发送失败:{e}")
            return
        yield event.plain_result(
            f"✅ 已匿名向用户 {target_uid} 提问(问题编号 {qa_id})。\n"
            "对方回复 /a <编号> <回答> 后,回答会自动转达给你"
        )

    async def _set_receive(self, event: AstrMessageEvent, on: bool) -> str:
        """设置当前用户是否接收匿问(按发送者 uid 识别)。"""
        try:
            uid = str(event.get_sender_id() or "").strip()
        except Exception:
            uid = ""
        if not uid:
            return "❌ 无法识别你的用户ID,请稍后重试"
        self.qa_store.set_receive(uid, on)
        state = "已开启 ✅" if on else "已关闭 ❌"
        return f"匿问接收{state}。{'' if on else '别人将无法再匿名向你提问,可随时用 /q on 恢复。'}"

    async def _llm_review_question(
        self, event: AstrMessageEvent, question: str
    ) -> str | None:
        """用 LLM 审核问题:仅允许询问类且非敏感的内容。

        返回 None 表示审核通过;否则返回拒绝提示文案。
        """
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return None  # 未配置 LLM 时不做审核
            system = (
                "你是一名严格的内容审核员。判断给定内容是否满足全部条件:"
                "1) 意图是向他人提问/询问;2) 内容不涉及色情、暴力、违法、"
                "赌博、侮辱攻击、涉政等敏感或违规信息。"
                "只输出一行 JSON,格式:{\"allowed\": true或false, \"reason\": \"一句简短原因\"}。"
            )
            resp = await provider.text_chat(
                prompt=f"待审核内容:{question}",
                system_prompt=system,
            )
            text = ""
            if resp and resp.result_chain:
                text = (resp.result_chain.get_plain_text() or "").strip()
            if not text:
                text = (resp._completion_text or "").strip()  # type: ignore[attr-defined]
            import json as _json

            decision = _json.loads(text)
            allowed = bool(decision.get("allowed"))
            reason = str(decision.get("reason", "内容不符合提问规范"))
            if not allowed:
                return (
                    "❌ 提问被审核拦截,未发送。\n"
                    f"原因:{reason}\n"
                    "匿问仅接受正常的询问类问题,请重新组织后尝试。"
                )
            return None
        except Exception as e:
            logger.warning(f"[OTTOhub Actions] question review failed: {e}")
            return None  # 审核服务异常时放行,避免阻塞正常提问

    @filter.command("a", alias={"匿答", "回答匿问", "anonanswer"})
    async def cmd_qa_answer(self, event: AstrMessageEvent):
        """回答匿问:把被问者对某条匿名提问的回答转达给提问者。"""
        parts = self._raw_args(event).split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法:/a <编号> <回答>\n例:/a 1 最近在写代码"
            )
            return
        qa_id, answer = parts[0].strip(), parts[1].strip()
        if not qa_id.isdigit():
            yield event.plain_result("问题编号应为数字(如 /a 1 你的回答)")
            return
        if not answer:
            yield event.plain_result("回答内容不能为空")
            return
        sender_uid = str(event.get_sender_id() or "")
        record = self.qa_store.get(qa_id)
        if not record:
            yield event.plain_result(f"未找到问题编号 {qa_id}")
            return
        if str(record.get("target_uid", "")) != sender_uid:
            yield event.plain_result("⛔ 无权回答该匿问(仅被问者可回答)")
            return
        if record.get("status") == "answered":
            yield event.plain_result(f"问题 {qa_id} 已回答过,无需重复回答")
            return

        answerer_name = self._sender_name(event)
        self.qa_store.mark_answered(qa_id, answer, answerer_name)
        text = (
            f"{answerer_name}回答了你的问题 #{qa_id}:\n"
            f"{answer}"
        )
        chunks = [text[i:i + 200] for i in range(0, len(text), 200)]
        try:
            for chunk in chunks:
                await self.context.send_message(
                    record["asker_origin"], MessageChain([Plain(chunk)])
                )
        except Exception as e:
            logger.error(f"[OTTOhub Actions] relay answer failed: {e}")
            yield event.plain_result(f"转达失败:{e}")
            return
        yield event.plain_result(
            f"✅ 回答已转达给提问者(问题编号 {qa_id})。"
        )

    @filter.command("匿问状态", alias={"匿问查询", "qastatus", "anonstatus"})
    async def cmd_qa_status(self, event: AstrMessageEvent):
        """查询匿问状态:我发起的匿问(待回答/已回答)或我收到的匿问(被问者视角)。"""
        qa_id = self._first_token(self._raw_args(event))
        records: list[dict[str, Any]] = []
        if qa_id:
            record = self.qa_store.get(qa_id)
            if not record:
                yield event.plain_result(f"未找到匿问记录 {qa_id}")
                return
            records = [record]
            if not self._can_view_qa(event, record):
                yield event.plain_result("⛔ 无权查看该匿问记录")
                return
        else:
            records = self._viewer_qa_records(event)
        if not records:
            yield event.plain_result("暂无匿问记录(我发起的或我接收的)")
            return
        yield event.plain_result(self._fmt_qa_records(records))

    def _can_view_qa(self, event: AstrMessageEvent, record: dict[str, Any]) -> bool:
        """查询权限:仅提问者本人或被问者可查看对应记录。"""
        if record.get("asker_origin") == event.unified_msg_origin:
            return True
        try:
            sender_uid = str(event.get_sender_id() or "")
        except Exception:
            return False
        return sender_uid == str(record.get("target_uid", ""))

    def _viewer_qa_records(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        """当前用户可见的匿问记录:提问者视角(发起)+ 被问者视角(收到)。"""
        records = self.qa_store.list_by_asker(event.unified_msg_origin)
        try:
            sender_uid = str(event.get_sender_id() or "")
        except Exception:
            sender_uid = ""
        for r in self.qa_store.list_by_target(sender_uid):
            if r not in records:
                records.append(r)
        records.sort(key=lambda r: r.get("created_at", ""))
        return records

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
    async def llm_get_user_detail(self, event: AstrMessageEvent, uid: str) -> str:
        """查看 OTTOhub 用户的详情,包括昵称、简介、性别、荣誉、作品数、粉丝数与关注数。

        Args:
            uid(string): 要查询的用户ID,纯数字,例如 "123"
        """
        if not str(uid).strip().isdigit():
            return "用户ID格式不正确,应为纯数字。"
        err = await self._ensure_client()
        if err:
            return err
        try:
            data = await self.client.get_user_detail(str(uid).strip())  # type: ignore[union-attr]
        except Exception as e:
            return f"查询失败:{e}"
        if not data:
            return "未找到该用户"
        return self._fmt_user(data)

    @filter.llm_tool(name="ottohub_post_blog")
    async def llm_post_blog(
        self, event: AstrMessageEvent, title: str, content: str
    ) -> str:
        """在 OTTOhub 发布一条新动态,需要提供标题与正文内容。

        Args:
            title(string): 动态标题,1-100字
            content(string): 动态正文,1-10000字
        """
        guard = self._guard_write(event)
        if guard:
            return guard
        title = str(title).strip()
        content = str(content).strip()
        if not content:
            return "动态内容不能为空"
        # 结尾自动追加署名,说明是谁让发布的;内容超长时截断正文以保留署名
        sign = f"\n\n—— 由「{self._sender_name(event)}」让我代发"
        if len(sign) > 10000:
            return "发送者昵称过长,无法添加署名"
        content = (content + sign)[:10000]
        if len(content) > 10000:
            content = content[:10000 - len(sign)] + sign
        if len(title) > 100:
            title = title[:100]
        err = await self._ensure_client()
        if err:
            return err
        try:
            result = await self.client.submit_blog(title=title, content=content)  # type: ignore[union-attr]
        except Exception as e:
            return f"发布失败:{e}"
        if result.get("if_warn") == 1:
            return "动态发布成功,但内容触发审核,通过后将公开展示。"
        return "动态发布成功。"

    @filter.llm_tool(name="ottohub_get_blog_detail")
    async def llm_get_blog_detail(self, event: AstrMessageEvent, bid: str) -> str:
        """通过动态ID(bid)查看 OTTOhub 某条动态的完整详情,包括标题、作者、正文、点赞与评论数。

        Args:
            bid(string): 动态ID,纯数字,例如 "1001"
        """
        if not str(bid).strip().isdigit():
            return "动态ID格式不正确,应为纯数字。"
        err = await self._ensure_client()
        if err:
            return err
        try:
            data = await self.client.get_blog_detail(str(bid).strip())  # type: ignore[union-attr]
        except Exception as e:
            return f"查询失败:{e}"
        if not data:
            return "未找到该动态"
        return self._fmt_blog_detail(data)

    @filter.llm_tool(name="ottohub_get_user_blogs")
    async def llm_get_user_blogs(
        self, event: AstrMessageEvent, uid: str, num: str = "10"
    ) -> str:
        """查看 OTTOhub 某个用户最近发布的动态列表,按时间从新到旧,返回每条动态的ID、标题、时间与点赞/浏览/评论数。

        Args:
            uid(string): 用户ID,纯数字,例如 "123"
            num(string): 返回数量,最大24,默认10
        """
        uid = str(uid).strip()
        if not uid.isdigit():
            return "用户ID格式不正确,应为纯数字。"
        try:
            count = max(1, min(int(num), 24))
        except (TypeError, ValueError):
            count = 10
        err = await self._ensure_client()
        if err:
            return err
        try:
            result = await self.client.get_user_blogs(uid, offset=0, num=count)  # type: ignore[union-attr]
        except Exception as e:
            return f"查询失败:{e}"
        blog_list = result.get("blog_list", [])
        if not blog_list:
            return f"用户 {uid} 暂无动态。"
        lines = [f"用户 {uid} 最近动态,共 {len(blog_list)} 条:"]
        for b in blog_list:
            title = (b.get("title") or "").strip() or "(无标题)"
            line = f"#{b.get('bid')} {title}"
            extra = []
            if b.get("time"):
                extra.append(f"时间:{b['time']}")
            if b.get("like_count") is not None:
                extra.append(f"点赞:{b['like_count']}")
            if b.get("view_count") is not None:
                extra.append(f"浏览:{b['view_count']}")
            if b.get("comment_count") is not None:
                extra.append(f"评论:{b['comment_count']}")
            if extra:
                line += " | " + " ".join(extra)
            lines.append(line)
        return "\n".join(lines)

    @filter.llm_tool(name="ottohub_search_blogs")
    async def llm_search_blogs(
        self, event: AstrMessageEvent, keyword: str, num: str = "5"
    ) -> str:
        """在 OTTOhub 搜索动态,按关键词匹配标题与内容,返回动态列表摘要。

        Args:
            keyword(string): 搜索关键词,例如 "赛博朋克"
            num(string): 返回数量,最大24,默认5
        """
        keyword = str(keyword).strip()
        if not keyword:
            return "搜索关键词不能为空"
        try:
            count = max(1, min(int(num), 24))
        except (TypeError, ValueError):
            count = 5
        err = await self._ensure_client()
        if err:
            return err
        try:
            result = await self.client.search_blogs(keyword, offset=0, num=count)  # type: ignore[union-attr]
        except Exception as e:
            return f"搜索失败:{e}"
        blog_list = result.get("blog_list", [])
        total = result.get("total_count", len(blog_list))
        if not blog_list:
            return f"未找到与“{keyword}”相关的动态(共 {total} 条)"
        lines = [f"搜索“{keyword}”,共 {total} 条,显示前 {len(blog_list)} 条:"]
        for b in blog_list:
            title = (b.get("title") or "").strip() or "(无标题)"
            line = f"#{b.get('bid')} {title}"
            extra = []
            if b.get("username"):
                extra.append(b["username"])
            if b.get("like_count") is not None:
                extra.append(f"点赞{b['like_count']}")
            if b.get("view_count") is not None:
                extra.append(f"浏览{b['view_count']}")
            if extra:
                line += " (" + " ".join(extra) + ")"
            lines.append(line)
        return "\n".join(lines)

    @filter.llm_tool(name="ottohub_follow_user")
    async def llm_follow_user(self, event: AstrMessageEvent, uid: str) -> str:
        """关注 OTTOhub 上的某个用户(若已关注则提示,不重复操作)。

        Args:
            uid(string): 要关注的用户ID,纯数字,例如 "123"
        """
        guard = self._guard_write(event)
        if guard:
            return guard
        uid = str(uid).strip()
        if not uid.isdigit():
            return "用户ID格式不正确,应为纯数字。"
        err = await self._ensure_client()
        if err:
            return err
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") == 1:
                return f"已经关注了用户 {uid},无需重复关注。"
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            return f"关注失败:{e}"
        fans = result.get("new_fans_count", "")
        if fans != "":
            return f"已关注用户 {uid},对方粉丝数:{fans}。"
        return f"已关注用户 {uid}。"

    @filter.llm_tool(name="ottohub_unfollow_user")
    async def llm_unfollow_user(self, event: AstrMessageEvent, uid: str) -> str:
        """取消关注 OTTOhub 上的某个用户(若未关注则提示,不重复操作)。

        Args:
            uid(string): 要取消关注的用户ID,纯数字,例如 "123"
        """
        guard = self._guard_write(event)
        if guard:
            return guard
        uid = str(uid).strip()
        if not uid.isdigit():
            return "用户ID格式不正确,应为纯数字。"
        err = await self._ensure_client()
        if err:
            return err
        try:
            status = await self.client.get_follow_status(uid)  # type: ignore[union-attr]
            if status.get("follow_status") != 1:
                return f"尚未关注用户 {uid}。"
            result = await self.client.follow_user(uid)  # type: ignore[union-attr]
        except Exception as e:
            return f"取关失败:{e}"
        fans = result.get("new_fans_count", "")
        if fans != "":
            return f"已取消关注用户 {uid},对方粉丝数:{fans}。"
        return f"已取消关注用户 {uid}。"

    @filter.llm_tool(name="ottohub_send_message")
    async def llm_send_message(
        self, event: AstrMessageEvent, receiver: str, content: str
    ) -> str:
        """主动给 OTTOhub 上的某个用户发送私信消息。

        Args:
            receiver(string): 接收者用户ID,纯数字,例如 "123"
            content(string): 消息内容,1-222字,超长自动分片发送
        """
        guard = self._guard_write(event)
        if guard:
            return guard
        receiver = str(receiver).strip()
        content = str(content).strip()
        if not receiver.isdigit():
            return "接收者用户ID格式不正确,应为纯数字。"
        if not content:
            return "消息内容不能为空"
        # 私信开头自动添加转达前缀
        content = f"帮{self._sender_name(event)}转达:{content}"
        err = await self._ensure_client()
        if err:
            return err
        chunks = [content[i:i + 200] for i in range(0, len(content), 200)]
        try:
            for chunk in chunks:
                await self.client.send_message(receiver, chunk)  # type: ignore[union-attr]
                await asyncio.sleep(0.5)
        except Exception as e:
            return f"发送失败:{e}"
        return f"已发送 {len(chunks)} 条私信给用户 {receiver}。"

    @filter.llm_tool(name="ottohub_comment_blog")
    async def llm_comment_blog(
        self,
        event: AstrMessageEvent,
        bid: str,
        content: str,
        parent_bcid: str = "0",
    ) -> str:
        """在 OTTOhub 的某条动态下发表评论或回复评论,自动在开头添加转达前缀。

        Args:
            bid(string): 动态ID,纯数字,例如 "1001"
            content(string): 评论内容,1-459字
            parent_bcid(string): 父评论ID;评论动态本身填 "0",回复某条评论填该评论的bcid
        """
        guard = self._guard_write(event)
        if guard:
            return guard
        bid = str(bid).strip()
        content = str(content).strip()
        parent_bcid = str(parent_bcid or "0").strip()
        if not bid.isdigit():
            return "动态ID格式不正确,应为纯数字。"
        if not parent_bcid.isdigit():
            return "父评论ID格式不正确,应为纯数字。"
        if not content:
            return "评论内容不能为空"
        err_msg, content = self._relay_content(event, content, max_len=459)
        if err_msg:
            return err_msg

        err = await self._ensure_client()
        if err:
            return err
        try:
            result = await self.client.reply_blog_comment(  # type: ignore[union-attr]
                bid, content, parent_bcid=parent_bcid
            )
        except Exception as e:
            return f"评论失败:{e}"
        if result.get("if_warn") == 1:
            return "评论发表成功,但内容触发审核,通过后将公开展示。"
        return "评论发表成功。"

    # ------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:
                logger.warning(f"[OTTOhub Actions] close client failed: {e}")
