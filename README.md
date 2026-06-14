# OTTOhub 平台适配器

AstrBot 平台适配器插件，接入 [OTTOhub](https://www.ottohub.cn) 平台。

## 功能

- **OTTOhub私信**：轮询未读私信并自动回复
- **OTTOhub评论**：轮询评论通知（动态 / 视频 @消息），自动获取上下文并回复

## 配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 用户ID | OTTOhub 登录用户 ID | - |
| 密码 | OTTOhub 登录密码 | - |
| API地址 | OTTOhub API 地址 | https://api.ottohub.cn |
| 轮询间隔 | 消息轮询间隔（秒） | 3（私信）/ 5（评论） |

## 使用

在 AstrBot 面板中添加平台，选择 **OTTOhub私信** 或 **OTTOhub评论**，填写配置后启用即可。

## 文件结构

```
ottohub_adapters/
├── main.py                     # 插件入口
├── ottohub_adapter.py          # OTTOhub私信 适配器
├── ottohub_comment_adapter.py  # OTTOhub评论 适配器
├── ottohub_client.py           # OTTOhub API 客户端
├── ottohub_event.py            # 私信事件
├── ottohub_comment_event.py    # 评论事件
├── metadata.yaml               # 插件元数据
└── logo.png                    # 平台 Logo
```

## 依赖

- `astrbot` >= 4.1.0
