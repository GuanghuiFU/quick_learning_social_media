"""千问 qwen-audio ASR 语音转写（同步版，链路同参考项目）。
上传音频 → 临时 URL → ASR multipart 转写，返回带时间戳的分段。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

FILE_UPLOAD_URL = "https://dashscope.aliyuncs.com/api/v1/files"
_TIMEOUT = httpx.Timeout(300.0, connect=30.0)


class QwenAudioSTT:
    def __init__(self, api_key: str, model: str, api_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.api_url = api_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _upload_get_url(self, audio_path: Path) -> str:
        with httpx.Client(timeout=_TIMEOUT) as client:
            with audio_path.open("rb") as f:
                files = {"file": (audio_path.name, f, "audio/wav")}
                resp = client.post(FILE_UPLOAD_URL, headers=self._headers(), files=files)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            uploaded = (data.get("uploaded_files") or [{}])[0]
            file_id = uploaded.get("file_id")
            if not file_id:
                raise RuntimeError(f"上传失败: {resp.text[:300]}")

            import time as _time
            info = None
            for attempt in range(3):
                info = client.get(f"{FILE_UPLOAD_URL}/{file_id}", headers=self._headers())
                if info.status_code != 429:
                    break
                _time.sleep(3 * (attempt + 1))
            info.raise_for_status()
            url = info.json()["data"].get("url")
            if not url:
                raise RuntimeError(f"无法获取文件 URL: {info.text[:300]}")
            return url

    def transcribe_segments(self, audio_path: Path) -> list[dict]:
        """转写音频，返回 [{text, begin_ms, end_ms, words}] 分段。"""
        if not audio_path.exists():
            raise FileNotFoundError(f"音频不存在: {audio_path}")
        audio_url = self._upload_get_url(audio_path)
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_audio", "input_audio": {"data": audio_url}}
                        ],
                    }
                ]
            },
            "parameters": {"format": "wav", "sample_rate": "16000"},
        }
        headers = {**self._headers(), "Content-Type": "application/json", "X-DashScope-SSE": "disable"}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(self.api_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        inner = data.get("output", {}).get("output", {})
        sentence = inner.get("sentence")
        if not sentence or not sentence.get("text"):
            raise RuntimeError(f"ASR 返回结构异常: {json.dumps(data, ensure_ascii=False)[:500]}")

        words = sentence.get("words") or []
        if not words:
            return [{
                "text": sentence["text"],
                "begin_ms": sentence.get("begin_time", 0),
                "end_ms": sentence.get("end_time", 0),
                "words": [],
            }]
        sentences = []
        cur_words = []
        _SENT_END = ("。", "！", "？", "…", ".", "!", "?", "...")
        for w in words:
            cur_words.append(w)
            if w.get("punctuation") in _SENT_END:
                sentences.append(_flush(cur_words))
                cur_words = []
        if cur_words:
            sentences.append(_flush(cur_words))
        if not sentences:
            sentences = [{
                "text": sentence["text"],
                "begin_ms": sentence.get("begin_time", 0),
                "end_ms": sentence.get("end_time", 0),
                "words": words,
            }]
        return sentences

    def transcribe(self, audio_path: Path) -> str:
        segs = self.transcribe_segments(audio_path)
        return "\n".join(s["text"] for s in segs)

    def transcribe_video_wav(self, wav: Path, chunk_sec: int = 270) -> tuple[str, list[dict]]:
        """把整段 wav 按 chunk_sec 切块转写，返回 (带全局时间戳全文, 全部分句)。

        qwen-audio-3.0-asr-flash 单次上限约 5 分钟 → 切 4.5 分钟段，段内偏移加全局起点。
        """
        import math
        import subprocess
        import tempfile
        import time as _time

        dur = math.ceil(float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True,
        ).stdout.strip() or 0))
        segs = []
        all_segments = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for start in range(0, dur, chunk_sec):
                chunk = td / f"chunk_{start}.wav"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start), "-t", str(chunk_sec), "-i", str(wav), str(chunk)],
                    check=True, capture_output=True,
                )
                try:
                    chunk_segs = self.transcribe_segments(chunk)
                    for s in chunk_segs:
                        if s["text"].strip():
                            s["begin_ms"] += start * 1000
                            s["end_ms"] += start * 1000
                            all_segments.append(s)
                    chunk_text = "\n".join(s["text"] for s in chunk_segs if s["text"].strip())
                    if chunk_text:
                        segs.append((start, chunk_text))
                except Exception as e:  # noqa: BLE001
                    # 单段失败不中断，留空待下次重试该笔记
                    print(f"  [转写段 {start // 60}:{start % 60:02d} 失败] {e}")
                _time.sleep(0.5)  # 轻微限流间隔
        full = "\n".join(f"[{s // 60:02d}:{s % 60:02d}] {t}" for s, t in segs)
        return full, all_segments


def _flush(words: list[dict]) -> dict:
    text = "".join(w.get("text", "") for w in words)
    begin = words[0].get("begin_time", 0)
    end = words[-1].get("end_time", begin)
    return {"text": text, "begin_ms": begin, "end_ms": end, "words": words}
