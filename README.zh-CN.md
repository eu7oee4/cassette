> English: [README.md](README.md)

# cassette

一个装在自己口袋里的 AI 伙伴：iOS 原生聊天 app + 跑在自己 Mac 上的 Python 后端，模型调用走本机 `claude` CLI 的登录态。没有第三方服务器，聊天记录只存在你自己的手机里。

给它起个名字、写一份人设、配几张表情包——然后它会在你不找它的时候，自己"醒来"，想一想最近的对话，决定要不要主动给你发条消息。

## 它能做什么

- **像聊天软件一样聊**：流式逐字回复、Markdown 渲染（代码块带高亮和一键复制）、消息编辑/删除/重新生成、头像昵称自定义、微信式的倒置滚动和新消息角标。
- **自主醒来**：后端调度器按你设置的频率随机唤醒模型，它读一遍最近的对话和自己上几次醒来时的内心独白，自己决定发消息还是安静待着。它也可以给自己定闹钟（"3 小时后叫我"），聊天里说到"我去睡了"，它会顺手安排一次到点的醒来。
- **长期记忆（可选）**：对接 [Ombre-Brain](https://github.com/P0luz/Ombre-Brain)（P0luz 的开源记忆系统，自部署 Docker 服务）——聊天开场相关记忆自然浮现，值得留住的事它随手存下，醒来时也带着记忆想事；存了什么、改了什么在聊天里以灰字可见。没跑 Ombre 就自动退回纯聊天，一切照常。
- **发表情包**：从相册往表情库里加图，模型看图自动写一句描述；之后它会在合适的情绪下挑一张发给你（聊天里和醒来时都会），觉得描述不准还会自己改。想预置一套默认表情，把图（png）放进 `ios/cassette/DefaultStickers/` 即可——文件名就是初始描述，首启自动入库。
- **有时间感**：知道现在是"周三深夜"还是"周六上午"，知道你隔了 3 小时才回消息——这些都注入在 prompt 里，它不会在下午跟你说晚安。
- **消息不丢**：生成中途锁屏、切后台、断网，后端照跑到完，回复进"待送达盒子"，app 回前台自动补上屏；配了 [Bark](https://github.com/Finb/Bark) 的话手机还会收到推送。反过来的情况也兜住了：请求根本没到后端，app 会和后端对账、一分钟左右提示你重发；那轮模型空产出，也会收到明确提示——一轮对话不会不明不白地消失。

## 架构

```
┌─────────────┐  HTTP + SSE   ┌──────────────────┐  子进程   ┌────────────┐
│  iOS app     │ ────────────▶ │  FastAPI 后端     │ ────────▶ │ claude -p  │
│ (SwiftUI)    │ ◀──────────── │  (跑在你的 Mac)   │ ◀──────── │ (CLI 登录态)│
└─────────────┘   X-Auth 密钥  └──────────────────┘  stream-json └────────────┘
  聊天历史唯一主人      无状态 + 少量运行时状态           每条消息一次性进程
```

三条设计主线：

1. **app 是聊天历史的唯一主人**。后端不记对话——每次请求 app 把最近的完整历史发过来（默认 100 条），后端拼成一次性 prompt 交给模型。编辑、删除、重新生成因此都是纯本地操作，历史随 app 走。
2. **每条消息一个一次性 `claude -p` 子进程**。没有常驻会话，上下文靠历史注入。人设文件通过 `--system-prompt-file` **完整替换**默认系统提示词——模型看到的就是你写的人格，别的什么都不混进来。凭据走 CLI 登录态（子进程环境里主动删掉 `ANTHROPIC_API_KEY`）——订阅账号登录时聊天和醒来全部吃订阅额度，不产生按量 API 计费；想走 API key 的话，把 `pipeline.py` 里删 key 的那行去掉即可。
3. **后端只存"醒来所需的最小状态"**（`server/state/`，已 gitignore）：最近对话的窗口快照、醒来日志、醒来排程（自定的下次醒来时间点，跨重启保留）、待送达盒子、表情清单、设置。app 不在场时，模型靠这些醒来。

## 自主醒来是怎么工作的

省 token 是第一原则：调度器每 5 分钟 tick 一次，但**所有预判都是纯本地的**——掷概率、看时段、查静默期、查最小间隔——只有真的要醒才起一次模型进程。

醒来时模型收到最近对话 + 自己前几次醒来的内心（按时间线合并），按四段协议回答：

```
THOUGHTS: 此刻真实的内心
ACTION:   none / message
CONTENT:  要发的话（ACTION=message 时）
NEXT:     希望多久后再醒（可写"无"）
```

发消息之前还有一道**打扰控制**硬闸：每天条数上限、两条主动消息的最小间隔、你刚说过话后的静默期。闸门只拦推送、不拦思考——而且会在醒来时提前告诉模型"这轮你发了也送不出去"，它不会误以为消息发出去了。设置页里这些都可以调，也可以整个关掉。

几个细节：

- 模型自定的 NEXT 只保证"到点醒一次"，不压随机醒来的节奏。调度器是 5 分钟轮询一次，所以"到点"可能有延迟，最多 5 分钟。
- 醒来生成的几十秒里你恰好发来新消息 → 那条主动消息整条作废（内容讲的已是旧世界），只记日志。
- 聊天正在进行中 → 醒来避让到下个 tick，不拿过期上下文说胡话。
- 连续失败（如 CLI 登录态过期）→ 30 分钟退避，不会每个 tick 都白起进程。

## 历史是可编辑的（编辑它 = 编辑 TA 的记忆）

每条消息都带编辑和复制按钮，长按气泡可删除；自己的消息还能"编辑并重新回复"——清掉这条之后的旧对话，让 TA 重新回答。这些都是纯本地操作：后端无状态、历史每轮整体注入，所以**改本地历史就是改 TA 的记忆**，下一轮立即生效，不需要后端配合。

编辑比看起来有用得多：

- **改错别字**：无关紧要的笔误直接修掉，强迫症友好，不用为它重新生成一轮。
- **改小错误**：小体量的错误信息——包括 TA 自己历史回复里说错的——直接改正文就行，不用为一个小点反复纠正模型或整段重新生成；下次注入的历史就是准的。
- **修语气**：把 TA 的历史回复改成你喜欢的说法，等于在 persona 文件之外做示范式微调——注入的历史就是最有力的范例，改几次之后，就能找到你想要的那个 TA。

## 快速开始

需要：一台 Mac（后端跑在这）、一台 iPhone（前端是 iOS app）、已安装并登录的 [claude CLI](https://claude.com/claude-code)（默认只走订阅额度；API 也能用，但要删一行代码，见「架构」第 2 条）、Python 3、Xcode。

### 1. 后端

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # 至少填 CASSETTE_AUTH_KEY（自定一串随机密钥）
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

`.env` 里可配的还有：模型（`CLAUDE_MODEL`，默认 opus）、时区、人设文件路径、Bark 推送地址、醒来调度开关和 tick 间隔。所有个人配置只活在 `.env`，不进仓库。

### 2. 人设

```bash
cp persona.example.md persona.md   # 然后自由改写
```

这份文件就是它的全部人格。支持 `{{AGENT_NAME}}` / `{{USER_NAME}}` 占位符（由 app 里起的名字替换）。每次调用现读——改人设不用重启后端。

### 3. iOS app

先从模板复制一份连接配置（`Config.swift` 已 gitignore，真实地址和密钥不进仓库）：

```bash
cd ios/cassette
cp Config.swift.example Config.swift
```

然后用 Xcode 打开 `ios/cassette.xcodeproj`，改 `Config.swift` 里的两个值：

- `authKey`：和 `.env` 里的 `CASSETTE_AUTH_KEY` 一致。
- `baseURL`：模拟器直接用 `127.0.0.1:8000`；真机换成 Mac 的局域网 IP（同一 Wi-Fi），想在外面也能聊就用 [Tailscale](https://tailscale.com) 的 IP。

跑起来，首启引导里给它和自己起好名字，开聊。

### 4. 长期记忆（可选）

自部署一个 [Ombre-Brain](https://github.com/P0luz/Ombre-Brain)（P0luz 的开源记忆系统，Docker 一条命令起），在 `.env` 里把 `OMBRE_MCP_URL` 指过去；Ombre 侧开了静态密钥鉴权（`mcp_auth_mode: "token"`，推荐）就把同一个密钥填进 `OMBRE_MCP_TOKEN`。后端每次调用前会快速探活：Ombre 不在（没装、没开、中途挂了）就自动退回纯聊天，永不因记忆层断掉。

## 项目结构

```
server/
  app.py          # FastAPI 路由（/health /chat /chat/stream /chat/active /pending
                  #   /pending/ack /settings /describe_sticker）+ 流式心跳、断连补投守护
  config.py       # 配置读取：.env → 常量；名字取值（app 设置优先，env 兜底）
  pipeline.py     # prompt 拼装、claude -p 子进程、时间感知、内联标记解析
  sse.py          # stream-json → SSE 的流式翻译（标记过滤、空闲超时检测）
  wake.py         # 醒来调度器：本地预闸门 → 四段协议 → 打扰控制
  state_store.py  # 运行时状态（纯文件，原子写 + 锁）
  notify.py       # Bark 推送 + 带时间戳的错误日志
  persona.example.md
ios/cassette/     # SwiftUI app：聊天界面、表情库、设置页、本地持久化
```

## 数据与安全

- 聊天记录存 app 沙盒 `Documents/chat_history.json`；后端只保留一份最近窗口的影子快照（≤300 条）。
- 除 `/health` 健康检查外，所有接口都要过 `X-Auth` 共享密钥认证；没配密钥时这些接口全部拒绝（fail closed），密钥比较用常数时间。
- 醒来日志 `server/state/wake_log.jsonl` 是 append-only 的，存着每次醒来的完整内心独白、连同被打扰控制拦下没发出的消息正文，且不会自动清理（待送达盒子有 7 天清理，它没有）。它不出你的 Mac，但整个项目里私密浓度最高的就是这个文件，值得知道它的存在。
- 模型子进程默认 `--tools ""`，纯对话；挂了 Ombre 时也只放行记忆工具白名单（`--strict-mcp-config` 屏蔽机器上其它 MCP，`--allowedTools` 预批准免弹权限），Bash / 文件读写这类内置工具永远不开，也从不使用 `--dangerously-skip-permissions`。
- 建议后端只暴露在局域网或 Tailscale 内网，不要裸奔公网。

## Roadmap

- **聊天模式 ⇄ 写代码模式无缝切换**：从 `claude -p` 的 chat 模式一键切进 tmux 会话里的 code 模式——同一个人设、连续的记忆和意识，用手机就能让 Mac 上的 Claude Code 帮你跑代码。
- **插件生态**：工具族（浏览器、网页生成等）做成独立插件仓，后端动态挂载，app 内一键装卸；registry 采用写死白名单，install 绝不接受任意 URL。
- **Web 客户端**：客户端足够薄，值得一个浏览器里的版本。

## 致谢

长期记忆对接 [Ombre-Brain](https://github.com/P0luz/Ombre-Brain)——P0luz 的开源记忆系统，本项目只作为外部服务对接、不内嵌其代码。iOS 端用到 [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui)、[Highlightr](https://github.com/raspu/Highlightr)、[swift-markdown](https://github.com/swiftlang/swift-markdown)；推送用 [Bark](https://github.com/Finb/Bark)。

## License

[MIT](LICENSE)
