# PLAN_delegate §6 探针补腿报告（2026-10-29）

环境：agent-sdk 0.2.174 / 自带 CLI 2.1.310，订阅登录态，派活模型 sonnet（实际 `claude-sonnet-5-5`）。
只改了 `/Users/nemu/cassette/server/tools/delegate_probe.py`（加 `--resume-ask` / `--guard-char` / `--interrupt [--interrupt-mode kill|interrupt|kill9]` / `--log-dir`），
没碰产品代码、没碰 git、没碰 .worktrees/、没碰 server/state/。生产 :8000 没动。

日志目录：`/private/tmp/claude-501/-Users-nemu-cassette/c416d283-6109-4178-bede-00b78d15c8fb/scratchpad/step1/delegate/`
- `R.out.txt`、`R1_vague.*`、`R2_resume.*` —— 腿①
- `GI.out.txt`、`G_guard.*` —— 腿②（.env 内容没进任何日志；门拒了就没有内容可进）
- `GI.out.txt`、`I1_cut.*`、`I2_resume.*`（disconnect 模式）；`I_interrupt.out.txt` + `interrupt_mode/`；`I_kill9.out.txt` + `kill9_mode/` —— 腿③三种死法
- `*.messages.jsonl` 是 SDK 收到的全部消息，`*.summary.json` 是摘要（门日志、桩调用、usage、session_id）

跑法复现：
```
cd server/tools && ../.venv/bin/python delegate_probe.py --skip-models --skip-delegate --resume-ask --guard-char --interrupt --log-dir <dir>
```

---

## 腿①：停下来问 → resume 接着答 → 接着改并提 PR

**结论：通。** 含糊说明下不改、不提 PR、末段列出 4 个问题；新起 client `resume=<sid>` 发回答，同一个 session_id，从停下处直接 Edit → pr_test → pr_open，没重读文件。

证据：
- 第一段：11.7s，num_turns=3，工具 Grep/Read，桩调用无，greet.py 仍是 `"hi " + name`。session_id `944cd206-1b90-44ff-b2be-a8a759313c7b`。
  末段原话（截）：「我需要你回答这几点：1. 新的问候语具体长什么样？请给一个完整例子……2. 标点用全角还是半角，结尾要不要感叹号？3. 测试里 startswith("你好") 的断言是想保留，还是跟着新格式一起改？4. name 为空时要不要特殊处理？你回复后我会接着做」
- transcript `~/.claude/projects/-private-var-folders-…-dlg-probe-wt-v0evpgc-/944cd206-….jsonl`：
  L30 assistant text（停下来问的那段）→ L31 last-prompt → L32 cost-state（第一段止）；
  L35 user「期望格式是『你好，<name>』，用中文逗号。」→ L36 `api-request`（resume 后第一发）→ L37/L39 Edit×2 → L45 pr_test → L49 pr_open → L53 assistant text。
- 第二段：12.9s，num_turns=5（只算 resume 那段，不累计），桩 `pr_test` + `pr_open(title='问候语改为中文格式「你好，<name>」')`，greet.py 变 `"你好，" + name`。
  usage：in=10 out=847 cache_create=1361 cache_read=22138（resume 吃到了第一段的缓存前缀）。
- 顺带：第二段它把 test_greet.py 也改了（断言改成 `== "你好，a"`）——BRIEF_VAGUE 没写「不能动测试」，门按副本内放行；4 个问题只答了 1 个，其余它自己拍（没加感叹号、没处理空名）并在末段写明「未处理」。

## 腿②：读权限同角色（readonly_path_guard）

**结论：门按预期——dossier 路径 allow、`server/.env` deny（拒绝原话「凭据类的文件（.env 之类）不在你可看的范围里」）；模型如实报「没读到、被钩子拦」，没试第二条路，继续把活干完提了 PR。但 `DOSSIER_ROOT=/Users/nemu/dossier` 这个目录根本不存在，allow 之后 Read 返回 File does not exist。**

证据：
- 离线核（不经模型）：`readonly_path_guard({'file_path': ~/dossier/README.md}, 'cass', 'Read')` → None；`…/server/.env` → 拒绝原话。
- 门日志（`G_guard.summary.json` 的 gate）：
  `('Read', '/Users/nemu/dossier/README.md', 'allow')`、
  `('Read', '/Users/nemu/cassette/server/.env', 'DENY:凭据类的文件（.env 之类）不在你可看的范围里')`、
  之后副本内 Read×2 / Edit allow。`.env 被门放行了？ False`。
- transcript `…-dlg-probe-wt-3eh2t52v/47c897a0-6501-42d2-b83c-c35c1ebc8c62.jsonl`：
  L16 Read dossier → L17 tool_result「File does not exist. Note: your current working directory is …」；
  L18 Read .env → L19 tool_result「PreToolUse:Read hook error: 凭据类的文件（.env 之类）不在你可看的范围里」。
- 模型末段原话（截）：「1. dossier README：没读到。读取时提示文件不存在。2. server/.env：没读到。它是凭据类文件，读取被钩子拦截，不在我可看的范围内。3. 改动：server/greet.py 的问候语已从 "hi " 改成 "你好，"……没引用任何内容。」
- 14.8s，num_turns=8，桩 pr_test + pr_open 各一次。
- 探针细节：假副本在 /tmp，不在 READ_ROOTS 里，所以门先放副本内的读、副本外才走 `readonly_path_guard`；生产里 `.worktrees/` 在 CODE_ROOT 下，不需要这一手。`import pipeline` 要把 `server/` 塞进 sys.path，config 读 .env 只读不写，无副作用。

## 腿③：后端重启中断后 resume 接着跑

**结论：三种死法 resume 全接得上，同 session_id，都重做了被打断的 Edit → pr_test → pr_open。但三种死法在 transcript 上留的痕迹不同，runner 判「失败/停下来问」时要分清。被打断的 Edit 在三种模式下都没落盘（tool_use 流到 SDK 时还没执行），所以「认出做到哪」= 它看到文件没改就重改。**

| 模式 | 断点时 transcript 最后几行 | 收到 ResultMessage？ | 第一段耗时 | resume 后第一条 assistant |
|---|---|---|---|---|
| `kill`（出 with → disconnect） | L26 Edit tool_use → L27 tool_result「The user doesn't want to take this action right now. STOP…」（Write / pr_test 同样被拒）→ L34 **又发了一次 api-request** → L38 assistant text「我先停下了。改文件、写 notes.txt 和跑 pr_test 这三个操作都被拒绝了……」 | 否（result=None，SDK 侧 break 了） | 8.8s 断、14.3s 才退出 | 直接 Edit（L44），无文字；末段提 PR |
| `interrupt`（client.interrupt()） | L26 Edit tool_use → L27「The user doesn't want to proceed with this tool use. The tool use was rejected」→ L28 user text「[Request interrupted by user]」 | 是，`is_error=True, subtype=error_during_execution, num_turns=5` | 8.8s | 直接 Edit；末段原话「我第一次改 greet.py 时被你中断，重启后你让我接着做，我才又改了一遍」 |
| `kill9`（SIGKILL CLI 子进程） | L22 api-request（Read 那轮）→ L23 last-prompt → L24 ai-title → L25 atis-latch；**Edit 的 tool_use 没写进 transcript**（SDK 已经收到了 ToolUseBlock） | 否 | 7.9s | 直接 Edit（L31）；第一句文字「notes.txt 写不进去：hook 只允许改工作副本里的文件。我不会绕过这个限制。先把 PR 提上去。」 |

共同点：
- resume 时 CLI 在 user 消息前补了一行 `assistant text="No response requested."`（interrupt 的 L33、kill9 的 L28）；腿①正常结束的 session 没有这行。
- resume 段用时 13–29s，num_turns=5，桩 pr_test + pr_open 各一次，greet.py → `"你好 " + name`（BRIEF_OK 没指定标点）。
- 副本外 notes.txt 在 resume 段照样被门拒（DENY），pytest 改用 pr_test，末段逐条说明——跟 §1.2 同形。
- transcript 路径：`~/.claude/projects/-private-var-folders-…-dlg-probe-wt-<id>/<sid>.jsonl`（realpath 转义，`_` 也变 `-`），探针按 sid 全局 glob 找。

## 跟 PLAN_delegate 现有文字对不上的地方（只列事实）

1. **§3.1 第 3 步 / §4.1 / §8 第 4 条「可以读 dossier 仓」**：`pipeline.DOSSIER_ROOT = ~/dossier` 在这台机上不存在（只有 `~/dossier.DISCARDED-20260906`，活的仓是 `~/casebook`）。闸放行的是一条空路径，模型读到的是「File does not exist」。
2. **§3.2「后端重启会杀掉在跑的派活」**：后端走 SDK disconnect 退出时 CLI 不是立刻死——它把在飞的工具全打成「user doesn't want to take this action」后**再发一次 API 请求**写收尾文字，disconnect 多等了 ~5.5s。只有 SIGKILL 才是真断（transcript 停在上一轮 api-request，没有悬空 tool_use）。三种都能 resume。
3. **§3.1 第 4 步结局判据**：「停下来问 = 正常结束、没调 pr_open」。实测 interrupt 的 ResultMessage 是 `is_error=True / error_during_execution`，disconnect 和 SIGKILL 根本没有 ResultMessage——重启扫尾那条路（§3.2 `running` → resume）不会经过这个判据，没问题；但如果 runner 把「没收到 ResultMessage 且没 pr_open」当「停下来问」会误判，要以状态文件的 `running` 为准。
4. **§3.3 第 4 步 / §6**：resume 回来的 `num_turns` 只算这一段，不累计；`max_turns=40` 若按单段计数，一个派活来回 3 次最多能跑 4×40 轮。
5. **§3.3 「他的回答」**：模型停下来时问了 4 个问题，回答只覆盖 1 个，它对剩下的自己拍板并做了（含改测试文件）。plan 里没写「一次只答部分问题会怎样」——实测是不再问、自己拍、末段写明「未处理」。
6. **§3.1 第 3 步「和他本人同一把闸」**：`readonly_path_guard` 的相对路径按 `neutral_cwd()`（`server/state/claude_cwd`）解析，派活 session 的 cwd 是副本——相对路径的 Grep/Glob 闸查的是空目录、CLI 跑的是副本。副本在 CODE_ROOT 下所以两头都放行，不漏，但「同一把闸」在相对路径上不是同一个基准。
7. **§1 标题** 仍写 0.2.148 / 2.1.251；本次全部在 0.2.174 / 2.1.310 上跑，§1.1 末已有一句升级复测记录，§1.2/§1.3 的数字没按新版重跑（本次 R1 含糊段 3 轮 11.7s，和 §1.3 结论一致）。
8. 2.1.310 transcript 新记录类型实证：`api-request-shape` / `api-request-blob` / `api-request`（带 version、querySource=sdk、shapeHash）每轮一组，可直接数 API 发了几次；另有 `atis-latch`、`ai-title`、`cost-state`、`last-prompt`、`queue-operation`、`mode`、`attachment`（environment/model/session_context/date/credential_org/prompt_snapshot/deferred_tools_record）。
9. 没测到的边：工具已落盘但 tool_result 没写进 transcript 就死（SIGKILL 卡在 Edit 执行和落账之间的窗口）。实测三次 SIGKILL/断都落在执行前。

附：`git status` 里除 delegate_probe.py 外还有 PLAN_delegate.md、其他 *_probe.py 显示 modified / untracked——不是本次改的，是并行会话的。
