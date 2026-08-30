"""应用配置。从 .env 读取，密钥不硬编码。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM（DeepSeek / 千问 OpenAI 兼容端点） ----
    llm_provider: str = "deepseek"                 # deepseek | qwen
    llm_model: str = "deepseek-v4-flash"           # 摘要主模型
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    classify_model: str = "deepseek-v4-flash"      # 便宜兜底模型（分类/抽主题）

    # ---- 千问 audio ASR（语音转写） ----
    asr_provider: str = "qwen-audio-3.0-asr-flash"
    asr_api_url: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    asr_api_key: str = Field(default="", validation_alias="ASR_API")

    # ---- Obsidian 笔记输出（与视频项目「学习笔记」岔开） ----
    vault_path: str = ""
    notes_dir: str = "小红书学习"                   # vault 顶层独立目录

    # ---- OCR：apple (本地) 优先，读不懂再升级 qwen-vl ----
    ocr_engine: str = "apple"                      # apple | qwen-vl
    ocr_escalate_min_chars: int = 40               # 本地 OCR 字符数低于此 → 升级云端

    # ---- 小红书采集 ----
    xhs_favorites_limit: int = 20                  # 单次收藏拉取条数（opencli 上限 100）
    xhs_backfill: bool = False
    clean_downloads: bool = True                   # 每次扫描开始前清空 data/downloads 缓存

    # ---- 视频转写上限（秒），超出只靠正文摘要 ----
    stt_max_sec: int = 1800

    @property
    def notes_root(self) -> Path:
        return Path(self.vault_path) / self.notes_dir

    @property
    def attachments_root(self) -> Path:
        return self.notes_root / "attachments"


settings = Settings()
