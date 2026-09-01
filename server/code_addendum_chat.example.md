# 动手改东西时的纪律（code_addendum_chat.md 的样例）

<!--
把这份拷成 server/code_addendum_chat.md 再改（正式文件 gitignore，不入库）。
写给 {{AGENT_NAME}} 的工地规矩，两个角色共用同一份——纪律不是人格。
占位符 {{AGENT_NAME}} / {{USER_NAME}} 会照 persona 的渲染路替换。
capsule 收场约定（「◆ 结论 ← 出处」）是代码侧机制，常驻在系统提示里，
这份文件不用重复写。
-->

- 提交只 add 自己动过的文件，永远不用 `git add -A`——别人可能也在动手，
  `-A` 会把对方改了一半的文件卷进你的 commit。
- commit 和 push 是两件事：{{USER_NAME}}没点头之前只 commit 不 push。
