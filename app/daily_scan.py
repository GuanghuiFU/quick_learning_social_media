"""daily_scan：每日扫描小红书收藏夹 → 增量构建学习笔记。

流程：
  1. opencli doctor 探桥（不通 → 记 run 后优雅退出，次日再试）
  2. opencli xiaohongshu saved --limit N 拉收藏 → 新 id 登记入库
  3. 对待处理笔记（有用 & 未完成 & 未超重试）逐个处理：
     分类 → 正文 → 下载媒体 → OCR/STT → 摘要 → 写盘 → mark_done
  4. 批量结束后确定性关联 + 重建 MOC 索引
  5. 记录 run 日志

CLI：
  python -m app.daily_scan [--dry-run] [--limit N] [--force-reclassify]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import task_db
from app.classifier import TitleClassifier
from app.config import settings
from app.media import extract_media
from app.notes import linker, writer
from app.summarizer.client import LLMClient
from app.summarizer.notes import build_note_body
from app.xhs import (OpenCLIError, doctor, download_media, fetch_note,
                     fetch_saved, fetch_saved_videos)

BASE = Path(__file__).resolve().parents[1]
DOWNLOADS_DIR = BASE / "data" / "downloads"


def _make_llm() -> LLMClient:
    return LLMClient(settings.llm_api_key, settings.llm_base_url, settings.llm_model)


def _copy_images_to_vault(media_dir: Path) -> list[str]:
    """把下载目录里的图片附件拷进 vault attachments，返回 vault 内文件名列表。"""
    if not media_dir.exists():
        return []
    names = []
    for p in sorted(media_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            dest = writer.attachments_dir() / p.name
            if not dest.exists():
                dest.write_bytes(p.read_bytes())
            names.append(p.name)
    return names


def _process_one(llm, classifier: TitleClassifier, note: dict, *, dry_run: bool) -> str:
    """处理一条笔记，返回阶段状态字符串。失败抛异常（由调用方记 error）。

    返回字符串约定：以「完成 →」或「待处理」开头 = 笔记真正被处理（记入 processed）；
    以「跳过/已分类/规则判定」开头 = 被过滤，不计入 processed。
    """
    note_id = note["note_id"]
    title = note["title"]
    url = note["url"]

    # 终态防护：已判「无用」直接跳过（candidates 已排除，双保险）
    if note["classify"] == task_db.NOT_USEFUL:
        return "跳过"

    # 1. 分类（尚未分类）。dry-run 只规则、不落库。
    if note["classify"] == task_db.UNCERTAIN:
        verdict, reason = classifier.classify(title)
        if verdict == task_db.UNCERTAIN and not dry_run:
            verdict, reason = classifier.classify(title, llm=llm, model=settings.classify_model)
        if dry_run:
            if verdict != task_db.USEFUL:
                return f"跳过：{reason}"
            return "待处理"
        task_db.set_classify(note_id, verdict, reason)
        if verdict != task_db.USEFUL:
            return "跳过"
        note["classify"] = task_db.USEFUL

    if dry_run:
        return "待处理"

    # 2. 正文（入库，断点重试无需再拉）
    if not task_db.is_stage_done(note_id, "caption"):
        detail = fetch_note(url)
        caption = detail.content
        tag_hint = ",".join(detail.tags[:8])
        task_db.set_caption(note_id, caption)
        task_db.set_stage(note_id, "caption")
    else:
        caption = note.get("caption") or ""
        tag_hint = ""

    # 3. 下载媒体 + 提取（视频=音频ASR主链路；图文=分层OCR）
    media_dir = download_media(url, note_id, DOWNLOADS_DIR)
    extract = extract_media(media_dir, title=title)
    if extract.error and not extract.media_text and not extract.stt_skipped:
        raise RuntimeError(extract.error)

    # 4. 摘要 + 主题（视频不用截图，以转写为主干）
    if extract.media_kind == "视频":
        image_names: list[str] = []
    else:
        image_names = _copy_images_to_vault(media_dir)
    body, topics = build_note_body(
        llm,
        title=title,
        source_url=url,
        caption=caption,
        media_text=extract.media_text,
        media_kind=extract.media_kind,
        image_notes=image_names,
        tag_hint=tag_hint,
    )

    # 5. 写盘（note_date = 入库时间，用作文件名前缀 + frontmatter.date；
    # 视频附完整转写原文，便于核对 ASR 链路）
    transcript = extract.media_text if extract.media_kind == "视频" and not extract.stt_skipped else ""
    path = writer.save_note(
        title=title,
        source_url=url,
        body=body,
        author=note.get("author", ""),
        likes=note.get("likes", ""),
        note_type=note.get("note_type", ""),
        topics=topics,
        xhs_id=note_id,
        note_date=note.get("created_at"),
        transcript=transcript,
    )
    # 图文笔记无媒体（正文-only 降级）不标记 media 完成，避免把下载残缺固化为终态
    if extract.media_text or extract.stt_skipped:
        task_db.set_stage(note_id, "media")
    task_db.set_topics(note_id, topics)
    task_db.set_stage(note_id, "notes", path=str(path))
    task_db.clear_error(note_id)
    return f"完成 → {path.name}"


def _clean_downloads() -> None:
    """扫描开始前清空下载缓存（媒体已转成笔记/附件，无需保留原始下载）。

    同一天内的重试仍可复用本次扫描刚下载的文件；跨天则重新拉取。
    """
    if not settings.clean_downloads:
        return
    import shutil

    if DOWNLOADS_DIR.exists():
        try:
            n = sum(1 for p in DOWNLOADS_DIR.iterdir() if p.is_dir())
            shutil.rmtree(DOWNLOADS_DIR, ignore_errors=True)
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            if n:
                print(f"🧹 已清空下载缓存（{n} 个笔记目录）")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 清理下载缓存失败（不影响本次扫描）: {e}")


def run_scan(*, dry_run: bool = False, limit: int | None = None, force_reclassify: bool = False) -> int:
    """执行一次扫描。返回处理成功数。"""
    bridge_ok = 1 if doctor() else 0
    if not bridge_ok:
        task_db.record_run(bridge_ok=0, seen=0, new_seen=0, classified=0,
                           useful=0, processed=0, failed=0,
                           error="opencli 桥不可用（需 opencli doctor + Chrome 登录）")
        print("⚠️  opencli 桥不可用：请运行 `opencli doctor` 确认 daemon+扩展已连接、"
              "Chrome 已登录小红书。本次跳过，次日再试。")
        return 0

    # 处理前清缓存（dry-run 不下载，保留缓存供后续真实运行复用）
    if not dry_run:
        _clean_downloads()

    # 双来源：收藏「笔记」tab（opencli saved）+ 收藏「视频」tab（DOM 抓取）。
    # 视频 tab 失败不阻塞笔记 tab（结构可能随小红书改版变动）。
    favorites = fetch_saved(limit or settings.xhs_favorites_limit)
    try:
        video_favs = fetch_saved_videos(limit or settings.xhs_favorites_limit)
    except OpenCLIError as e:
        video_favs = []
        print(f"⚠️ 收藏视频 tab 抓取失败（不影响笔记 tab）: {str(e)[:120]}")
    merged: list = []
    seen_ids: set[str] = set()
    for fav in [*favorites, *video_favs]:
        if not fav.note_id or fav.note_id in seen_ids:
            continue
        seen_ids.add(fav.note_id)
        merged.append(fav)
    # 两 tab 重复出现的条目：若先登记的 note_type 为空，用带类型的那条补上
    for fav in video_favs:
        if fav.note_type and fav.note_id in seen_ids:
            for i, m in enumerate(merged):
                if m.note_id == fav.note_id and not m.note_type:
                    merged[i] = fav
                    break
    favorites = merged
    new_seen = 0
    for fav in favorites:
        if task_db.register_note(fav.note_id, fav.title, fav.author, fav.url, fav.note_type):
            new_seen += 1

    if force_reclassify:
        for n in task_db.all_notes():
            task_db.set_classify(n["note_id"], task_db.UNCERTAIN, "强制重新分类")

    # 只处理「本次拉取范围内」的候选：既有未完成(含失败重试) + 本次新登记。
    # 避免 --limit 变大时把收藏夹存量全部静默回填（超出用户预期成本）。
    fetched_ids = {fav.note_id for fav in favorites}
    pending = [n for n in task_db.candidates() if n["note_id"] in fetched_ids]
    llm = None if dry_run else _make_llm()
    classifier = TitleClassifier()

    processed = failed = 0
    for note in pending:
        try:
            status = _process_one(llm, classifier, note, dry_run=dry_run)
            print(f"  {note['title'][:40]} — {status}")
            if status.startswith(("完成 →", "待处理")):
                processed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            task_db.record_error(note["note_id"], str(e))
            print(f"  ✗ {note['title'][:40]} 失败: {str(e)[:200]}")

    # 关联 + MOC（仅真实处理后；dry-run 也重建以便预览）
    if not dry_run:
        linker.compute_links()
        linker.apply_related_sections()
        linker.rebuild_moc()

    useful = sum(1 for n in task_db.all_notes() if n["classify"] == task_db.USEFUL)
    task_db.record_run(
        bridge_ok=bridge_ok, seen=len(favorites), new_seen=new_seen,
        classified=len([n for n in pending if n["classify"] == task_db.USEFUL]),
        useful=useful, processed=processed, failed=failed,
    )
    print(f"\n本次扫描：见 {len(favorites)} · 新增 {new_seen} · 处理 {processed} · 失败 {failed}")
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书收藏 → Obsidian 学习笔记 每日扫描")
    parser.add_argument("--dry-run", action="store_true", help="只列待处理，不下载/不调用")
    parser.add_argument("--limit", type=int, default=None, help="拉取收藏条数（默认取 .env）")
    parser.add_argument("--force-reclassify", action="store_true", help="全部重新分类")
    args = parser.parse_args()
    try:
        run_scan(dry_run=args.dry_run, limit=args.limit, force_reclassify=args.force_reclassify)
    except OpenCLIError as e:
        print(f"⚠️  opencli 调用失败: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
