"""分类器单测：规则层 + LLM 兜底 + 冲突处理。"""
from __future__ import annotations

from app.classifier import TitleClassifier, _llm_classify
from app.task_db import NOT_USEFUL, UNCERTAIN, USEFUL


class FakeLLM:
    """模拟便宜分类模型，返回可预测 JSON。"""
    def __init__(self, verdict: str, reason: str = "test") -> None:
        self.verdict = verdict
        self.reason = reason

    def chat(self, prompt, system="", model=None, **kw):
        return f'{{"verdict": "{self.verdict}", "reason": "{self.reason}"}}'


def c():
    return TitleClassifier()


def test_useful_tech_title():
    v, r = c().rule_verdict("nnU-Net 医学影像分割复现教程")
    assert v == USEFUL
    assert "分割" in r


def test_useless_life_title():
    v, r = c().rule_verdict("杭州探店｜这家日料值得去")
    assert v == NOT_USEFUL


def test_useless_strong_veto_priority():
    # "AI 攻略" 既含技术词也含强无用信号 → 一票否决
    v, r = c().rule_verdict("AI 攻略：一周瘦 10 斤")
    assert v == NOT_USEFUL


def test_ambiguous_needs_llm():
    v, r = c().rule_verdict("Matt Pocock Skills v1.2 更新")
    assert v == UNCERTAIN


def test_llm_fallback_useful():
    c_ = c()
    v, r = c_.classify("一些英文播客节目推荐", llm=FakeLLM("useful", "技术播客"))
    assert v == USEFUL


def test_llm_fallback_not_useful():
    c_ = c()
    v, r = c_.classify("周末去哪儿玩", llm=FakeLLM("not_useful", "生活分享"))
    assert v == NOT_USEFUL


def test_no_llm_keeps_uncertain():
    c_ = c()
    v, r = c_.classify("一些模糊标题")
    assert v == UNCERTAIN


def test_llm_classify_handles_markdown_fence():
    raw = '```json\n{"verdict": "useful", "reason": "含代码"}\n```'
    class FenceLLM:
        def chat(self, prompt, system="", model=None, **kw):
            return raw
    v, r = _llm_classify(FenceLLM(), "标题", "")
    assert v == USEFUL
