#!/usr/bin/env python3
"""三层评分（设计见 ../PLAN_companion.md §10.2）。

第一层（程序直接数）:
  python score.py auto results/qiuzhao/deepseek
第二层（配对盲评，裁判默认 opus，左右互换压位置偏差）:
  python score.py judge results/qiuzhao/deepseek results/qiuzhao/haiku
第三层: 自己读 transcript，没有命令。重点读①②打分冲突的场次。

已知偏差: Claude 裁判可能偏心 Claude 系回复（同族偏差），盲评+换位能压不能除，
结果里注明。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics

JUDGE_PROMPT = """你在盲评两个 AI 伙伴对同一条用户消息的回复。场景是用户和自己的长期 AI 伙伴闲聊。

用户刚发的消息:
{user_msg}

回复甲:
{a}

回复乙:
{b}

按四个轴分别比较（各一句话）:
1. 自然度——像人发消息，还是像客服/助手
2. 贴合陪伴关系——有自己的立场，不迎合不说教
3. 情绪——接没接住用户此刻的状态
4. 信感——如果用到了记忆，是织进话里，还是在念档案

最后单独一行输出综合结论，只写一个字: 甲 或 乙 或 平"""


def load_runs(d: str) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(pathlib.Path(d).glob("run*.json"))
    ]


def reply_after(transcript: list[dict], idx: int) -> str:
    return next((m["content"] for m in transcript[idx:] if m["role"] == "companion"), "")


# ---------------- 第一层: 程序直接数 ----------------


def auto(run_dir: str) -> None:
    runs = load_runs(run_dir)
    if not runs:
        print(f"{run_dir}: 没有 run*.json")
        return
    for r in runs:
        t = r["transcript"]
        checks = r.get("checks", {})
        lens = [len(m["content"]) for m in t if m["role"] == "companion"]

        # 违禁短语: 助手腔 + 挽留话术（§8.5，应恒为 0）
        banned = checks.get("banned_phrases", [])
        hits = [
            (ph, m["content"])
            for m in t if m["role"] == "companion"
            for ph in banned if ph in m["content"]
        ]

        # 纠偏探针: 探针轮之后 window 轮的回复长度 vs 之前
        corr = None
        idxs = [i for i, m in enumerate(t) if m.get("probe") == "correction"]
        if idxs:
            i = idxs[0]
            window = checks.get("correction", {}).get("window", 4)
            before = [len(m["content"]) for m in t[:i] if m["role"] == "companion"]
            after = [len(m["content"]) for m in t[i:] if m["role"] == "companion"][:window]
            if before and after:
                corr = (sum(after) / len(after)) / (sum(before) / len(before))

        # 记忆硬检查: memory_keywords 里指定探针的回复必须含关键词
        recall = {}
        for probe, kws in checks.get("memory_keywords", {}).items():
            for i, m in enumerate(t):
                if m.get("probe") == probe:
                    reply = reply_after(t, i)
                    recall[probe] = all(k in reply for k in kws)

        corr_s = f"{corr:.2f}" if corr is not None else "n/a"
        print(
            f"run{r['run']}: 回复长度中位数={statistics.median(lens):.0f}字 "
            f"纠偏后/前长度比={corr_s}（<1 才算生效） "
            f"记忆硬命中={recall or 'n/a'} 违禁短语={len(hits)}"
        )
        for ph, c in hits:
            print(f"    ✗ 「{ph}」 in: {c[:50]}")


# ---------------- 第二层: 配对盲评 ----------------


def judge(dir_a: str, dir_b: str, judge_model: str = "opus") -> None:
    from run_eval import MODELS

    j = MODELS[judge_model]()
    runs_a, runs_b = load_runs(dir_a), load_runs(dir_b)
    name_a, name_b = pathlib.Path(dir_a).name, pathlib.Path(dir_b).name
    wins = {name_a: 0, name_b: 0, "平": 0}

    for ra, rb in zip(runs_a, runs_b):
        ta, tb = ra["transcript"], rb["transcript"]
        for i, m in enumerate(ta):
            probe = m.get("probe")
            if not probe or m["role"] != "user":
                continue
            # 对话已按模型分叉，探针名对齐两边
            mi = next((k for k, x in enumerate(tb) if x.get("probe") == probe), None)
            if mi is None:
                continue
            reply_a, reply_b = reply_after(ta, i), reply_after(tb, mi)

            for a_first in (True, False):  # 左右各一遍，压位置偏差
                x, y = (reply_a, reply_b) if a_first else (reply_b, reply_a)
                verdict = j.chat(
                    "你是严格的盲评裁判。",
                    [{"role": "user", "content": JUDGE_PROMPT.format(
                        user_msg=m["content"], a=x, b=y)}],
                    max_tokens=400,
                ).strip().splitlines()[-1]
                if "甲" in verdict:
                    wins[name_a if a_first else name_b] += 1
                elif "乙" in verdict:
                    wins[name_b if a_first else name_a] += 1
                else:
                    wins["平"] += 1
            print(f"  probe={probe} 累计: {wins}")

    total = sum(wins.values()) or 1
    print(f"\n配对盲评 {name_a} vs {name_b}（裁判={judge_model}，含左右互换）:")
    for k, v in wins.items():
        print(f"  {k}: {v} ({v / total:.0%})")
    print("  ⚠️ 裁判为 Claude 时对 Claude 系回复可能有同族偏差，结论交叉参考第一/三层。")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("auto")
    p1.add_argument("run_dir")
    p2 = sub.add_parser("judge")
    p2.add_argument("dir_a")
    p2.add_argument("dir_b")
    p2.add_argument("--judge", default="opus", choices=["opus", "sonnet", "haiku", "deepseek"])
    args = ap.parse_args()
    if args.cmd == "auto":
        auto(args.run_dir)
    else:
        judge(args.dir_a, args.dir_b, args.judge)


if __name__ == "__main__":
    main()
