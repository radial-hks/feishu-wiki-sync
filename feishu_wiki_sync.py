#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 Wiki 空间 -> 本地 Markdown 目录树 同步工具。

应用接入（AppID/AppSecret, tenant_access_token），定时全量遍历指定 Wiki 空间，
将 docx/sheet/bitable 文档转换为 Markdown 落盘。幂等：重复执行只重写有变化
（obj_edit_time 变化或内容哈希变化）的文件。

配置通过仓库根目录的 .env 提供（模板见 .env.example），
凭证优先级: 命令行参数 > 已有环境变量 > .env 文件。

用法:
  # 首次: 复制 .env.example 为 .env，填入 FEISHU_APP_ID / FEISHU_APP_SECRET

  # 列出应用可访问的 wiki 空间（找 space_id）
  python3 feishu_wiki_sync.py list-spaces

  # 全量同步（space/out 等从 .env 读取）
  python3 feishu_wiki_sync.py sync

  # 定时同步 + 清理（cron 推荐）
  python3 feishu_wiki_sync.py sync --prune

.env 关键项:
  FEISHU_APP_ID / FEISHU_APP_SECRET   飞书自建应用凭证
  FEISHU_SPACE                        目标空间 ID
  FEISHU_ROOT                         起始节点 token（空=空间根全量）
  SYNC_OUT                            输出目录（如 ~/wiki/raw）
  SYNC_DOWNLOAD_IMAGES                是否下载图片（默认 true）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:
    print("需要 requests: pip install requests", file=sys.stderr)
    raise

LOG = logging.getLogger("feishu_sync")


def load_env_file(env_path: Optional[str] = None) -> None:
    """加载 .env 到环境变量（不覆盖已存在的值）。

    查找顺序: 显式路径 > 脚本同目录 .env > 当前目录 .env
    格式: KEY=VALUE，支持 # 注释与引号值。
    """
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / ".env")
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text("utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        LOG.debug("已加载 env: %s", path)
        return
    LOG.debug("未找到 .env，跳过")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_BASE = "https://open.feishu.cn"
API = "/open-apis"

# docx block_type -> 内容属性名（参考飞书官方文档 + lark-oapi Block 模型）
BLOCK_TYPE_TO_ATTR = {
    1: "page", 2: "text", 3: "heading1", 4: "heading2", 5: "heading3",
    6: "heading4", 7: "heading5", 8: "heading6", 9: "heading7",
    10: "heading8", 11: "heading9", 12: "bullet", 13: "ordered",
    14: "code", 15: "quote", 17: "todo", 18: "bitable", 19: "callout",
    22: "divider", 23: "iframe", 26: "image_view", 27: "image",
    30: "sheet", 31: "table", 32: "table_cell", 33: "view",
    34: "quote_container", 35: "task", 36: "okr", 40: "add_ons",
    42: "chat_card", 43: "link_preview", 46: "file",
}
WIKI_TYPE_MAP = {"doc": "doc", "docx": "docx", "sheet": "sheets",
                 "sheets": "sheets", "bitable": "base", "base": "base"}
FILE_SUFFIX = ".md"
MAX_NAME_CHARS = 80  # 目录/文件名安全长度

WINDOWS_RESERVED = {"con", "prn", "aux", "nul",
                    "com1", "com2", "com3", "com4", "com5", "com6",
                    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3",
                    "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"}

IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),  # 需再验证 WEBP
)

# 图片占位引用格式: ![alt](feishu://image/<token>)
FEISHU_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(feishu://image/([^)]+)\)")

# ---------------------------------------------------------------------------
# 文档内嵌属性块检测（正文开头/结尾的 ```text YAML 代码块）
# ---------------------------------------------------------------------------

INLINE_PROPS_FENCE_RE = re.compile(
    r"```[a-z0-9_-]*\s*\n(---\n.*?\n---)\s*\n```", re.DOTALL)

# frontmatter 中由同步器管理、内嵌属性不得覆盖的保留键
RESERVED_FRONTMATTER_KEYS = frozenset({
    "title", "source", "revision", "imported_at", "synced_at", "sha"})


def extract_inline_props(body: str) -> tuple:
    """从正文中提取内嵌属性代码块。

    返回 (props_dict, cleaned_body)。只识别包含 YAML frontmatter
    (--- ... ---) 的 ```text/yaml 代码块；没有则返回 ({}, body) 原样。
    """
    match = INLINE_PROPS_FENCE_RE.search(body)
    if not match:
        return {}, body
    raw = match.group(1)
    props = _parse_simple_yaml(raw)
    if not props:
        return {}, body
    # 移除代码块（含其后紧邻的空行），保留其余正文
    cleaned = body[:match.start()] + body[match.end():]
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return props, cleaned


def _parse_simple_yaml(raw: str) -> dict:
    """解析扁平 YAML（key: value / key: [a, b]），够用即可，不引依赖。

    多行值（summary: "...") 按带引号字符串或折叠到单行处理。
    """
    lines = raw.strip().strip("-").strip().splitlines()
    props: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            # 可能是多行值（下一行缩进引号串）——尽力拼接
            while i < len(lines) and lines[i].strip() and ":" not in lines[i]:
                value += (" " if value else "") + lines[i].strip()
                i += 1
            if not value:
                props[key] = ""
                continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            props[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
        else:
            props[key] = value.strip("'\"")
    return props


def yaml_value(v) -> str:
    """把值渲染回 YAML 安全的标量。"""
    if isinstance(v, list):
        return "[" + ", ".join(yaml_value(x) for x in v) + "]"
    s = str(v)
    if any(c in s for c in ":#{}[]\"'&*!|>%@`,\n") or s != s.strip() or not s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_frontmatter(title: str, source_url: str, revision: Any,
                      imported_at: str, inline_props: Optional[dict],
                      extra: Optional[dict] = None) -> str:
    """组装统一规范的 frontmatter。

    结构分三段：
      1. 同步器管理字段（title/source/revision/imported_at）
      2. 内嵌属性块合并进来的业务字段（type/department/tags/owner/...，
         仅补缺，不覆盖保留键）
      3. extra 附加字段（如 synced_at）
    """
    lines = ["---",
             f"title: {yaml_value(title)}",
             f"source: {yaml_value(source_url)}",
             f"revision: {yaml_value(str(revision) if revision is not None else '')}",
             f"imported_at: {yaml_value(imported_at)}"]
    for key, value in (inline_props or {}).items():
        if key.lower() in RESERVED_FRONTMATTER_KEYS or key in lines[1:4]:
            continue
        lines.append(f"{key}: {yaml_value(value)}")
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def sanitize_name(name: str, fallback: str = "untitled") -> str:
    """文件/目录名净化：非法字符、Windows 保留名、长度。"""
    safe = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", str(name or "")).strip(" ._")
    safe = re.sub(r"\s+", " ", safe)
    if not safe:
        safe = fallback
    if safe.lower() in WINDOWS_RESERVED:
        safe += "-"
    if safe.startswith("."):
        safe = "_" + safe
    if len(safe) > MAX_NAME_CHARS:
        safe = safe[:MAX_NAME_CHARS].rstrip(" ._") or fallback
    return safe


def guess_image_ext(content: bytes, content_type: Optional[str]) -> str:
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    for magic, ext in IMAGE_MAGIC:
        if content.startswith(magic):
            return ".webp" if ext == ".webp" else ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return ".png"


# ---------------------------------------------------------------------------
# API 客户端（tenant_access_token 应用接入）
# ---------------------------------------------------------------------------

class FeishuError(Exception):
    def __init__(self, msg: str, code: Optional[int] = None, http: Optional[int] = None):
        super().__init__(msg)
        self.code = code
        self.http = http

    @property
    def permanent(self) -> bool:
        text = str(self).lower()
        return any(t in text for t in
                   ("invalid", "expired", "revoked", "not exist", "not found",
                    "permission", "forbidden"))


class FeishuClient:
    """基于 requests 的飞书开放 API 客户端，应用身份（tenant token）。

    HTTP 层可注入（fetcher 回调）以便离线单测。
    """

    def __init__(self, app_id: str, app_secret: str,
                 base: str = DEFAULT_BASE, timeout: float = 30.0,
                 fetcher=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expire = 0.0
        # fetcher(method, url, headers, body_bytes) -> (status, body_bytes, resp_headers)
        self._fetch = fetcher or self._default_fetch

    # -- HTTP ------------------------------------------------------------
    @staticmethod
    def _default_fetch(method: str, url: str, headers: dict, body: Optional[bytes]):
        resp = requests.request(method, url, headers=headers, data=body, timeout=30)
        return resp.status_code, resp.content, dict(resp.headers)

    def _request_json(self, method: str, path: str,
                      params: Optional[dict] = None,
                      body: Optional[dict] = None) -> dict:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self._tenant_token()}"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        status, content, _ = self._fetch(method, url, headers, payload)
        try:
            data = json.loads(content.decode("utf-8")) if content else {}
        except (ValueError, UnicodeDecodeError):
            raise FeishuError(f"非 JSON 响应 HTTP {status}: {content[:200]!r}",
                              http=status)
        if status < 200 or status >= 400 or data.get("code", 0) not in (0, None):
            raise FeishuError(
                f"飞书 API 失败 {method} {path}: http={status} "
                f"code={data.get('code')} msg={data.get('msg')}",
                code=data.get("code"), http=status)
        return data.get("data", {})

    def _request_raw(self, path: str, params: Optional[dict] = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self._tenant_token()}"}
        status, content, resp_headers = self._fetch("GET", url, headers, None)
        if status < 200 or status >= 400:
            # 下载接口错误也是 JSON
            try:
                data = json.loads(content.decode("utf-8"))
                raise FeishuError(
                    f"下载失败 {path}: http={status} code={data.get('code')} "
                    f"msg={data.get('msg')}", code=data.get("code"), http=status)
            except (ValueError, UnicodeDecodeError):
                raise FeishuError(f"下载失败 {path}: HTTP {status}", http=status)
        return content, resp_headers

    # -- token -----------------------------------------------------------
    def _tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expire - 120:
            return self._token  # type: ignore[return-value]
        body = {"app_id": self.app_id, "app_secret": self.app_secret}
        status, content, _ = self._fetch(
            "POST", self.base + f"{API}/auth/v3/tenant_access_token/internal",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(body).encode("utf-8"))
        try:
            data = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise FeishuError(f"获取 tenant token 失败: HTTP {status}")
        if data.get("code", 0) != 0 or not data.get("tenant_access_token"):
            raise FeishuError(
                f"获取 tenant token 失败: code={data.get('code')} msg={data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expire = now + int(data.get("expire", 1800))
        LOG.debug("tenant token 已刷新，有效期 %ss", data.get("expire"))
        return self._token

    # -- wiki ------------------------------------------------------------
    def list_spaces(self) -> list:
        spaces, page_token = [], None
        while True:
            params = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json("GET", f"{API}/wiki/v2/spaces", params)
            spaces.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return spaces

    def get_wiki_node(self, token: str) -> dict:
        return self._request_json("GET", f"{API}/wiki/v2/spaces/get_node",
                                  {"token": token, "obj_type": "wiki"}).get("node", {})

    def list_node_children(self, space_id: str, parent_token: str = "",
                           max_items: Optional[int] = None) -> list:
        """分页列出 wiki 节点的子节点；parent_token 为空时列空间根节点。"""
        children: list = []
        page_token: Optional[str] = None
        pages: set = set()
        while True:
            params: dict = {"page_size": 50}
            if parent_token:
                params["parent_node_token"] = parent_token
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET", f"{API}/wiki/v2/spaces/{space_id}/nodes", params)
            children.extend(data.get("items") or [])
            if max_items is not None and len(children) > max_items:
                return children[:max_items + 1]
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if page_token in pages:
                raise FeishuError("飞书返回重复分页 token")
            pages.add(page_token)
            if not page_token:
                raise FeishuError("飞书 has_more 但未给 page_token")
        return children

    # -- docx ------------------------------------------------------------
    def get_doc_meta(self, doc_id: str) -> dict:
        return self._request_json("GET", f"{API}/docx/v1/documents/{doc_id}")

    def get_all_blocks(self, doc_id: str) -> list:
        blocks, page_token = [], None
        while True:
            params = {"page_size": 500, "document_revision_id": -1}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET", f"{API}/docx/v1/documents/{doc_id}/blocks", params)
            blocks.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
        return blocks

    # -- sheets ------------------------------------------------------------
    def get_sheet_meta(self, token: str) -> dict:
        return self._request_json(
            "GET", f"{API}/sheets/v2/spreadsheets/{token}/metainfo")

    def read_sheet_values(self, token: str, sheet_id: str,
                          max_rows: int, max_cols: int = 26) -> list:
        end_col = chr(ord("A") + min(max_cols, 26) - 1)
        rng = f"{sheet_id}!A1:{end_col}{max_rows}"
        data = self._request_json(
            "GET", f"{API}/sheets/v2/spreadsheets/{token}/values/{urllib.parse.quote(rng, safe='!?:')}")
        return (data.get("valueRange") or {}).get("values") or []

    # -- bitable ------------------------------------------------------------
    def list_bitable_tables(self, app_token: str) -> list:
        tables, page_token = [], None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET", f"{API}/bitable/v1/apps/{app_token}/tables", params)
            tables.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return tables

    def list_bitable_fields(self, app_token: str, table_id: str) -> list:
        fields, page_token = [], None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET",
                f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                params)
            fields.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return fields

    def list_bitable_records(self, app_token: str, table_id: str,
                              max_records: int = 500) -> list:
        records, page_token = [], None
        while len(records) < max_records:
            params = {"page_size": min(500, max_records - len(records))}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET",
                f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                params)
            items = data.get("items") or []
            records.extend(items)
            if len(items) > max_records - len(records):
                records = records[:max_records]
                break
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return records

    # -- media ------------------------------------------------------------
    def download_media(self, file_token: str, extra: Optional[str] = None):
        params = {"extra": extra} if extra else None
        return self._request_raw(f"{API}/drive/v1/medias/{file_token}/download",
                                 params)


# ---------------------------------------------------------------------------
# 增量版本比对（v2: 树遍历阶段即判定，未变文档零内容拉取）
# ---------------------------------------------------------------------------

def node_version(node: dict, meta: Optional[dict] = None) -> str:
    """节点的云端版本号（统一表达式，写入 state 与增量闸门共用同源值）。

    优先 obj_edit_time(节点元数据自带, 列子节点时免费获得)；
    缺失时回退 doc 元数据的 edit_time / revision_id；再缺回退 "0"。
    """
    v = node.get("obj_edit_time")
    if v is None and isinstance(meta, dict):
        v = meta.get("edit_time", meta.get("revision_id"))
    return str(v) if v is not None else "0"


def needs_sync(state_entry: Optional[dict], version: str,
               local_path: Path) -> bool:
    """判断是否需要拉取并重写该节点。

    规则（云端始终为准）:
      1. state 无记录（新文档）→ 需要
      2. 本地文件不存在（被删/损坏）→ 需要
      3. 云端版本号(obj_edit_time)变化 → 需要（强制覆盖）
      4. 其余 → 跳过（零 API 内容调用）
    """
    if not state_entry:
        return True
    if not local_path.exists():
        return True
    return state_entry.get("edit_time") != version


def node_content_key(node: dict) -> str:
    """state 的主键: obj_token（同一文档在树中移动/改名仍指向同一条记录）。"""
    return str(node.get("obj_token") or node.get("node_token", ""))


# ---------------------------------------------------------------------------
# docx block -> markdown
# ---------------------------------------------------------------------------

def block_attr(block: dict) -> Optional[str]:
    bt = block.get("block_type")
    attr = BLOCK_TYPE_TO_ATTR.get(bt)
    if attr and block.get(attr) is not None:
        return attr
    # 回退：白名单扫描
    for attr in BLOCK_TYPE_TO_ATTR.values():
        if block.get(attr) is not None:
            return attr
    return None


def render_inline(elements: list) -> str:
    parts = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        run = el.get("text_run")
        if run:
            text = run.get("content", "")
            style = run.get("text_element_style") or {}
            if style.get("inline_code"):
                text = f"`{text}`"
            link = style.get("link")
            if link and link.get("url"):
                text = f"[{text}]({link['url']})"
            if style.get("bold"):
                text = f"**{text}**"
            if style.get("italic"):
                text = f"*{text}*"
            if style.get("strikethrough"):
                text = f"~~{text}~~"
            if style.get("underline"):
                text = f"<u>{text}</u>"
            parts.append(text)
            continue
        mu = el.get("mention_user")
        if mu:
            parts.append(f"@{mu.get('user_id', 'user')}")
            continue
        md = el.get("mention_doc")
        if md:
            title = md.get("title", "document")
            url = md.get("url", "")
            parts.append(f"[{title}]({url})" if url else str(title))
            continue
        eq = el.get("equation")
        if eq:
            parts.append(f"${eq.get('content', '')}$")
            continue
    return "".join(parts)


def _table_to_md(rows: list) -> Optional[str]:
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join(["---"] * width) + "|"]
    for row in rows[1:]:
        cells = [c.replace("|", "\\|").replace("\n", "<br>") for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _cell_text(cell_block: dict, block_map: dict) -> str:
    texts = []
    for cid in cell_block.get("children", []):
        child = block_map.get(cid)
        if not child:
            continue
        attr = block_attr(child)
        if attr:
            obj = child.get(attr) or {}
            text = render_inline(obj.get("elements", [])) if isinstance(obj, dict) else ""
            if text:
                texts.append(text)
    return " ".join(texts)


def _read_sheet(client: FeishuClient, spreadsheet_token: str, sheet_id: str,
                max_rows: int) -> list:
    try:
        values = client.read_sheet_values(spreadsheet_token, sheet_id, max_rows)
    except FeishuError as exc:
        LOG.warning("读取内嵌 sheet %s 失败: %s", sheet_id, exc)
        return []
    return [[str(c) if c is not None else "" for c in row] for row in values]


class BlockRenderer:
    """把扁平 block 列表按树渲染成 Markdown。"""

    def __init__(self, client: FeishuClient, doc_id: str,
                 max_sheet_rows: int = 200):
        self.client = client
        self.doc_id = doc_id
        self.max_sheet_rows = max_sheet_rows

    def render(self, blocks: list) -> str:
        block_map = {b["block_id"]: b for b in blocks}
        page_blocks = [b for b in blocks if b.get("block_type") == 1]
        root = page_blocks[0] if page_blocks else None
        root_id = root["block_id"] if root else blocks[0]["block_id"]
        lines = []
        self._render_children(root_id, block_map, lines, depth=0)
        return "\n\n".join(ln for ln in lines if ln)

    def _render_children(self, parent_id: str, block_map: dict,
                         lines: list, depth: int) -> None:
        ordered_counter: dict[str, int] = {}
        for bid in block_map.get(parent_id, {}).get("children", []):
            block = block_map.get(bid)
            if not block:
                continue
            line = self._render_block(block, block_map, ordered_counter,
                                      depth=depth)
            if line:
                lines.append(line)

    def _render_block(self, block: dict, block_map: dict,
                      ordered_counter: dict, depth: int) -> Optional[str]:
        attr = block_attr(block)
        if attr in ("page", "table_cell", "quote_container", "view",
                    "grid", "grid_column"):
            return None
        obj = block.get(attr) or {}
        children_ids = block.get("children", [])
        indent = "  " * depth if attr in ("bullet", "ordered", "todo") else ""
        elements = obj.get("elements", []) if isinstance(obj, dict) else []
        text = render_inline(elements)

        if attr and attr.startswith("heading"):
            level = int(attr.replace("heading", "") or 1)
            return f"{'#' * min(9, level)} {text}".rstrip()

        if attr == "bullet":
            sub = []
            self._render_children(block["block_id"], block_map, sub, depth + 1)
            return indent + f"- {text}\n{chr(10).join(sub)}" if sub else indent + f"- {text}"

        if attr == "ordered":
            parent = block.get("parent_id", "")
            counter = ordered_counter.get(parent, 0) + 1
            ordered_counter[parent] = counter
            sub = []
            self._render_children(block["block_id"], block_map, sub, depth + 1)
            return (indent + f"{counter}. {text}\n{chr(10).join(sub)}"
                    if sub else indent + f"{counter}. {text}")

        if attr == "todo":
            done = bool((obj.get("style") or {}).get("done"))
            box = "[x]" if done else "[ ]"
            sub = []
            self._render_children(block["block_id"], block_map, sub, depth + 1)
            return (indent + f"- {box} {text}\n{chr(10).join(sub)}"
                    if sub else indent + f"- {box} {text}")

        if attr == "code":
            lang = (obj.get("style") or {}).get("language", "") or ""
            return f"```{lang}\n{text}\n```"

        if attr == "quote":
            return f"> {text}" if text else None

        if attr == "callout":
            lines = [f"> [!note] {text}"]
            sub = []
            self._render_children(block["block_id"], block_map, sub, 0)
            lines.extend(f"> {ln}" for ln in ("\n".join(sub)).split("\n") if ln.strip())
            return "\n".join(lines)

        if attr == "divider":
            return "---"

        if attr == "image":
            token = obj.get("token", "")
            alt = obj.get("alt", "") or "image"
            return f"![{alt}](feishu://image/{token})" if token else None

        if attr in ("image_view", "file", "iframe"):
            # file/image_view 走 drive 通用的 drive/file 链接；iframe 外链
            if attr == "iframe":
                url = ""
                if isinstance(obj, dict):
                    for key in ("url", "link", "component"):
                        v = obj.get(key)
                        if isinstance(v, str) and v:
                            url = v
                            break
                return f"*iframe: {url}*" if url else None
            token = obj.get("token", "") if isinstance(obj, dict) else ""
            name = (obj.get("name", "") if isinstance(obj, dict) else "") or token
            return f"[附件: {name}](feishu://file/{token})" if token else None

        if attr == "sheet":
            # 内嵌 sheet: 通过父 block 接口无法直接取 token，降级为提示
            return "*内嵌电子表格（需在飞书中查看）*"

        if attr == "bitable":
            return "*内嵌多维表格（需在飞书中查看）*"

        if attr == "table":
            prop = (obj or {}).get("property") or {}
            row_size = prop.get("row_size", 0)
            col_size = prop.get("column_size", 0)
            cells = (obj or {}).get("cells", []) or children_ids
            rows = []
            for r in range(row_size):
                row = []
                for c in range(col_size):
                    idx = r * col_size + c
                    if idx < len(cells):
                        cell_block = block_map.get(cells[idx], {})
                        row.append(_cell_text(cell_block, block_map))
                    else:
                        row.append("")
                rows.append(row)
            return _table_to_md(rows)

        return text or None


# ---------------------------------------------------------------------------
# sheet / bitable -> markdown
# ---------------------------------------------------------------------------

def render_sheet(client: FeishuClient, token: str, max_rows: int = 500) -> tuple:
    meta = client.get_sheet_meta(token)
    props = meta.get("properties", {})
    title = props.get("title") or "Spreadsheet"
    parts = [f"# {title}"]
    for sheet in meta.get("sheets", []):
        sid = sheet.get("sheetId", "")
        stitle = sheet.get("title", sid)
        parts.append(f"## Sheet: {stitle}")
        row_count = int(sheet.get("rowCount") or 0)
        col_count = int(sheet.get("columnCount") or 0)
        if not row_count or not col_count:
            parts.append("*空表*")
            continue
        rows = _read_sheet(client, token, sid, min(row_count, max_rows))
        if rows:
            md = _table_to_md(rows)
            if md:
                parts.append(md)
        if row_count > max_rows:
            parts.append(f"*... 截断，共 {row_count} 行，已导出 {max_rows} 行 ...*")
        if col_count > 26:
            parts.append(f"*... {col_count - 26} 列（Z 列之后）未导出 ...*")
    return "\n\n".join(parts), title


def _format_bitable_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_format_bitable_value(v) for v in value)
    if isinstance(value, dict):
        if "file_token" in value:
            name = str(value.get("name") or "附件")
            return f"![{name}](feishu://image/{value['file_token']})"
        text = value.get("text")
        if text is not None:
            return str(text)
        link = value.get("link") or {}
        if isinstance(link, dict) and link.get("text"):
            return str(link["text"])
        # 选项类 { "options": [...] }、人员 { "id":.. } 等取可用字段
        for key in ("name", "en_name", "id"):
            if value.get(key):
                return str(value[key])
        return str(value)
    return str(value)


def render_bitable(client: FeishuClient, app_token: str, max_records: int = 500,
                   table_id: Optional[str] = None) -> tuple:
    if table_id:
        tables = [{"table_id": table_id, "name": table_id}]
        title = f"Bitable {table_id}"
        heading = "###"
        parts = [f"# {title}"]
    else:
        tables = client.list_bitable_tables(app_token)
        title = f"Bitable ({len(tables)} tables)"
        heading = "##"
        parts = [f"# {title}"]
    for t in tables:
        tid = t["table_id"]
        tname = t.get("name") or tid
        try:
            fields = client.list_bitable_fields(app_token, tid)
        except FeishuError as exc:
            LOG.warning("读取 bitable 字段 %s 失败: %s", tid, exc)
            continue
        field_names = [f["field_name"] for f in fields]
        try:
            records = client.list_bitable_records(app_token, tid, max_records)
        except FeishuError as exc:
            LOG.warning("读取 bitable 记录 %s 失败: %s", tid, exc)
            continue
        parts.append(f"{heading} {tname}")
        parts.append(f"**记录数:** {len(records)}")
        if field_names and records:
            rows = [field_names]
            for rec in records:
                rec_fields = rec.get("fields", {})
                rows.append([_format_bitable_value(rec_fields.get(n, ""))
                             for n in field_names])
            md = _table_to_md(rows)
            if md:
                parts.append(md)
    return "\n\n".join(parts), title


def render_legacy_doc(client: FeishuClient, doc_token: str) -> tuple:
    """老版 doc (/docs/) 无公开 raw_content API 的替代：提示手动迁移。"""
    return (f"*此文档为飞书老版 doc（docs），开放 API 不再支持内容读取，"
            f"请在飞书中迁移为新版 docx 后重试。*", f"doc_{doc_token}")


# ---------------------------------------------------------------------------
# Wiki 树遍历 + 同步
# ---------------------------------------------------------------------------

@dataclass
class SyncStats:
    total: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    images: int = 0
    errors: list = field(default_factory=list)


class WikiSyncer:
    def __init__(self, client: FeishuClient, out_dir: Path,
                 space_id: str, max_depth: int = 20, max_nodes: int = 5000,
                 max_sheet_rows: int = 500, max_bitable_records: int = 500,
                 state_file: Optional[Path] = None,
                 download_images: bool = True,
                 skip_tables: bool = False):
        self.client = client
        self.out = out_dir
        self.space_id = space_id
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_sheet_rows = max_sheet_rows
        self.max_bitable_records = max_bitable_records
        self.download_images = download_images
        self.skip_tables = skip_tables
        self.state_file = state_file or (out_dir / ".feishu_sync_state.json")
        self.state: dict = {"nodes": {}}
        self.stats = SyncStats()
        self._load_state()

    # -- state（增量: node_token -> obj_edit_time + content sha） ------------
    def _load_state(self):
        if self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text("utf-8"))
            except (ValueError, OSError):
                LOG.warning("状态文件损坏，将全量同步")

    def _save_state(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                       "utf-8")
        tmp.replace(self.state_file)

    # -- tree -------------------------------------------------------------
    def walk_from(self, root_token: str = ""):
        """从 wiki 节点 token 开始递归遍历；为空时从空间根节点列表开始。"""
        if root_token:
            roots = [self.client.get_wiki_node(root_token)]
        else:
            # wiki v2 nodes 接口不传 parent_node_token 即返回空间根节点列表
            roots = self.client.list_node_children(self.space_id, "")
        stack = [(r, Path(".")) for r in roots]
        visited = set()
        count = 0
        while stack:
            node, rel_dir = stack.pop()
            token = node.get("node_token", "")
            if not token or token in visited:
                continue
            visited.add(token)
            count += 1
            if count > self.max_nodes:
                LOG.warning("达到节点数上限 %d，停止遍历", self.max_nodes)
                return
            title = sanitize_name(node.get("title") or token)
            obj_type = node.get("obj_type", "")
            has_children = bool(node.get("has_child"))
            children = []
            if has_children:
                try:
                    children = self.client.list_node_children(
                        self.space_id, token)
                except FeishuError as exc:
                    LOG.warning("列子节点失败 %s: %s", token, exc)
            node_dir = rel_dir
            if children or (has_children and len(children) == 0):
                # 有子节点 → 建同名目录
                node_dir = rel_dir / title
            yield node, node_dir, len(children)
            # 子节点先入栈（逆序保持稳定顺序）
            for child in reversed(children):
                stack.append((child, node_dir))

    # -- materialize -------------------------------------------------------
    def sync_from(self, root_token: str = "", prune: bool = False):
        self.out.mkdir(parents=True, exist_ok=True)
        seen_paths: set = set()
        for node, node_dir, child_count in self.walk_from(root_token):
            self.stats.total += 1
            obj_type = WIKI_TYPE_MAP.get(str(node.get("obj_type") or ""),
                                     str(node.get("obj_type") or ""))
            obj_token = node.get("obj_token", "")
            title = node.get("title") or obj_token or "untitled"
            # 目标路径先算出来，供增量闸门与 prune 共用
            path = self.out / node_dir / (sanitize_name(title) + FILE_SUFFIX)
            rel_path = str(path.relative_to(self.out))
            try:
                # ---- 表格跳过（skip_tables 时: 不进 seen_paths → prune 清理旧文件）----
                if obj_type in ("sheets", "base") and self.skip_tables:
                    LOG.debug("跳过表格节点 %s (%s)", title, obj_type)
                    self.stats.skipped += 1
                    continue
                seen_paths.add(rel_path)
                # ---- 增量闸门（云端版本为准，提前到内容拉取之前）----
                entry = self.state["nodes"].get(node_content_key(node))
                if obj_type == "docx" and not needs_sync(
                        entry, node_version(node), path):
                    self.stats.skipped += 1
                    continue
                if obj_type in ("docx", "doc", "sheets", "base") or \
                        (child_count == 0 and obj_type not in ("file",)):
                    content, meta = self._render_node(obj_type, obj_token, title)
                else:
                    # 纯目录节点 / file 附件
                    target_dir = self.out / node_dir
                    target_dir.mkdir(parents=True, exist_ok=True)
                    self.stats.skipped += 1
                    continue
                md, images = self._resolve_images(content, node_dir)
                # 内嵌属性块: 从正文中提取并合并到 frontmatter
                inline_props, cleaned_body = extract_inline_props(md)
                source_url = (f"https://feishu.cn/wiki/"
                               f"{node.get('node_token', '')}")
                revision = (meta or {}).get("revision_id",
                                            node.get("obj_edit_time", ""))
                front = build_frontmatter(
                    title=title, source_url=source_url,
                    revision=revision,
                    imported_at=datetime.now().strftime("%Y-%m-%d"),
                    inline_props=inline_props,
                    extra={"synced_at":
                           datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
                full_md = f"{front}\n\n{cleaned_body}\n" if cleaned_body else front + "\n"
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._write_if_changed(path, full_md, node, meta):
                    self.stats.written += 1
                else:
                    self.stats.skipped += 1
                self.stats.images += images
            except FeishuError as exc:
                if exc.permanent:
                    LOG.warning("节点 %s (%s) 永久失败: %s", title, obj_type, exc)
                    self.stats.failed += 1
                    self.stats.errors.append(f"{title}: {exc}")
                else:
                    LOG.error("节点 %s 失败: %s", title, exc)
                    self.stats.failed += 1
                    self.stats.errors.append(f"{title}: {exc}")
            except Exception as exc:  # noqa: BLE001
                LOG.exception("节点 %s 异常", title)
                self.stats.failed += 1
                self.stats.errors.append(f"{title}: {exc}")
        self._prune(seen_paths if prune else None)
        self._save_state()
        return self.stats

    def _prune(self, seen_paths: Optional[set]) -> int:
        """删除本地多余文件（源端已删除/改名），并清理失效状态与孤儿资源。

        seen_paths 为 None 时跳过清理（保守模式）。
        顺序:
          1) 删除不在 seen_paths 的 .md
          2) state 中 path 失效的条目移除
          3) 孤儿资源清理: 解析全部存活 .md 的 assets/ 引用，
             未被任何存活文档引用的图片文件删除
          4) 空目录回收（自底向上）
        返回删除的文件数。
        """
        if seen_paths is None:
            return 0
        removed = 0
        # 排除图谱工具产物（graphify-out: 语义缓存/图谱/报告，与飞书源无关，
        # 由 graphify 自身的增量机制管理，不受 prune 波及）
        excluded_prefixes = ("graphify-out",)
        # 1) 本地存在但本轮未同步到的 .md → 源端已删除/改名
        for fpath in self.out.rglob("*.md"):
            rel = str(fpath.relative_to(self.out))
            first = rel.split("/", 1)[0] if "/" in rel else rel
            if first in excluded_prefixes:
                continue
            if rel not in seen_paths:
                LOG.info("清理(源端已删除): %s", rel)
                fpath.unlink()
                removed += 1
        # 2) state 重建（按 path 保留）
        new_nodes = {}
        for token, entry in self.state.get("nodes", {}).items():
            if entry.get("path") in seen_paths:
                new_nodes[token] = entry
        self.state["nodes"] = new_nodes
        # 3) 孤儿资源: 存活 .md 中引用的 assets 相对路径并集
        referenced: set = set()
        for fpath in self.out.rglob("*.md"):
            rel_dir = fpath.parent
            try:
                text = fpath.read_text("utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"\]\((assets/[^)\s]+)\)", text):
                referenced.add((rel_dir / m.group(1)).resolve())
        orphan_assets = 0
        for apath in self.out.rglob("assets/*"):
            if apath.is_file() and apath.resolve() not in referenced:
                LOG.info("清理(孤儿资源): %s", apath.relative_to(self.out))
                apath.unlink()
                orphan_assets += 1
        if orphan_assets:
            LOG.info("共清理孤儿资源 %d 个", orphan_assets)
        # 4) 空目录回收（自底向上；不删排除前缀内的目录）
        for d in sorted(self.out.rglob("*"), reverse=True):
            if d.is_dir() and d != self.out:
                rel = str(d.relative_to(self.out))
                first = rel.split("/", 1)[0] if "/" in rel else rel
                if first in excluded_prefixes:
                    continue
                try:
                    next(d.iterdir())
                except StopIteration:
                    d.rmdir()
        return removed

    def _render_node(self, obj_type: str, obj_token: str, title: str):
        """按文档类型渲染正文，返回 (body_markdown, meta)。

        frontmatter 由 sync_from 统一组装，这里只产正文。
        """
        if obj_type == "docx":
            meta = self.client.get_doc_meta(obj_token)
            blocks = self.client.get_all_blocks(obj_token)
            renderer = BlockRenderer(self.client, obj_token,
                                     self.max_sheet_rows)
            body = renderer.render(blocks)
            return body, meta
        if obj_type == "sheets":
            body, stitle = render_sheet(self.client, obj_token,
                                        self.max_sheet_rows)
            return body, {}
        if obj_type == "base":
            body, btitle = render_bitable(self.client, obj_token,
                                          self.max_bitable_records)
            return body, {}
        if obj_type == "doc":
            body, _ = render_legacy_doc(self.client, obj_token)
            return body, {}
        # 未知类型（wiki 子 wiki / 文件等）
        return f"*不支持的文档类型: {obj_type}*", {}

    def _resolve_images(self, markdown: str, node_dir: Path) -> tuple:
        """下载 feishu://image/ 引用并改写为相对路径，返回 (md, 下载计数)。"""
        if not self.download_images:
            return markdown, 0
        matches = list(FEISHU_IMAGE_RE.finditer(markdown))
        if not matches:
            return markdown, 0
        images_dir = self.out / node_dir / "assets"
        token_map = {}
        downloaded = 0
        for match in matches:
            token = match.group(2)
            if token in token_map:
                continue
            try:
                content, headers = self.client.download_media(token)
            except FeishuError as exc:
                LOG.warning("图片 %s 下载失败: %s", token, exc)
                continue
            ext = guess_image_ext(content,
                                  headers.get("Content-Type",
                                              headers.get("content-type")))
            fname = f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', token)}{ext}"
            images_dir.mkdir(parents=True, exist_ok=True)
            fpath = images_dir / fname
            if not fpath.exists():
                fpath.write_bytes(content)
                downloaded += 1
            token_map[token] = f"assets/{fname}"
        def _replace(m):
            rel = token_map.get(m.group(2))
            return f"![{m.group(1)}]({rel})" if rel else m.group(0)
        return FEISHU_IMAGE_RE.sub(_replace, markdown), downloaded

    def _write_if_changed(self, path: Path, content: str, node: dict,
                          meta: dict) -> bool:
        """写入节点内容并记录版本（云端为准: 已过第一道闸门的必写）。

        第二道确认: 内容 sha 与 state 一致时跳过磁盘写（版本号变化但
        内容实质相同, 如仅光标/元数据变动）, 但仍更新版本号。
        """
        token = node_content_key(node)
        entry = self.state["nodes"].get(token, {})
        edit_time = node_version(node, meta)
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        content_unchanged = (str(entry.get("edit_time", "")) == str(edit_time)
                             and entry.get("sha") == sha and path.exists())
        if not content_unchanged:
            path.write_text(content, encoding="utf-8")
        self.state["nodes"][token] = {
            "title": node.get("title", ""),
            "path": str(path.relative_to(self.out)),
            "edit_time": str(edit_time),
            "sha": sha,
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return not content_unchanged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="飞书 Wiki 空间同步到本地 Markdown")
    parser.add_argument("--app-id", help="飞书应用 AppID（默认取 .env / 环境变量）")
    parser.add_argument("--app-secret", help="飞书应用 AppSecret（默认取 .env / 环境变量）")
    parser.add_argument("--env", help="指定 .env 文件路径（默认: 脚本同目录 .env）")
    parser.add_argument("--space", help="Wiki space_id（默认取 .env 的 FEISHU_SPACE）")
    parser.add_argument("--root", help="起始 wiki 节点 token（默认取 .env 的 FEISHU_ROOT，"
                        "为空则从空间根节点全量遍历）")
    parser.add_argument("--out", help="输出目录（默认取 .env 的 SYNC_OUT，再默认 ./feishu-wiki-out）")
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument("--no-images", action="store_true",
                        help="不下载图片（.env 中 SYNC_DOWNLOAD_IMAGES=false 同效）")
    parser.add_argument("--prune", action="store_true",
                        help="删除源端已不存在的本地文件（定时清理）")
    parser.add_argument("--skip-tables", action="store_true",
                        help="跳过 bitable/sheet 表格类文档（图谱价值低，默认在 .env 中配置）")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("command", choices=["list-spaces", "sync"],
                        help="list-spaces: 列出空间; sync: 同步")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # 配置优先级: 命令行参数 > 已有环境变量 > .env
    load_env_file(args.env)

    app_id = args.app_id or os.environ.get("FEISHU_APP_ID", "")
    app_secret = args.app_secret or os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise SystemExit("缺少凭证：--app-id/--app-secret 或 .env 中 "
                         "FEISHU_APP_ID / FEISHU_APP_SECRET")
    client = FeishuClient(app_id, app_secret)

    if args.command == "list-spaces":
        for sp in client.list_spaces():
            print(f"{sp.get('space_id')}\t{sp.get('name')}")
        return 0

    # sync — space/root/out 均可由 .env 提供
    space = args.space or os.environ.get("FEISHU_SPACE", "")
    root_token = (args.root if args.root is not None
                  else os.environ.get("FEISHU_ROOT", ""))
    out_raw = (args.out or os.environ.get("SYNC_OUT", "")
               or "./feishu-wiki-out")
    out = Path(os.path.expanduser(out_raw))
    if not space:
        raise SystemExit("缺少 --space（可用 list-spaces 查询后填入 .env 的 FEISHU_SPACE）")

    if not root_token:
        LOG.info("未指定 --root，从空间 %s 的根节点开始全量遍历", space)

    download_images = (os.environ.get("SYNC_DOWNLOAD_IMAGES", "true")
                       .strip().lower() not in ("false", "0", "no"))
    skip_tables = (args.skip_tables or
                   os.environ.get("SYNC_SKIP_TABLES", "true")
                   .strip().lower() in ("true", "1", "yes"))
    syncer = WikiSyncer(client, out, space,
                        max_depth=args.max_depth, max_nodes=args.max_nodes,
                        download_images=download_images and not args.no_images,
                        skip_tables=skip_tables)
    stats = syncer.sync_from(root_token, prune=args.prune)
    print(f"完成: 遍历 {stats.total} 节点, 写入 {stats.written}, "
          f"跳过 {stats.skipped}, 失败 {stats.failed}, 图片 {stats.images}")
    for err in stats.errors[:10]:
        print(f"  失败详情: {err}", file=sys.stderr)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())