#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 Wiki 空间 -> 本地 Markdown 目录树 同步工具。

应用接入（AppID/AppSecret, tenant_access_token），定时全量遍历指定 Wiki 空间，
将 docx/sheet/bitable 文档转换为 Markdown 落盘。幂等：重复执行只重写有变化
（obj_edit_time 变化或内容哈希变化）的文件。

用法:
  # 列出自己可访问的 wiki 空间（找 space_id）
  python3 feishu_wiki_sync.py list-spaces --app-id X --app-secret Y

  # 全量同步一个空间
  python3 feishu_wiki_sync.py sync --app-id X --app-secret Y \
      --space 1234567890 --out /path/to/output

  # 按配置文件 + 环境变量凭证同步（适合 cron）
  FEISHU_APP_ID=X FEISHU_APP_SECRET=Y \
  python3 feishu_wiki_sync.py sync --config sync.yaml

凭证优先级: 命令行参数 > 环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET > 配置文件。
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
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:
    print("需要 requests: pip install requests", file=sys.stderr)
    raise

LOG = logging.getLogger("feishu_sync")

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
            spaces.extend(data.get("items", []))
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
            children.extend(data.get("items", []))
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
            blocks.extend(data.get("items", []))
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
        return data.get("valueRange", {}).get("values", [])

    # -- bitable ------------------------------------------------------------
    def list_bitable_tables(self, app_token: str) -> list:
        tables, page_token = [], None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET", f"{API}/bitable/v1/apps/{app_token}/tables", params)
            tables.extend(data.get("items", []))
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
            fields.extend(data.get("items", []))
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
            items = data.get("items", [])
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
                 download_images: bool = True):
        self.client = client
        self.out = out_dir
        self.space_id = space_id
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_sheet_rows = max_sheet_rows
        self.max_bitable_records = max_bitable_records
        self.download_images = download_images
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
    def walk_space(self):
        """从空间根开始遍历，yield (node, rel_dir)。

        返回值 node 含: node_token, obj_type, obj_token, title, obj_edit_time
        rel_dir 是该节点应落的相对目录。
        """
        # 空间根: 拿不到根节点 token，用 spaces API 无法列根；遍历策略:
        # 官方 API 不提供“空间根节点列表”，只能从已挂载节点开始。
        # 常见做法: 空间创建时的首个节点无法枚举 -> 我们提供 --root 参数
        raise NotImplementedError

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
    def sync_from(self, root_token: str):
        self.out.mkdir(parents=True, exist_ok=True)
        for node, node_dir, child_count in self.walk_from(root_token):
            self.stats.total += 1
            obj_type = WIKI_TYPE_MAP.get(str(node.get("obj_type") or ""),
                                     str(node.get("obj_type") or ""))
            obj_token = node.get("obj_token", "")
            title = node.get("title") or obj_token or "untitled"
            try:
                if obj_type in ("docx", "doc", "sheets", "base") or \
                        (child_count == 0 and obj_type not in ("file",)):
                    content, meta = self._render_node(obj_type, obj_token, title)
                else:
                    # 纯目录节点
                    target_dir = self.out / node_dir
                    target_dir.mkdir(parents=True, exist_ok=True)
                    self.stats.skipped += 1
                    continue
                md, images = self._resolve_images(content, node_dir)
                path = self.out / node_dir / (sanitize_name(title) + FILE_SUFFIX)
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._write_if_changed(path, md, node, meta):
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
        self._save_state()
        return self.stats

    def _render_node(self, obj_type: str, obj_token: str, title: str):
        """按文档类型渲染，返回 (markdown, meta)。"""
        front = f"---\nfeishu_title: {json.dumps(title, ensure_ascii=False)}\n"
        if obj_type == "docx":
            meta = self.client.get_doc_meta(obj_token)
            blocks = self.client.get_all_blocks(obj_token)
            renderer = BlockRenderer(self.client, obj_token,
                                     self.max_sheet_rows)
            body = renderer.render(blocks)
            return (f"{front}feishu_doc: docx/{obj_token}\n---\n\n{body}"), meta
        if obj_type == "sheets":
            body, stitle = render_sheet(self.client, obj_token,
                                        self.max_sheet_rows)
            return (f"{front}feishu_doc: sheets/{obj_token}\n---\n\n{body}"), {}
        if obj_type == "base":
            body, btitle = render_bitable(self.client, obj_token,
                                          self.max_bitable_records)
            return (f"{front}feishu_doc: base/{obj_token}\n---\n\n{body}"), {}
        if obj_type == "doc":
            body, _ = render_legacy_doc(self.client, obj_token)
            return (f"{front}feishu_doc: doc/{obj_token}\n---\n\n{body}"), {}
        # 未知类型（wiki 子 wiki / 文件等）
        return (f"{front}feishu_doc: {obj_type}/{obj_token}\n---\n\n"
                f"*不支持的文档类型: {obj_type}*"), {}

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
        """增量写入: edit_time 未变 + 内容一致则跳过。返回 True=已写。"""
        token = node.get("obj_token") or node.get("node_token", "")
        entry = self.state["nodes"].get(token, {})
        edit_time = node.get("obj_edit_time", 0) or meta.get("edit_time", 0)
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (entry.get("edit_time") == edit_time and entry.get("sha") == sha
                and path.exists()):
            return False
        path.write_text(content, encoding="utf-8")
        self.state["nodes"][token] = {
            "title": node.get("title", ""),
            "path": str(path.relative_to(self.out)),
            "edit_time": edit_time,
            "sha": sha,
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_credentials(args) -> tuple:
    app_id = args.app_id or os.environ.get("FEISHU_APP_ID", "")
    app_secret = args.app_secret or os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        if getattr(args, "config", None):
            try:
                cfg = json.loads(Path(args.config).read_text("utf-8"))
                app_id = app_id or cfg.get("app_id", "")
                app_secret = app_secret or cfg.get("app_secret", "")
            except (OSError, ValueError):
                pass
    if not app_id or not app_secret:
        raise SystemExit("缺少凭证：设置 --app-id/--app-secret 或环境变量 "
                         "FEISHU_APP_ID / FEISHU_APP_SECRET")
    return app_id, app_secret


def main(argv=None):
    parser = argparse.ArgumentParser(description="飞书 Wiki 空间同步到本地 Markdown")
    parser.add_argument("--app-id", help="飞书应用 AppID")
    parser.add_argument("--app-secret", help="飞书应用 AppSecret")
    parser.add_argument("--config", help="JSON 配置文件（含 app_id/app_secret/space/out）")
    parser.add_argument("--space", help="Wiki space_id")
    parser.add_argument("--root", help="起始 wiki 节点 token（默认用 space 下第一个根节点）")
    parser.add_argument("--out", help="输出目录")
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("command", choices=["list-spaces", "sync"],
                        help="list-spaces: 列出空间; sync: 同步")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    app_id, app_secret = load_credentials(args)
    client = FeishuClient(app_id, app_secret)

    if args.command == "list-spaces":
        for sp in client.list_spaces():
            print(f"{sp.get('space_id')}\t{sp.get('name')}")
        return 0

    # sync
    cfg = {}
    if args.config:
        try:
            cfg = json.loads(Path(args.config).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"配置文件读取失败: {exc}")
    space = args.space or cfg.get("space", "")
    out = Path(args.out or cfg.get("out", "./feishu-wiki-out"))
    if not space:
        raise SystemExit("缺少 --space（可用 list-spaces 查询）")

    root_token = args.root or cfg.get("root", "")
    if not root_token:
        LOG.info("未指定 --root，从空间 %s 的根节点开始全量遍历", space)

    syncer = WikiSyncer(client, out, space,
                        max_depth=args.max_depth, max_nodes=args.max_nodes,
                        download_images=not args.no_images)
    stats = syncer.sync_from(root_token)
    print(f"完成: 遍历 {stats.total} 节点, 写入 {stats.written}, "
          f"跳过 {stats.skipped}, 失败 {stats.failed}, 图片 {stats.images}")
    for err in stats.errors[:10]:
        print(f"  失败详情: {err}", file=sys.stderr)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())