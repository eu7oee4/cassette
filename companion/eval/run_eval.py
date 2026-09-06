#!/usr/bin/env python3
"""companion 选型自测 runner。

persona × 用户模拟器多轮对话 × 模型矩阵 × n 遍，产出带探针标注的 transcript。
设计定稿见 ../PLAN_companion.md §10.2；评分在 score.py。

用法:
  export DEEPSEEK_API_KEY=... ANTHROPIC_API_KEY=...
  python run_eval.py scenarios/qiuzhao.yaml --models deepseek,haiku --runs 3

输出:
  results/<scenario>/<model>/run<k>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import random
import os

import yaml

# ---------------- providers ----------------
# 注入形状对齐生产（§4.2）：system = persona + 规矩 + 记忆（骨架里静态注入，
# 生产是检索式注当轮尾部）；messages 每条带落库时间戳前缀，首次出现即冻结（§6.3）。


class DeepSeekProvider:
    """OpenAI 兼容接口。自动前缀缓存，无需断点。"""

    def __init__(self, model: str = "deepseek-chat"):
        from openai import OpenAI

        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )
        self.model = model

    def chat(self, system: str, messages: list[dict], max_tokens: int = 1024) -> str:
        r = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, *messages],
        )
        return r.choices[0].message.content or ""


class AnthropicProvider:
    """直调 Messages API（生产同构）。断点放 system 尾，TTL 1h（§4.3）。

    注意：eval 的 persona 往往低于最小可缓存前缀（Haiku 4.5 要 4096 token），
    断点可能静默不生效——无害，保留是为了和生产形状一致。
    """

    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    def chat(self, system: str, messages: list[dict], max_tokens: int = 1024) -> str:
        r = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=messages,
        )
        return "".join(b.text for b in r.content if b.type == "text")


MODELS = {
    "deepseek": lambda: DeepSeekProvider("deepseek-chat"),
    "haiku": lambda: AnthropicProvider("claude-haiku-4-5"),
    "sonnet": lambda: AnthropicProvider("claude-sonnet-5"),
    "opus": lambda: AnthropicProvider("claude-opus-5"),
}


def pick_user_sim(tested: str):
    """用户模拟器用和被测不同族的模型，避免同族腔调互相成全。"""
    return MODELS["haiku"]() if tested == "deepseek" else MODELS["deepseek"]()


# ---------------- prompts ----------------

COMPANION_SYSTEM = """{persona}

# 你记得的事
{memories}

# 规矩
- 消息前的 [MM-DD HH:MM] 是这条消息的发生时间，最新一条的时间就是现在。
- 你只知道上面写着的事。没写的不要编。
- 像人发消息一样说话：短，口语，一次别发一大段。"""

USER_SIM_SYSTEM = """你在扮演一个真实的人，在手机上和自己的 AI 伙伴闲聊。你不是助手，你是用户。

人设与今天的隐藏状态（不要直接说破，按它行事）:
{hidden_state}

说话风格: {style}

纪律:
- 每次只发一两句。短句。口语。可以敷衍、可以答非所问。
- 不要礼貌客套，不要「谢谢你」「你说得对」连发。
- 不许出戏：不评价对方回复的好坏，不提测试。
- 按「本轮桥段」推进，用自己的话说，不照抄桥段文字。"""


# ---------------- run ----------------


def render_history(transcript: list[dict]) -> list[dict]:
    """被测模型视角的 messages。时间戳前缀首次出现即冻结（§6.3）。"""
    return [
        {
            "role": "user" if m["role"] == "user" else "assistant",
            "content": f"[{m['ts']}] {m['content']}",
        }
        for m in transcript
    ]


def sim_user_turn(sim, scenario: dict, transcript: list[dict], beat: str) -> str:
    convo = "\n".join(
        f"{'你' if m['role'] == 'user' else 'TA'}: {m['content']}" for m in transcript
    ) or "(还没开始)"
    system = USER_SIM_SYSTEM.format(
        hidden_state=scenario["user_sim"]["hidden_state"],
        style=scenario["user_sim"]["style"],
    )
    prompt = (
        f"目前的聊天记录:\n{convo}\n\n"
        f"本轮桥段: {beat}\n\n"
        "你接下来发的那条消息（只输出消息本身，不带引号）:"
    )
    return sim.chat(system, [{"role": "user", "content": prompt}], max_tokens=150).strip()


def run_once(scenario: dict, model_name: str, run_idx: int, out_dir: pathlib.Path) -> pathlib.Path:
    tested = MODELS[model_name]()
    sim = pick_user_sim(model_name)

    persona = pathlib.Path(scenario["persona_file"]).read_text(encoding="utf-8")
    system = COMPANION_SYSTEM.format(
        persona=persona.strip(),
        memories="\n".join(f"- {m}" for m in scenario.get("memory", [])) or "（暂无）",
    )
    clock = dt.datetime.fromisoformat(scenario.get("start_time", "2026-09-05T20:00:00"))

    transcript: list[dict] = []
    for step in scenario["user_sim"]["script"]:
        clock += dt.timedelta(minutes=random.randint(1, 4))
        user_msg = sim_user_turn(sim, scenario, transcript, step["beat"])
        transcript.append({
            "role": "user", "content": user_msg,
            "ts": clock.strftime("%m-%d %H:%M"), "probe": step.get("probe"),
        })

        clock += dt.timedelta(minutes=random.randint(1, 3))
        reply = tested.chat(system, render_history(transcript), max_tokens=500)
        transcript.append({
            "role": "companion", "content": reply,
            "ts": clock.strftime("%m-%d %H:%M"), "probe": None,
        })
        tag = step.get("probe") or "-"
        print(f"  [{tag}] 用户: {user_msg[:30]} | 回复: {reply[:30]}")

    out = {
        "scenario": scenario["name"], "model": model_name, "run": run_idx,
        "checks": scenario.get("checks", {}), "transcript": transcript,
    }
    path = out_dir / f"run{run_idx}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help="scenarios/*.yaml")
    ap.add_argument("--models", default="deepseek", help="逗号分隔: deepseek,haiku,sonnet,opus")
    ap.add_argument("--runs", type=int, default=3, help="每格跑几遍（波动靠多跑体现）")
    args = ap.parse_args()

    scenario = yaml.safe_load(pathlib.Path(args.scenario).read_text(encoding="utf-8"))
    for model_name in args.models.split(","):
        out_dir = pathlib.Path("results") / scenario["name"] / model_name
        out_dir.mkdir(parents=True, exist_ok=True)
        for k in range(1, args.runs + 1):
            print(f"== {scenario['name']} × {model_name} × run{k}")
            p = run_once(scenario, model_name, k, out_dir)
            print(f"   -> {p}")


if __name__ == "__main__":
    main()
