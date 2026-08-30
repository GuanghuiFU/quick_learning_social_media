"""媒体提取：视频 → ffmpeg 音频 → 千问 ASR；图片 → 分层 OCR（按 carousel 顺序）。

下载目录约定（opencli download 实测）：
  <output>/<note_id>/<note_id>_1.mp4 ... <note_id>_N.jpg
  视频优先序号 1，图片随后；但 type 不可靠，实际以文件扩展名区分。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.ocr.vision import ocr_text
from app.stt.qwen_audio import QwenAudioSTT

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}


@dataclass
class MediaExtract:
    media_kind: str            # "视频" | "图文"
    media_text: str = ""       # STT 转写 or OCR 拼接
    video_path: Path | None = None
    image_files: list[Path] = field(default_factory=list)  # carousel 顺序
    audio_duration: float = 0.0
    stt_skipped: bool = False  # 超时长上限未转写
    error: str = ""


def _probe_duration(path: Path) -> float:
    """ffprobe 获取视频时长（秒）。失败返回 0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        import json
        data = json.loads(out.stdout)
        return float(data["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def _extract_audio(video: Path, wav: Path) -> bool:
    """ffmpeg 提取 16kHz 单声道 wav。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", str(wav)],
            capture_output=True, text=True, timeout=600,
            check=True,
        )
        return wav.exists() and wav.stat().st_size > 1000
    except Exception:  # noqa: BLE001
        return False


def _list_media(note_dir: Path) -> tuple[list[Path], list[Path]]:
    """返回 (videos, images) 按文件名数字后缀排序（保持 carousel 顺序）。"""
    files = [p for p in note_dir.iterdir() if p.is_file()]
    videos, images = [], []
    for p in files:
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:
            videos.append(p)
        elif ext in IMAGE_EXT:
            images.append(p)
    key = lambda p: [int(x) if x.isdigit() else 0 for x in re.findall(r"(\d+)", p.stem)]
    videos.sort(key=key)
    images.sort(key=key)
    return videos, images


def extract_media(note_dir: Path, *, title: str, force_cloud_ocr: bool = False) -> MediaExtract:
    """从已下载的 note 目录提取媒体文本。"""
    if not note_dir.exists():
        return MediaExtract("", error=f"媒体目录不存在: {note_dir}")

    videos, images = _list_media(note_dir)
    # 优先级：有视频走视频；否则走图片（即便目录里两者都有，视频笔记以视频为主，图片作附图）
    if videos:
        return _extract_video(videos[0], title)
    if images:
        return _extract_images(images, title, force_cloud_ocr)
    return MediaExtract("", error="目录内无视频/图片媒体")


def _has_audio(video: Path) -> bool:
    """ffprobe 检测音轨（参考 quick_learning transcribe_video 的做法）。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def _extract_video(video: Path, title: str) -> MediaExtract:
    """视频笔记主链路：下载的视频 → ffmpeg 提取 16k 音频 → 分段 ASR 转写。

    对齐 quick_learning：以语音转文本为主，不做画面/截图 OCR。
    """
    dur = _probe_duration(video)
    extract = MediaExtract(media_kind="视频", video_path=video, audio_duration=dur)
    if not _has_audio(video):
        # 无音轨：降级为仅正文摘要（不算失败，不触发重试）
        extract.stt_skipped = True
        return extract
    if dur > settings.stt_max_sec:
        extract.stt_skipped = True
        extract.error = f"视频时长 {dur:.0f}s 超过上限 {settings.stt_max_sec}s，仅正文摘要"
        return extract
    wav = video.with_name(video.stem + ".wav")
    try:
        if not _extract_audio(video, wav):
            extract.error = "ffmpeg 提取音频失败"
            return extract
        stt = QwenAudioSTT(settings.asr_api_key, settings.asr_provider, settings.asr_api_url)
        full, _ = stt.transcribe_video_wav(wav)
        extract.media_text = full
        if not full.strip():
            extract.error = "ASR 未返回任何转写文本"
    except Exception as e:  # noqa: BLE001
        extract.error = f"ASR 失败: {str(e)[:300]}"
    finally:
        wav.unlink(missing_ok=True)
    return extract


def _extract_images(images: list[Path], title: str, force_cloud: bool) -> MediaExtract:
    texts = []
    for i, img in enumerate(images, 1):
        t = ocr_text(img, title=title, force_cloud=force_cloud)
        if t:
            texts.append(f"【图{i}】{t}")
        else:
            texts.append(f"【图{i}】（未识别到文字）")
    return MediaExtract(
        media_kind="图文",
        image_files=images,
        media_text="\n\n".join(texts),
    )
