# quick_learning_social_media — 小红书收藏 → Obsidian 学习笔记

仿照 `quick_learning`（视频学习助手）的架构，把**小红书收藏夹**里值得学习的素材（科研/技术干货）自动构建成结构化学习笔记，写入 Obsidian——但目录与视频项目的「学习笔记」**完全岔开**（vault 顶层「小红书学习/」）。

## 工作流程

```
每日扫描 (python -m app.daily_scan)
  │  opencli doctor 探桥（不通 → 优雅退出，次日再试）
  ▼
拉收藏（双来源，按 note_id 去重）：
   · opencli xiaohongshu saved —— 「收藏笔记」tab
   · 浏览器桥 DOM 抓取 —— 「收藏视频」tab（opencli 只抓笔记子标签，
     视频类收藏必须走 subTab=video，2026-08-30 联影笔记漏抓的教训）
  ▼
按【标题】两段式分类（规则 → LLM 兜底）：
   有用（科研/技术）→ 继续
   无用（日常/攻略/探店…）→ 记录决策入库，跳过
  ▼
对每条有用笔记：拉正文 → 下载媒体 → 提取 → LLM 摘要 → 写盘
   · 视频笔记（对齐 quick_learning）：下载视频 → ffmpeg 提取音频 → 千问 ASR
     分段转写（4.5min/段，带 [MM:SS] 时间戳）→ 以转写为主干摘要，不用截图，
     完整转写作为「语音转写（完整附录）」附在笔记末尾便于核对检索
   · 图文笔记：多图按轮播顺序分层 OCR（本地 Apple Vision，读不懂才升级 qwen-vl-ocr）
   · 下载校验：逐项 status 检查 + 失败重试 + 已下载文件幂等复用
   · 缓存清理：每次真实扫描开始前清空 data/downloads（笔记/附件已生成，原始下载不留存；
     开关 CLEAN_DOWNLOADS=true/false）
  ▼
批量后：确定性关联（共享主题 wikilink）+ 重建主题索引 _索引/MOC
  ▼
SQLite 增量控制：已完成永不重做，失败当天复用缓存重试，跨天重新下载（上限 3 次）
```

## 目录结构

```
app/
  config.py          # pydantic-settings，读 .env
  task_db.py         # SQLite：notes / runs / links 三表 + 阶段门控
  xhs.py             # opencli 封装：saved/收藏视频tab/note/download（含状态校验），可注入 fake
  classifier.py      # 标题两段式分类：关键词规则 → 便宜 LLM 兜底
  media.py           # 视频→ffmpeg→千问ASR；图片→分层OCR（本地优先）
  stt/qwen_audio.py  # qwen-audio ASR（4.5min 切段转写，参考 quick_learning）
  ocr/vision.py      # Apple Vision 本地 OCR，稀疏/知识卡片才升级 qwen-vl-ocr
  summarizer/client.py  # OpenAI 兼容 LLM 客户端（deepseek / 千问通用）
  summarizer/notes.py   # 摘要 prompt → Markdown 正文 + TOPICS 注释
  notes/writer.py    # Obsidian 写入：文件名 YYYY-MM-DD_标题.md（入库日期前缀，按时间排序）
  notes/linker.py    # 相关笔记 wikilink + _索引/MOC 重建（零 LLM）
  notes/migrate_rename.py  # 一次性：旧笔记补日期前缀重命名（幂等）
  daily_scan.py      # CLI 入口
keywords/useful.txt useless.txt   # 标题关键词规则
tests/               # pytest（含 fake 桥 mock e2e）
data/                # gitignore：state.db + downloads/<note_id>/
```

## 使用

```bash
# 环境（Python 3.11）
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements.txt   # httpx pydantic-settings pyobjc-framework-Vision

# 配置 .env（密钥已沿用 quick_learning/.env）
cp .env.example .env

# 只预览，不下载不调用（分类仅用规则，不写库）
./.venv/bin/python -m app.daily_scan --dry-run

# 真实处理最近 N 条收藏
./.venv/bin/python -m app.daily_scan --limit 5

# 强制全部重新分类
./.venv/bin/python -m app.daily_scan --force-reclassify

# 测试
./.venv/bin/python -m pytest tests/
```

## 定时（当前已启用：launchd，每天上午 10:03）

由 LaunchAgent `~/Library/LaunchAgents/com.fuguanghui.xhs-learning-daily.plist` 触发，
plist 内显式设置 PATH（anaconda 提供 ffmpeg/ffprobe，/usr/local/bin 提供 opencli），
日志写 `data/scan.log`。管理命令：

```bash
launchctl list | grep xhs-learning                    # 查看状态
launchctl kickstart -k gui/$(id -u)/com.fuguanghui.xhs-learning-daily   # 手动触发一次
launchctl bootout   gui/$(id -u)/com.fuguanghui.xhs-learning-daily      # 停用
# 改时间：编辑 plist 的 StartCalendarInterval → plutil -lint 校验 → bootout + bootstrap 重载
```

**每日扫描覆盖双来源**：收藏「笔记」tab + 收藏「视频」tab（视频类收藏自动纳入笔记生成）。

依赖：opencli 桥可用 + Chrome 已登录小红书（`opencli doctor` 确认）。
桥不可用当次会记录 run(bridge_ok=0) 并次日自动重试，不崩溃、不丢进度。

## 成本克制策略

- 分类：**规则优先**（关键词文件），只对两边都不命中的标题花一次便宜 LLM（`CLASSIFY_MODEL`）。
- OCR：默认 **Apple Vision 本地**；仅当本地读不出（字符数 < 阈值）或标题像知识卡片（整理/图解/一图看懂…）才升级 `qwen-vl-ocr`。
- 视频：音频超 `STT_MAX_SEC`（默认 30min）跳过转写，只靠正文摘要并标注。
- 关联/MOC：**纯确定性**（共享主题/关键词），零 LLM。
- 存量控制：只处理本次拉取窗口内的候选，`--limit` 放大不会静默回填历史收藏。

## 与 quick_learning 的差异

| 项目 | quick_learning | 本项目 |
|---|---|---|
| 素材 | 视频（B站/本地/浏览器捕捉） | 小红书收藏（视频/图文） |
| 分类 | 无（所有视频都做） | 标题两段式（有用/无用） |
| 输入 | 字幕OCR + STT + 截图 | 正文 + 音频ASR（视频）/ 分层OCR（图文） |
| 输出目录 | 学习笔记/`<课程>` | **小红书学习/**（`YYYY-MM-DD_标题` 平铺 + 主题索引） |
| 关联 | 课程内章节 | 跨笔记主题 wikilink + MOC |
