# feishu-wiki-sync

飞书 Wiki 空间 → 本地 Markdown 目录树 同步工具。独立 Python 脚本，仅依赖 `requests`。
为 [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 流水线提供 raw 源料层。

## 特性

- **应用接入**：AppID/AppSecret 换 tenant_access_token，无需用户 OAuth
- **全量遍历**：递归整个 Wiki 空间节点树（可指定起始节点），深度/节点数上限保护、环检测、单节点失败不中断
- **文档转换**：docx（blocks API 表驱动渲染：标题/嵌套列表/代码/表格/待办/引用块/分割线/公式）、sheets、bitable → Markdown
- **表格跳过**：`--skip-tables`（默认开）跳过 bitable/sheet——表格对知识图谱价值低
- **图片落盘**：自动下载 `feishu://image/` 引用到 `assets/`，字节魔数判真实格式，重写为相对路径
- **双闸门增量**（云端始终为准，实测 348 节点增量轮 ~47s）：
  - 闸门 1（树遍历阶段）：`obj_edit_time` 与本地 state 比对，未变文档**零内容 API 调用**
  - 闸门 2（写盘之前）：内容 sha256 确认，版本号变但内容没变则只更新版本不重写
  - 本地文件缺失/新文档 → 强制拉取覆盖（自愈）
- **定时清理**（`--prune`）：源端删除 → 本地 `.md` + state 条目 + **孤儿图片**（无存活文档引用的 assets）+ 空目录四联动清理
- **属性块合并**：文档正文里的 ```` ```text ```` YAML 属性代码块自动提取合并进 frontmatter（兼容任意语言标识），移出正文
- **幂等**：重复执行安全，适合 cron 定时调度

## 设计来源

- 架构参考 OpenViking（AGPL-3.0，仅借鉴思路未抄代码）：tenant token 认证、wiki v2 nodes 递归、block_type 表驱动、skipped_items 容错
- 细节借鉴 obsidian-feishu-importer（MIT）：表格 `cells` + `column_size` 坐标还原、`<br>` 合并/竖杠转义、图片 token 防重名、HTTP 层可注入便于离线测试

## 快速开始

```bash
# 1. 飞书开放平台创建自建应用，开通权限：
#    wiki:wiki:readonly, docx:document:readonly, sheets:spreadsheet,
#    bitable:app:readonly, docs:document.media:download(或 drive:drive:readonly)
#    并将应用添加为目标 Wiki 空间的协作者（或文档所在目录授权）

# 2. 配置: 复制模板并填入凭证与目标
cp .env.example .env   # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_SPACE / SYNC_OUT

# 3. 找到 space_id（填回 .env 的 FEISHU_SPACE）
python3 feishu_wiki_sync.py list-spaces

# 4. 同步整个空间（所有配置来自 .env）
python3 feishu_wiki_sync.py sync

# 5. 定时（推荐用自带流水线脚本：增量同步 + prune + 有变化才 git 提交）
crontab -e   # 加入: 0 * * * * /bin/bash /path/to/feishu-wiki-sync/sync-cron.sh
```

配置优先级：命令行参数 > 已有环境变量 > `.env` 文件。`.env` 已 gitignore，不会进公开仓库。

`sync-cron.sh` 假定输出目录（`SYNC_OUT` 的父目录）是 git 仓库——每轮同步有变化时自动 `git commit`（提交信息含同步统计），git 历史即知识库变更审计线，也是误删恢复的兜底。

## Frontmatter 规范（统一属性）

每个导出文档自动组装规范化 frontmatter，分三段：

```yaml
---
# 1. 同步器管理字段（保留键，内嵌属性不覆盖）
title: 组织指南
source: https://feishu.cn/wiki/<node_token>
revision: 1788241130     # 云端版本号：obj_edit_time（增量比对依据）
imported_at: 2026-09-14
# 2. 文档内嵌属性块自动合并（正文中的 ```text YAML 代码块被提取、移出正文）
type: 指南
department: 工程与交付
tags: [组织架构, 部门总览, ...]
owner: 陈蓓
reviewer: 郑兴
status: 草稿
review: 2026-12-31
summary: "..."
# 3. 附加字段
synced_at: "2026-09-14T16:02:02"
---
```

## 增量与清理语义

| 场景 | 行为 |
|---|---|
| 云端未变 | 跳过，零内容 API 调用 |
| 云端内容更新 | 拉取 → 强制覆盖本地 → state 更新版本 |
| 云端仅元数据变动（版本号变、内容同） | 不重写文件，只更新版本号 |
| 云端新增 | 全量拉取写入 |
| 云端删除 | `--prune` 时：删本地 md + state 条目 + 孤儿图片 + 空目录 |
| 本地文件被删 | 下轮同步检测到缺失，自动重拉（自愈） |
| 本地篡改但云端未变 | 闸门不覆盖——用 git checkout 恢复（兜底设计） |
| 节点本轮拉取失败 | 不触发 prune 误删（seen_paths 已含） |

## 输出结构

```
~/wiki/raw/                     # SYNC_OUT，建议作为 LLM Wiki 的 raw 层
└── 知识库名/
    ├── .feishu_sync_state.json # 增量状态（obj_token → edit_time/sha/path）
    ├── 子目录/                  # 按知识库原始树形结构
    │   ├── 文档.md             # 统一 frontmatter（见上节）
    │   └── assets/             # 该目录文档引用的图片
    │       └── <image_token>.png
    └── ...
```

## 测试

```bash
python3 test_offline.py          # 离线冒烟：全链路 mock（树遍历/转换/表格/图片/二次零重写）
python3 test_incremental.py     # 增量闸门：API 调用计数证明 1 篇变化 → 1 次 blocks 拉取
python3 test_orphan_assets.py   # 孤儿清理：删除文档联动清图 / 存活引用图片不误删
python3 test_inline67.py         # 属性代码块语言标识兼容（真实语料 ```67）
```

全部测试不依赖网络与真实凭证（HTTP 层可注入 mock）。

## 已知限制

- 老版 doc（`/docs/`）飞书已停用内容 API，输出迁移提示
- docx 内嵌的 sheet/bitable 块降级为占位提示；独立 sheet/bitable 文档默认直接跳过（`SYNC_SKIP_TABLES=false` 可开启转换）
- 合并单元格在表格中被简化处理
- `file` 类型节点（xlsx/docx 附件）跳过——需 Drive 文件下载权限且对图谱价值低
