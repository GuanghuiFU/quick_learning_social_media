"""OpenCLI（@jackwener/opencli）小红书适配器封装。

命令契约（均已实测）：
  opencli doctor                                   → 桥/扩展/连接健康
  opencli xiaohongshu saved [id] --limit N -f json → [{"rank","id","title","author","likes","type","url"}]
  opencli xiaohongshu note <url> -f json           → [{"field","value"}...] 含 title/author/content/likes/collects/comments/tags
  opencli xiaohongshu download <url> --output <dir> -f json
                                                   → [{"index","type","status","size"}...] 媒体落盘 <dir>/<note_id>/

测试时注入 fake：XHS = FakeXHS()（实现同名字段/方法），xhs.py 不感知。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_OPENCLI = shutil.which("opencli") or "/usr/local/bin/opencli"


@dataclass
class SavedNote:
    note_id: str
    title: str
    author: str = ""
    likes: str = ""
    note_type: str = ""
    url: str = ""


@dataclass
class NoteDetail:
    title: str = ""
    author: str = ""
    content: str = ""
    likes: str = ""
    collects: str = ""
    comments: str = ""
    tags: list[str] = field(default_factory=list)


class OpenCLIError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 180) -> str:
    try:
        proc = subprocess.run(
            [_OPENCLI, *args], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise OpenCLIError(f"opencli 超时: {' '.join(args[:4])}...") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise OpenCLIError(f"opencli 失败({proc.returncode}): {tail}")
    return proc.stdout


def doctor() -> bool:
    """桥健康检查：daemon + 扩展 + 连接三项 [OK]。

    注意：不能用「Everything looks good」结尾文案判断——opencli 有新版本提示时
    会输出 Issues: 段替换该文案，但桥实际健康（8/25-8/28 连判失败的根因）。
    _run 对非零退出码抛异常；这里逐项核对三个 [OK]，版本更新提示不阻塞。
    """
    try:
        out = _run(["doctor"], timeout=60)
    except OpenCLIError:
        return False
    return ("[OK] Daemon" in out and "[OK] Extension" in out
            and "[OK] Connectivity" in out)


def _eval(js: str, timeout: int = 90) -> str:
    return _run(["browser", "default", "eval", js], timeout=timeout)


# 收藏「视频」子标签页的 DOM 提取（选择器参照 opencli collection-helpers）
_FAVORITES_VIDEO_DOM_JS = """
(() => {
  const normalizeUrl = (href) => {
    if (!href) return '';
    let url; try { url = new URL(href, 'https://www.xiaohongshu.com'); } catch { return ''; }
    if (url.hostname !== 'www.xiaohongshu.com') return '';
    if (!url.searchParams.get('xsec_token')) return '';
    return url.toString();
  };
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const results = []; const seen = new Set();
  document.querySelectorAll('section.note-item').forEach((el) => {
    const titleEl = el.querySelector('.title, .note-title, a.title, .footer .title span');
    const nameEl = el.querySelector('a.author .name, .author-name, .nick-name, .name');
    const likesEl = el.querySelector('.count, .like-count, .like-wrapper .count');
    // 逐个找带 xsec_token 的链接：视频条目首个 <a> 是无 token 的 /explore/ 裸链，
    // 不能用 querySelector 多选择器（按文档序取首个匹配，会命中裸链）
    let url = '';
    el.querySelectorAll('a').forEach((a) => {
      if (url) return;
      const u = normalizeUrl(a.getAttribute('href') || '');
      if (u) url = u;
    });
    if (!url) return;
    const seg = url.split('?')[0].split('/').pop() || '';
    const id = /^[0-9a-f]{24}$/i.test(seg) ? seg : '';
    if (!id || seen.has(id)) return;
    seen.add(id);
    results.push({id, title: clean(titleEl && titleEl.textContent),
                  author: clean(nameEl && nameEl.textContent),
                  likes: clean(likesEl && likesEl.textContent), url});
  });
  return JSON.stringify(results);
})()
"""


def current_user_id() -> str:
    """解析当前登录用户 id（opencli 同款：__INITIAL_STATE__.user.userInfo）。"""
    _run(["browser", "default", "open", "https://www.xiaohongshu.com/explore"], timeout=120)
    import time as _t
    _t.sleep(2)
    out = _eval("""(() => {
      const user = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user && window.__INITIAL_STATE__.user.userInfo;
      const info = (user && user._value) ? user._value : (user || {});
      return info.user_id || info.userId || info.userID || '';
    })()""")
    uid = out.strip().strip('"')
    if not re.fullmatch(r"[0-9a-f]{24}", uid):
        raise OpenCLIError(f"无法解析当前登录用户 id（输出: {out[:80]!r}）—— 请确认 Chrome 已登录小红书")
    return uid


def fetch_saved_videos(limit: int = 20) -> list[SavedNote]:
    """拉取「收藏视频」子标签页。

    opencli saved 只抓 subTab=note（收藏笔记），视频类收藏在 subTab=video，
    需直接驱动浏览器桥抓取 DOM（8/30 联影笔记漏抓的根因）。
    """
    import time as _t

    if not (1 <= limit <= 100):
        limit = 20
    uid = current_user_id()
    _run(["browser", "default", "open",
          f"https://www.xiaohongshu.com/user/profile/{uid}?tab=fav&subTab=video"], timeout=120)
    _t.sleep(3)
    rows: list[dict] = []
    prev = -1
    for _ in range(8):  # 滚动加载直到够数或不再增长
        out = _eval(_FAVORITES_VIDEO_DOM_JS)
        try:
            data = json.loads(out)
            if isinstance(data, str):
                data = json.loads(data)
            rows = data if isinstance(data, list) else []
        except json.JSONDecodeError:
            rows = []
        if len(rows) >= limit or len(rows) == prev:
            break
        prev = len(rows)
        _eval("window.scrollTo(0, document.body.scrollHeight); 'ok'")
        _t.sleep(2.5)
    notes = []
    for r in rows[:limit]:
        url = r.get("url", "")
        if not url or "xsec_token" not in url:
            continue
        notes.append(SavedNote(
            note_id=r.get("id", ""),
            title=r.get("title", ""),
            author=r.get("author", "") or "",
            likes=str(r.get("likes", "") or ""),
            note_type="video",
            url=url,
        ))
    return notes


def fetch_saved(limit: int = 20) -> list[SavedNote]:
    """拉取当前登录用户收藏。返回按 rank 排序的笔记列表。"""
    if not (1 <= limit <= 100):
        limit = 20
    out = _run(["xiaohongshu", "saved", "--limit", str(limit), "-f", "json"])
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as e:
        raise OpenCLIError(f"saved 输出非 JSON: {out[:300]}") from e
    notes = []
    for r in rows:
        url = r.get("url", "")
        # 必须带 xsec_token 的签名 URL 才有效（否则后续 note/download 无法鉴权）
        if not url or "xsec_token" not in url:
            continue
        notes.append(SavedNote(
            note_id=r.get("id", ""),
            title=r.get("title", ""),
            author=r.get("author", "") or "",
            likes=str(r.get("likes", "") or ""),
            note_type=r.get("type", "") or "",
            url=url,
        ))
    return notes


def fetch_note(url: str) -> NoteDetail:
    """获取笔记正文 + 互动数据 + tags。"""
    out = _run(["xiaohongshu", "note", url, "-f", "json"])
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as e:
        raise OpenCLIError(f"note 输出非 JSON: {out[:300]}") from e
    d = NoteDetail()
    for r in rows:
        f = r.get("field", "")
        v = r.get("value", "") or ""
        if f == "title":
            d.title = v
        elif f == "author":
            d.author = v
        elif f == "content":
            d.content = v
        elif f == "likes":
            d.likes = v
        elif f == "collects":
            d.collects = v
        elif f == "comments":
            d.comments = v
        elif f == "tags":
            d.tags = [t.strip().lstrip("#") for t in re.split(r"[,#\s]+", v) if t.strip()]
    return d


_MEDIA_EXT = {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}


def _media_files(d: Path) -> list[Path]:
    return [p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in _MEDIA_EXT] if d.exists() else []


def download_media(url: str, note_id: str, output_dir: Path) -> Path:
    """下载笔记媒体到 <output_dir>/<note_id>/，返回该目录。

    校验强化（8/30 联影笔记视频静默缺失的教训）：
      - 已有媒体文件 → 直接复用（断点重试免重复下载）
      - 解析 opencli download 的逐项 status，有 failed → 重试（最多 3 次）
      - 最终目录无媒体文件 → 抛错，让这条笔记标记失败、次日重试
    """
    import time as _t

    output_dir = Path(output_dir)
    dest = output_dir / note_id
    if _media_files(dest):
        return dest
    output_dir.mkdir(parents=True, exist_ok=True)

    last_status = ""
    for attempt in range(3):
        out = _run(["xiaohongshu", "download", url, "--output", str(output_dir), "-f", "json"])
        try:
            items = json.loads(out)
            if isinstance(items, str):
                items = json.loads(items)
        except json.JSONDecodeError:
            items = []
        failed = [i for i in items if isinstance(i, dict) and i.get("status") != "success"]
        last_status = ",".join(f"{i.get('type')}:{i.get('status')}"
                               for i in items if isinstance(i, dict))
        if not failed and items:
            break
        _t.sleep(3 * (attempt + 1))
    if not _media_files(dest):
        raise OpenCLIError(f"媒体下载不完整（{last_status or '无输出'}），目录无文件")
    return dest
