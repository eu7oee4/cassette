#!/usr/bin/env python3
"""Cass 搬家（PLAN_multichar M3 iOS 侧）：mianmian 聊天记录 → cassette 会话目录。

输入：mianmian app 的 Documents 整包（给 mianmian 加 UIFileSharingEnabled、重装后
从 Finder 拖出来的那个文件夹，里面有 chat_history.json + ChatImages/ChatFiles/Stickers）。
输出：一个 staging 文件夹，内容原样拖进 cassette app 的 Documents 即完成导入：
    conversations/cass/chat_history.json   ← 转换后的历史
    chat_images/ chat_files/ Stickers/     ← 历史引用到的媒体（目录名换成 cassette 口径）

两边 ChatMessage 都是 Swift 合成 Codable（JSONEncoder 默认策略），因此：
- timestamp 是 2001 纪元秒数 Double，原样拷；id/URL 是字符串，原样拷。
- kind 是 {"case名":{"_0":...}} 结构；mianmian 的 webpage 用了带标签的关联值
  （{"webpage":{"id":..,"title":..}}），与 cassette 的 _0/_1 不同——反正正文网页
  没跟着搬，和 forumAction 一起降级成文本占位（PLAN M3 的口径）。
- 媒体 URL 是绝对沙盒路径，两边 app 都只认 /Documents/ 之后的相对部分
  （AppFiles.reanchored 同款逻辑），所以只需把目录段换名：
  ChatImages→chat_images、ChatFiles→chat_files、Stickers→Stickers。
- Stickers 只搬历史引用到的图，不搬 manifest.json——贴纸库一期全局共用
  cassette 现有那份，覆盖 manifest 会把 TA 的贴纸面板砸了。

未知的 kind 直接报错（宁可在演练时发现新 case，不静默丢消息）。

用法：
    python3 tools/import_cass_chat.py --export-dir ~/Desktop/mianmian_documents \\
        [--out-dir ~/Documents/cass_chat_staging] [--char-id cass]
"""
import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote

DIR_MAP = {"ChatImages": "chat_images", "ChatFiles": "chat_files", "Stickers": "Stickers"}


def die(msg: str) -> None:
    sys.exit(f"❌ {msg}")


def rewrite_media_url(url: str, stats: dict) -> tuple[str, str | None]:
    """绝对沙盒 URL → 目录段换成 cassette 口径。返回 (新 URL, Documents 相对源路径)。
    不含 /Documents/ 的 URL 原样放回（renderer 反正也找不到，计数提醒人工看）。"""
    marker = "/Documents/"
    at = url.find(marker)
    if at < 0:
        stats["url_no_documents"] += 1
        return url, None
    prefix, rel = url[:at + len(marker)], url[at + len(marker):]
    seg, _, rest = rel.partition("/")
    if seg not in DIR_MAP:
        die(f"媒体 URL 落在未知目录「{seg}」：{url}")
    new_rel = DIR_MAP[seg] + "/" + rest
    return prefix + new_rel, rel


def forum_text(info: dict) -> str:
    """forumAction 降级成可读文本占位（内容进历史上下文，卡片交互不带走）。"""
    head = f"〔论坛·{info.get('action', 'post')}〕{info.get('boardName', '')}"
    title = info.get("title", "")
    body = info.get("body", "")
    parts = [head + (f"《{title}》" if title else "")]
    if body:
        parts.append(body)
    return "\n".join(parts)


def transform(messages: list, char_id: str, export_dir: Path, out_dir: Path,
              stats: dict) -> list:
    out = []
    media_jobs: list[tuple[Path, Path]] = []          # (源, 目标)
    for i, m in enumerate(messages):
        kind = m.get("kind")
        if not isinstance(kind, dict) or len(kind) != 1:
            die(f"第 {i} 条消息 kind 结构异常：{kind!r}")
        case, payload = next(iter(kind.items()))
        sender = m.get("sender")
        if sender not in ("me", "other"):
            die(f"第 {i} 条消息 sender 异常：{sender!r}")

        new_kind = None
        if case in ("text", "system", "memoryNote"):
            new_kind = {case: {"_0": payload["_0"]}}
        elif case == "image":
            url, rel = rewrite_media_url(payload["_0"], stats)
            new_kind = {"image": {"_0": url}}
            if rel:
                media_jobs.append((export_dir / rel, out_dir / rewrite_rel(rel)))
        elif case in ("file", "sticker"):
            url, rel = rewrite_media_url(payload["_0"], stats)
            new_kind = {case: {"_0": url, "_1": payload["_1"]}}
            if rel:
                media_jobs.append((export_dir / rel, out_dir / rewrite_rel(rel)))
        elif case == "webpage":
            title = payload.get("title") or payload.get("_1") or ""
            new_kind = {"text": {"_0": f"[网页：{title}]（正文在旧世界，没搬）"}}
            stats["webpage_degraded"] += 1
        elif case == "forumAction":
            info = payload.get("_0") or {}
            new_kind = {"text": {"_0": forum_text(info)}}
            stats["forum_degraded"] += 1
        else:
            die(f"第 {i} 条消息是没见过的 kind「{case}」——先决定映射再跑")

        out.append({
            "id": m["id"],
            "sender": sender,
            "senderID": "me" if sender == "me" else char_id,
            "kind": new_kind,
            "timestamp": m["timestamp"],
        })
        stats["kinds"][case] = stats["kinds"].get(case, 0) + 1

    # Swift 的 file URL 会把非 ASCII 文件名百分号编码；落盘路径要用解码后的真名
    #（URL 字符串本身保持编码——app 那头 URL→path 时会自己解，两边对称）。
    media_jobs = [(Path(unquote(str(s))), Path(unquote(str(d)))) for s, d in media_jobs]
    for src, dst in media_jobs:
        if not src.exists():
            stats["media_missing"] += 1
            stats.setdefault("missing_list", []).append(str(src.name))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
            stats["media_copied"] += 1
    return out


def rewrite_rel(rel: str) -> str:
    seg, _, rest = rel.partition("/")
    return DIR_MAP[seg] + "/" + rest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export-dir", required=True, type=Path,
                    help="mianmian Documents 整包（Finder 拖出来的）")
    ap.add_argument("--out-dir", type=Path,
                    default=Path.home() / "Documents" / "cass_chat_staging")
    ap.add_argument("--char-id", default="cass")
    args = ap.parse_args()

    src = args.export_dir / "chat_history.json"
    if not src.exists():
        die(f"找不到 {src}")
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        die(f"输出目录非空：{args.out_dir}（怕混进旧产物，先清掉）")

    messages = json.loads(src.read_text("utf-8"))
    if not isinstance(messages, list):
        die("chat_history.json 顶层不是数组——格式和预期不符")

    stats = {"kinds": {}, "media_copied": 0, "media_missing": 0,
             "webpage_degraded": 0, "forum_degraded": 0, "url_no_documents": 0}
    out = transform(messages, args.char_id, args.export_dir, args.out_dir, stats)

    dst = args.out_dir / "conversations" / args.char_id / "chat_history.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False), "utf-8")

    print(f"✅ {len(out)} 条消息 → {dst}")
    print(f"   kind 分布：{stats['kinds']}")
    print(f"   媒体：拷了 {stats['media_copied']} 个，缺失 {stats['media_missing']} 个"
          + (f"（{stats['missing_list'][:5]}…）" if stats["media_missing"] else ""))
    print(f"   降级：网页 {stats['webpage_degraded']}，论坛 {stats['forum_degraded']}；"
          f"URL 不含 /Documents/ 的 {stats['url_no_documents']} 条")
    print(f"\n下一步：把 {args.out_dir} 里的东西整体拖进 cassette app 的 Documents（Finder 文件共享）。")


if __name__ == "__main__":
    main()
