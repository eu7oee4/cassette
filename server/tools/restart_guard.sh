#!/bin/bash
# 重启看守（PLAN_prs §6.2）：app 上按「重启后端」时，后端起这个脱离进程、回 202，然后被它杀掉。
#
# 为什么要一个脱离的脚本：机主在手机上按的按钮，后端一停 app 就连不上——起不来就把人锁在门外，
# 连「退回」的按钮都一起没了（restart-backend-one-command 那次事故的放大版）。所以 kill、起、
# 验活、起不来自动退回、再起，全在这一个进程里一次做完，不经过任何要人按的东西。
#
# 只依赖 bash + curl + git + lsof：看守自己挂了，最坏也就是跟今天手动重启一样要人到 Mac 前。
# 回滚只用 git revert，永不 reset --hard（主目录里常有别人没提交的改动）。
#
# 全部参数走环境变量（后端 restart.spawn 填；演练 tests/test_restart_guard.py 填）：
#   GUARD_REPO        仓根（git 在这儿做 revert）
#   GUARD_SERVER_DIR  后端目录（在这儿起 uvicorn）
#   GUARD_UVICORN     uvicorn 可执行文件
#   GUARD_HOST/PORT   监听地址
#   GUARD_OLD_PID     要换掉的旧后端
#   GUARD_REVERTS     起不来时要退回的 PR 合并："<id>:<merge sha> ..."，按合并先后排
#   GUARD_STATE       PR 状态目录：退回标记 _rollback 写这儿，新后端启动时认领
#   GUARD_LOG         后端日志（nohup.out），看守自己的行带 [guard] 前缀
#   GUARD_HEALTH_SEC  验活最多等几秒（默认 60）
#   GUARD_BARK        Bark 地址（空 = 不推）
set -u

REPO=$GUARD_REPO
SERVER_DIR=$GUARD_SERVER_DIR
LOG=$GUARD_LOG
STATE=$GUARD_STATE
HEALTH_SEC=${GUARD_HEALTH_SEC:-60}
LOCK="$STATE/_restarting"

log() { echo "[guard $(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 推给机主的话也进日志：Bark 没配 / 推不出去时，到 Mac 前还能在 nohup.out 里看到
bark() {
  log "→ $1"
  [ -n "${GUARD_BARK:-}" ] || return 0
  curl -s -m 5 -o /dev/null --data-urlencode "title=cassette 后端" \
       --data-urlencode "body=$1" "$GUARD_BARK" || log "Bark 推送失败：$1"
}

finish() { rm -f "$LOCK"; log "看守退出（$1）"; exit "$1"; }

port_holder() { lsof -tiTCP:"$GUARD_PORT" -sTCP:LISTEN 2>/dev/null | head -1; }

# 起一个新后端并验活：活了回 0；没活回 1，并把它杀干净、把它这次的日志尾巴放进 $TAIL
NEW_PID=""
TAIL=""
start_and_check() {
  local start_bytes
  start_bytes=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  ( cd "$SERVER_DIR" && exec nohup "$GUARD_UVICORN" app:app --host "$GUARD_HOST" \
        --port "$GUARD_PORT" >> "$LOG" 2>&1 < /dev/null ) &
  NEW_PID=$!
  log "新后端 pid=${NEW_PID}，验活最多 ${HEALTH_SEC}s"
  local deadline=$(( $(date +%s) + HEALTH_SEC ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    kill -0 "$NEW_PID" 2>/dev/null || break          # 进程已经没了，不用再等
    # 认 pid：旧进程没死透还占着端口时，/health 是旧的在答——那不算活
    if curl -s -m 3 "http://127.0.0.1:$GUARD_PORT/health" | grep -q "\"pid\":$NEW_PID[,}]"; then
      return 0
    fi
    sleep 1
  done
  kill -9 "$NEW_PID" 2>/dev/null
  TAIL=$(tail -c +"$((start_bytes + 1))" "$LOG" 2>/dev/null | grep -v '^\[guard ' | tail -n 30)
  return 1
}

log "开始重启：旧后端 pid=${GUARD_OLD_PID}，待生效的 PR：${GUARD_REVERTS:-无}"
sleep 1   # 让旧后端把 202 回出去

# ① 停旧的：先 TERM 等 15s，不走再 KILL（开着的 SSE 流会拖住 uvicorn 的优雅退出）
kill -TERM "$GUARD_OLD_PID" 2>/dev/null
for _ in $(seq 1 15); do kill -0 "$GUARD_OLD_PID" 2>/dev/null || break; sleep 1; done
kill -0 "$GUARD_OLD_PID" 2>/dev/null && { log "旧后端 15s 没退，KILL"; kill -9 "$GUARD_OLD_PID"; }
for _ in $(seq 1 10); do [ -z "$(port_holder)" ] && break; sleep 1; done
if [ -n "$(port_holder)" ]; then
  # 端口被别的进程占着：这时候起不来不是代码的错，不能拿 PR 开刀
  log "端口 $GUARD_PORT 被 pid=$(port_holder) 占着，没法起"
  bark "重启失败：端口 $GUARD_PORT 被别的进程占着，后端现在没在跑，要到 Mac 前处理"
  finish 1
fi

# ② 起新的
if start_and_check; then
  log "重启成功"
  bark "后端已重启"
  finish 0
fi
log "新后端起不来，日志尾巴：
$TAIL"

# ③ 起不来：退回这一批还没生效过的 PR 合并（分不出是谁弄坏的，一起退）
if [ -z "${GUARD_REVERTS:-}" ]; then
  bark "后端起不来，而且没有待生效的 PR 可退——不是 PR 的事，要到 Mac 前看 nohup.out"
  finish 1
fi
branch=$(git -C "$REPO" symbolic-ref --short -q HEAD)
if [ "$branch" != "main" ]; then
  bark "后端起不来；主目录不在 main 上（在 ${branch:-游离提交}），没敢自动退回——要到 Mac 前处理"
  finish 1
fi

marker="$STATE/_rollback.tmp"
: > "$marker"
ids=""
# 倒着退：后合的先退，冲突最少
for item in $(printf '%s\n' $GUARD_REVERTS | tail -r); do
  id=${item%%:*}
  sha=${item#*:}
  if ! GIT_AUTHOR_NAME=restart-guard GIT_AUTHOR_EMAIL=guard@cassette.local \
       GIT_COMMITTER_NAME=restart-guard GIT_COMMITTER_EMAIL=guard@cassette.local \
       git -C "$REPO" revert -m 1 --no-edit "$sha" >> "$LOG" 2>&1; then
    git -C "$REPO" revert --abort >> "$LOG" 2>&1
    log "退回 #$id 失败"
    bark "后端起不来；自动退回 #$id 时 git 失败了（主目录可能有冲突的改动），要到 Mac 前处理"
    rm -f "$marker"
    finish 1
  fi
  echo "$id $sha $(git -C "$REPO" rev-parse HEAD)" >> "$marker"
  ids="$ids #$id"
done
printf '%s\n' "$TAIL" > "$STATE/_rollback_log"
mv "$marker" "$STATE/_rollback"      # 新后端启动时认领：标 reverted、写原因、通知提的角色
log "已退回${ids}，用退回后的代码再起一次"

# ④ 用退回后的代码再起
if start_and_check; then
  log "退回后起来了"
  bark "#${ids# #} 合进去起不来，已退回，后端已恢复"
  finish 0
fi
log "退回后还是起不来：
$TAIL"
bark "后端起不来，退回了$ids 也没救——要到 Mac 前看 nohup.out"
finish 1
