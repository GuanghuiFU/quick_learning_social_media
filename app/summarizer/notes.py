"""笔记正文生成：合并 正文(caption) + OCR 文本 / STT 转写 → Markdown 笔记 + topics。

单次主模型调用返回正文，并在文末附一行 `<!-- TOPICS: [...] -->` 供解析
（用于关联/MOC 索引，避免额外花一次 LLM 调用抽主题；解析失败则退回空主题）。
"""
from __future__ import annotations

import json
import re

_TOPICS_RE = re.compile(r"<!--\s*TOPICS:\s*(\[[^\]]*\])\s*-->")


def _build_prompt(
    title: str,
    source_url: str,
    caption: str,
    media_text: str,
    media_kind: str,          # "图文" | "视频"
    image_notes: list[str],   # 图片文件清单（供内嵌 ![[..]]）
    tag_hint: str = "",
) -> str:
    media_label = "OCR 识别的图片文字" if media_kind == "图文" else "语音转写文字"
    imgs = ""
    if image_notes:
        imgs = "\n".join(f"- {name}" for name in image_notes)
    tag_line = f"标签：{tag_hint}\n" if tag_hint else ""
    topics_line = "<!-- TOPICS: [\"主题1\",\"主题2\"] -->"
    imgs_block = "=== 图片文件清单（可内嵌到相关小节） ===\n" + imgs if image_notes else ""
    if media_kind == "视频":
        # 视频以转写为主干（对齐 quick_learning），不使用画面截图
        img_rule = "3. 视频内容以语音转写为准，不要引用或猜测画面/图片内容。"
    else:
        img_rule = ("3. 若上面给了图片文件清单，把对应的图片 `![[文件名]]` 内嵌到最相关的"
                    "小节里；只能引用清单里实际存在的文件名。")
    return f"""你是学习笔记助手。请把下面的小红书笔记整理成一份高质量、信息密集的学习笔记。

标题：{title}
来源：{source_url}
{tag_line}
=== 笔记正文（作者原文） ===
{caption[:6000]}

=== {media_label}（视频为转写，图文为图片 OCR） ===
{media_text[:12000]}

{imgs_block}

要求：
1. 输出 Markdown，包含：**概述**、**核心要点**（分小节，用 ## 标题）、**代码/示例**（如有）、**图示**（如有）、**行动项/后续**。
2. 以正文 + {media_label} 合并去重；信息不完整就如实写，不要编造。转写文本来自语音 ASR，可能有识别误差，按语义修正明显的错字。
{img_rule}
4. 去掉营销话术、口语冗余，保持信息密度。
5. 只输出 Markdown 正文。
6. 文末另起一行输出 {topics_line}，给出 2-5 个最贴切的主题词（技术名词/方向，不含笔记标题本身），用于建立笔记间关联。"""


def build_note_body(llm, *, title: str, source_url: str, caption: str,
                    media_text: str, media_kind: str, image_notes: list[str],
                    tag_hint: str = "") -> tuple[str, list[str]]:
    """返回 (markdown 正文, topics)。"""
    prompt = _build_prompt(title, source_url, caption, media_text, media_kind,
                           image_notes, tag_hint)
    raw = llm.chat(prompt, system="你是专业的结构化学习笔记助手，擅长把小红书干货整理为高质量笔记。")
    body, topics = _strip_topics(raw)
    return body, topics


def _strip_topics(markdown: str) -> tuple[str, list[str]]:
    """提取文末 TOPICS 注释并返回 (正文, topics)。"""
    m = _TOPICS_RE.search(markdown)
    if not m:
        return markdown, []
    try:
        topics = json.loads(m.group(1))
        if isinstance(topics, list):
            topics = [str(t).strip() for t in topics if str(t).strip()][:8]
        else:
            topics = []
    except (ValueError, TypeError):
        topics = []
    body = _TOPICS_RE.sub("", markdown).rstrip()
    return body, topics
