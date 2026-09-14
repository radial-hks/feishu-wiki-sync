# feishu-wiki-sync

飞书 Wiki 空间 → 本地 Markdown 目录树 同步工具。独立 Python 脚本，仅依赖 `requests`。

## 特性

- **应用接入**：AppID/AppSecret 换 tenant_access_token，无需用户 OAuth
- **全量遍历**：递归整个 Wiki 空间节点树（可指定起始节点），深度/节点数上限保护
- **文档转换**：docx（blocks API 表驱动渲染）、sheets、bitable → Markdown
- **图片落盘**：自动下载 `feishu://image/` 引用到 `assets/`，字节魔数判真实格式，重写为相对路径
- **增量同步**：`obj_edit_time` + 内容 sha256 双重判定，无变化的文件跳过写入（状态存 `.feishu_sync_state.json`）
- **幂等**：重复执行安全，适合 cron 定时调度

## 设计来源

- 架构参考 OpenViking（AGPL-3.0，仅借鉴思路未抄代码）：tenant token 认证、wiki v2 nodes 递归、block_type 表驱动、`skipped_items` 容错
- 细节借鉴 obsidian-feishu-importer（MIT）：表格 `cells` + `column_size` 坐标还原、`<br>` 合并/竖杠转义、图片 token 防重名、HTTP 层可注入便于离线测试

## 使用

```bash
# 1. 飞书开放平台创建自建应用，开通权限：
#    wiki:wiki:readonly, docx:document:readonly, sheets:spreadsheet,
#    bitable:app:readonly, docs:document.media:download(或 drive:drive:readonly)
#    并将应用添加为目标 Wiki 空间的协作者（或文档所在目录授权）

# 2. 找到 space_id
python3 feishu_wiki_sync.py list-spaces --app-id X --app-secret Y

# 3. 同步整个空间
python3 feishu_wiki_sync.py sync --app-id X --app-secret Y \
    --space <space_id> --out ./output

# 4. 定时（cron 示例，每小时）
FEISHU_APP_ID=X FEISHU_APP_SECRET=Y
0 * * * * python3 /path/to/feishu_wiki_sync.py sync --space <id> --out ./output >> sync.log 2>&1
```

## 输出结构

```
output/
├── .feishu_sync_state.json      # 增量状态（token -> edit_time/sha）
├── 根目录/
│   ├── 子文档A.md               # frontmatter: feishu_title / feishu_doc
│   ├── 数据表B.md
│   └── assets/                  # 该目录下所有文档的图片
│       └── <image_token>.png
```

## 测试

```bash
python3 test_offline.py   # 离线冒烟测试，mock 飞书 API，不发真实请求
```

## 已知限制

- 老版 doc（`/docs/`）飞书已停用内容 API，输出迁移提示
- 内嵌 sheet/bitable（docx 内部）降级为占位提示，独立 sheet/bitable 文档正常转换
- 合并单元格在表格中被简化处理
