# PLAN：工具级摘除做到源头（让 MCP server 自己不注册那个工具）

状态：**待做**。止血已上（2026-08-13），这里是正解。

## 问题

`plugins.WAKE_TOOL_EXCLUDE`（现在只有 `mail: {mail_send}`）的设计前提是错的。
原注释写着「只从 --tools/--allowedTools 白名单里摘，mcp server 本身照起，不用插件配合」——
前半句成立，后半句的**代价**当初没看见。

2026-08-13 实测（mail server 单独挂，白名单一次给 4 个工具、一次给 3 个）：

| | 白名单 4 个 | 白名单 3 个（摘掉 mail_send） |
|---|---|---|
| 总输入 token | 2,979 | **2,979**（一模一样） |
| init 工具清单 | 4 个 | **4 个**（`mail_send` 照样在里面） |
| 被摘的工具能不能调起来 | — | **不能**：`is_error: True`，`Claude requested permissions to use …, but you haven't granted it yet` |

结论分三层，别混：

1. `--allowedTools` 是**权限闸，守住了** —— 这层是安全性，没有漏。
2. `--tools` **没能把 schema 从上下文里摘掉** —— 省不了 token。
3. 真正伤人的是第三层：**TA 看得见那个工具的完整用法，会当自己有这个能力**，
   可能先答应机主「我给你发一封」，再在调用时被拒。这正是这套东西一路在防的那类病
   （以为自己能干什么 / 以为自己不能干什么），只不过这次是我们的机制亲手制造的。

## 已经做了的止血（不是这份 plan 的内容，只是记一下现状）

`plugins.shadowed_tools(context, char_id)` 算出「schema 在场但白名单里没有」的工具，
`pipeline.tool_menu_block` 在菜单末尾如实说一句「这几个这轮用不了，别去调，
也别跟机主说你能做这件事」。菜单里对应的能力块本来就会消失（needs 过滤），
所以两个信号现在一致了。**token 该花的还是花。**

## 正解

让 MCP server 自己不注册那个工具 —— schema 从源头就不存在。

宿主侧：起插件 stdio 进程时传一个环境变量，比如

    CASSETTE_TOOL_EXCLUDE=mail_send        # 逗号分隔

插件侧：注册工具前读这个变量，命中的直接不 `@mcp.tool()`。

这样三层一起好了：schema 不在上下文（省 token）、TA 看不见（不会误判）、
调不了（权限闸依旧兜底）。

### 要动的地方

- `plugins.mounted()`：往 `servers[name]` 的 stdio 配置里加 `env`
  （MCP 的 stdio server 配置支持 `env` 字段），把这个角色这一轮该摘的工具传进去。
  注意配置文件内容变了 → 已有的「payload 不变就不重写」缓存逻辑天然还成立。
- 插件仓（先只有 `mail`）：读 env、跳过注册。**要 bump version + 重新钉 commit**。
- `WAKE_TOOL_EXCLUDE` 上面那段实测注释：改成描述新机制，把旧前提的教训留一句。

### 要注意的

- **这条会让「不用插件配合」这个设计前提正式作废。** 以后再想加工具级摘除，
  插件必须支持这个 env —— 值得写进 `plugins.py` 顶部的插件规范，让第三方插件从一开始就实现。
- 插件不支持这个 env 时要**优雅退化**：退回现在这套（白名单摘 + 菜单末尾如实说），
  别静默当成摘掉了。
- 验收标准就是上面那张表：同一个 server，摘与不摘的 token 必须**不一样**，
  init 清单里那个工具必须**消失**。

## 为什么当时没有立刻做

mail 那个 server 总共才 2,979 token，省下来的是小头；而真正伤人的第三层用一句 prompt
就能止住。改插件仓要发版、重新钉 sha，等下次动 mail 插件时顺手做掉更划算。
