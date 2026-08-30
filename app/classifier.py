"""收藏标题两段式分类：先规则，不确定再花一次便宜 LLM。

规则层：命中 keywords/useful.txt 或 useless.txt（子串匹配，中文天然无词边界）。
冲突处理：
  1. 强无用信号（如「攻略」/「探店」/「穿搭」…）一票否决 → 无用（防止「AI 攻略」误判有用）。
  2. 其余冲突按匹配长度加权比较，无用优先。
LLM 兜底层：标题两边都不命中（uncertain）才调一次 classify_model，返回 JSON。

verdict: 1 有用 / 0 无用（与 task_db.USEFUL / NOT_USEFUL 一致）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.task_db import NOT_USEFUL, USEFUL, UNCERTAIN

KEYWORDS_DIR = Path(__file__).resolve().parents[1] / "keywords"

# 强无用信号：命中即判无用（一票否决）
STRONG_USELESS = {"攻略", "探店", "穿搭", "种草", "避雷", "开箱", "美食", "食谱",
                  "菜谱", "化妆", "护肤", "减肥", "育儿", "追星", "vlog", "吐槽",
                  "种草"}

_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️‍]+")


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


class TitleClassifier:
    def __init__(self, useful: list[str] | None = None, useless: list[str] | None = None) -> None:
        self.useful = useful if useful is not None else _load(KEYWORDS_DIR / "useful.txt")
        self.useless = useless if useless is not None else _load(KEYWORDS_DIR / "useless.txt")

    def _matches(self, text: str, words: list[str]) -> list[str]:
        """返回 text 中命中的词列表（子串匹配）。"""
        low = text.lower()
        hits = []
        for w in words:
            if not w:
                continue
            if w.isascii() and " " not in w:
                # 英文词按词边界匹配，避免 "ai" 误命中 "said"
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(w.lower())}(?![A-Za-z0-9])", low):
                    hits.append(w)
            else:
                if w in text:
                    hits.append(w)
        return hits

    def rule_verdict(self, title: str, caption: str = "") -> tuple[int, str]:
        """规则层。返回 (verdict, reason)。verdict=UNCERTAIN 表示需要 LLM 兜底。"""
        text = f"{title} {caption}".strip()
        if not text:
            return UNCERTAIN, "标题为空"
        # 强无用信号一票否决
        for w in STRONG_USELESS:
            if w in title:
                return NOT_USEFUL, f"命中强无用信号「{w}」"
        useful_hits = self._matches(text, self.useful)
        useless_hits = self._matches(text, self.useless)
        # 无任何命中 → 需要 LLM 兜底
        if not useful_hits and not useless_hits:
            return UNCERTAIN, "未命中规则"
        useful_score = sum(len(w) for w in useful_hits)
        useless_score = sum(len(w) for w in useless_hits)
        # 无用优先：useless 匹配长度 >= useful 时判无用
        if useless_score >= useful_score:
            reason = f"无用词「{'、'.join(useless_hits)}」"
            if useful_hits:
                reason += f"（同时含「{'、'.join(useful_hits)}」）"
            return NOT_USEFUL, reason
        return USEFUL, f"命中词「{'、'.join(useful_hits)}」"

    def classify(self, title: str, caption: str = "",
                 llm=None, model: str = "") -> tuple[int, str]:
        """完整两段式分类。llm 为可选 LLMClient；无 llm 时 uncertain 保持 UNCERTAIN。"""
        verdict, reason = self.rule_verdict(title, caption)
        if verdict != UNCERTAIN:
            return verdict, reason
        if llm is None:
            return UNCERTAIN, reason
        verdict, reason = _llm_classify(llm, title, caption, model)
        return verdict, reason


def _llm_classify(llm, title: str, caption: str, model: str = "") -> tuple[int, str]:
    """LLM 兜底：根据标题（+正文）判断是否科研/技术相关内容。"""
    cap = (caption or "")[:600].replace("\n", " ")
    prompt = f"""判断以下小红书笔记是否值得作为「科研/技术/工作学习」笔记收录。

判断标准（用户是医学影像 / AI / 大模型研究者）：
- 有用：AI、编程、算法、论文、科研方法、工具技巧、技术教程、行业技术分析等。
- 无用：日常生活分享、旅游/美食/穿搭/购物攻略、娱乐八卦、情绪记录等。

标题：{title}
正文节选：{cap}

只输出 JSON：{{"verdict": "useful" 或 "not_useful", "reason": "一句话原因"}}"""
    system = "你是严格的内容分类器，只输出合法 JSON。"
    raw = llm.chat(prompt, system=system, model=model or None)
    raw = _EMOJI_RE.sub("", raw)
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.S))
    except json.JSONDecodeError:
        # 退路：LLM 输出的文本里包含 useful / not_useful
        if "not_useful" in raw:
            return NOT_USEFUL, raw.strip()[:200]
        if "useful" in raw:
            return USEFUL, raw.strip()[:200]
        return UNCERTAIN, "LLM 输出无法解析"
    verdict = data.get("verdict", "")
    if verdict == "useful":
        return USEFUL, data.get("reason", "LLM 判断有用")
    if verdict == "not_useful":
        return NOT_USEFUL, data.get("reason", "LLM 判断无用")
    return UNCERTAIN, "LLM 判断不确定"
