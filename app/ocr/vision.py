"""分层 OCR：默认 Apple Vision 本地（快、免费、中文可用）；本地读不出的图升级 qwen-vl-ocr。

触发升级（满足其一，避免"每个 OCR 都调大模型"）：
  1. 本地结果字符数 < OCR_ESCALATE_MIN_CHARS（默认 40）→ 大概率是手写/复杂图/清晰度低。
  2. 标题命中知识卡片信号（整理/合集/图解/避坑/原理/一图看懂/思维导图…）→ 长图直接云端。
图片文件必须本身存在于磁盘（对 PDF 多页图会逐页 OCR）。
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from app.config import settings

_CARD_SIGNAL = ("整理", "合集", "图解", "避坑", "原理", "一图看懂", "思维导图", "干货", "汇总", "框架", "流程图")
_EMPTY_RE = re.compile(r"\s+")

_QWEN_VL_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


class VisionOCR:
    """Apple Vision OCR（pyobjc）。返回 [{text, x, y, w, h}]，归一化，y 自顶向下。"""

    def ocr_image(self, path: Path) -> list[dict]:
        import Vision
        import Quartz

        path = Path(path)
        url = Quartz.CFURLCreateFromFileSystemRepresentation(
            None, bytes(str(path), "utf-8"), len(str(path).encode("utf-8")), False
        )
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return []
        cgimage = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if cgimage is None:
            return []
        w = Quartz.CGImageGetWidth(cgimage)
        h = Quartz.CGImageGetHeight(cgimage)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        langs = ["zh-Hans", "zh-Hant", "en-US"]
        if hasattr(req, "setRecognitionLanguages_"):
            try:
                req.setRecognitionLanguages_(langs)
            except Exception:  # noqa: BLE001
                pass
        handler.performRequests_error_([req], None)
        out = []
        for obs in req.results() or []:
            box = obs.boundingBox()
            cand = obs.topCandidates_(1)
            if cand and len(cand) > 0:
                out.append({
                    "text": cand[0].string(),
                    "x": box.origin.x * w, "y": (1 - box.origin.y - box.size.height) * h,
                    "w": box.size.width * w, "h": box.size.height * h,
                })
        return out


class QwenVlOCR:
    """千问视觉 OCR（qwen-vl-ocr）。图片以 base64 内联发送（图片通常 <5MB）。"""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def ocr_image(self, path: Path) -> list[dict]:
        import base64

        path = Path(path)
        b64 = base64.b64encode(path.read_bytes()).decode()
        payload = {
            "model": "qwen-vl-ocr",
            "input": {"messages": [{"role": "user", "content": [
                {"image": f"data:image/jpeg;base64,{b64}"},
                {"text": "提取图中所有文字，按阅读顺序输出，保留代码/公式原样。"},
            ]}]},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = httpx.post(_QWEN_VL_URL, json=payload, headers=headers, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        inner = data.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(inner, list):
            inner = "".join(c.get("text", "") for c in inner if isinstance(c, dict))
        return [{"text": line.strip(), "x": 0, "y": 0, "w": 0, "h": 0}
                for line in str(inner).splitlines() if line.strip()]


def _engine_for():
    engine = (settings.ocr_engine or "apple").lower()
    if engine == "qwen-vl":
        return QwenVlOCR(settings.asr_api_key)
    return VisionOCR()


def _text_len(texts: list[dict]) -> int:
    return len(_EMPTY_RE.sub("", "".join(r["text"] for r in texts)))


def ocr_text(path: Path, *, title: str = "", force_cloud: bool = False) -> str:
    """整图 OCR 返回纯文本。分层：本地 → 稀疏/知识卡片 → 云端。"""
    path = Path(path)
    if not path.exists():
        return ""
    text = ""
    need_cloud = False
    if not force_cloud:
        try:
            rows = _engine_for().ocr_image(path)
            text = " ".join(r["text"] for r in rows)
            need_cloud = _text_len(rows) < settings.ocr_escalate_min_chars
            if not need_cloud and title:
                need_cloud = any(sig in title for sig in _CARD_SIGNAL)
            if not need_cloud:
                return text
        except Exception:  # noqa: BLE001
            need_cloud = True
    # 云端兜底
    try:
        rows = QwenVlOCR(settings.asr_api_key).ocr_image(path)
        return " ".join(r["text"] for r in rows)
    except Exception:  # noqa: BLE001
        if text and need_cloud:
            return text
        return ""
