#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟测试：用 mock fetcher 验证 WikiSyncer 全链路（不发真实请求）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from feishu_wiki_sync import (FeishuClient, WikiSyncer, BlockRenderer,
                              render_bitable, render_sheet, _table_to_md)

# ---------------- mock 飞书 API ----------------
WIKI_SPACE = "7000000000000000001"

NODES = {
    # 根节点: 一个带两个子节点的目录型 wiki 节点
    "wikin_root_001": {
        "node_token": "wikin_root_001", "space_id": WIKI_SPACE,
        "title": "根目录", "obj_type": "docx", "obj_token": "doxcn111",
        "has_child": True,
    },
    "wikin_child_doc1": {
        "node_token": "wikin_child_doc1", "space_id": WIKI_SPACE,
        "title": "子文档A", "obj_type": "docx", "obj_token": "doxcn222",
        "has_child": False, "obj_edit_time": 1700000000000,
    },
    "wikin_child_doc2": {
        "node_token": "wikin_child_doc2", "space_id": WIKI_SPACE,
        "title": "数据表B", "obj_type": "sheets", "obj_token": "shtcn333",
        "has_child": False, "obj_edit_time": 1700000000000,
    },
    "wikin_child_doc3": {
        "node_token": "wikin_child_doc3", "space_id": WIKI_SPACE,
        "title": "组织指南", "obj_type": "docx", "obj_token": "doxcn444",
        "has_child": False, "obj_edit_time": 1700000000000,
    },
}
CHILDREN = {"wikin_root_001": [NODES["wikin_child_doc1"], NODES["wikin_child_doc2"], NODES["wikin_child_doc3"]]}

DOCX_BLOCKS = {
    "doxcn111": [
        {"block_id": "p0", "block_type": 1, "children": ["p1"]},
        {"block_id": "p1", "block_type": 2, "parent_id": "p0", "children": [],
         "text": {"elements": [{"text_run": {"content": "这是根目录页面正文"}}]}},
    ],
    # 带内嵌属性块的文档（用户知识库的真实模式）
    "doxcn444": [
        {"block_id": "q0", "block_type": 1,
         "children": ["q1", "q2", "q3"]},
        {"block_id": "q1", "block_type": 14, "parent_id": "q0",
         "children": [],
         "code": {"elements": [{"text_run": {"content": "---\ntype: 指南\ndepartment: 工程与交付\ntags: [组织架构, 部门总览, 岗位职责, 新人指南, 制度规范, 团队会议, 团队运营, SOP, 工具与系统入口, 术语表]\nowner: 陈蓓\nreviewer: 郑兴\nstatus: 草稿\nreview: 2026-12-31\nsummary: \"本模块介绍 Aes 工程与交付团队的整体情况，帮助团队成员（尤其是新成员）快速了解组织架构、工作方式与协作规则。\"\n---"}}],
                 "style": {"language": "text"}}},
        {"block_id": "q2", "block_type": 3, "parent_id": "q0", "children": [],
         "heading1": {"elements": [{"text_run": {"content": "部门总览"}}]}},
        {"block_id": "q3", "block_type": 2, "parent_id": "q0", "children": [],
         "text": {"elements": [{"text_run": {"content": "正文内容"}}]}},
    ],
    "doxcn222": [
        {"block_id": "b0", "block_type": 1, "children": ["b1", "b2", "b3", "b4", "b5", "b6", "b7"]},
        {"block_id": "b1", "block_type": 3, "parent_id": "b0",
         "children": [], "heading1": {"elements": [{"text_run": {"content": "文档A标题", "text_element_style": {"bold": True}}}]}},
        {"block_id": "b2", "block_type": 2, "parent_id": "b0",
         "children": [], "text": {"elements": [{"text_run": {"content": "普通文本带 ", "text_element_style": {}}},
                              {"text_run": {"content": "行内代码", "text_element_style": {"inline_code": True}}},
                              {"text_run": {"content": " 和链接", "text_element_style": {"link": {"url": "https://example.com"}}}}]}},
        {"block_id": "b3", "block_type": 14, "parent_id": "b0",
         "children": [], "code": {"elements": [{"text_run": {"content": "print('hi')"}}], "style": {"language": "python"}}},
        {"block_id": "b4", "block_type": 22, "parent_id": "b0", "children": [], "divider": {}},
        {"block_id": "b5", "block_type": 31, "parent_id": "b0",
         "children": ["c0", "c1", "c2", "c3"],
         "table": {"property": {"row_size": 2, "column_size": 2}, "cells": ["c0", "c1", "c2", "c3"]}},
        {"block_id": "c0", "block_type": 32, "parent_id": "b5", "children": ["c0a"], "table_cell": {}},
        {"block_id": "c0a", "block_type": 2, "parent_id": "c0", "children": [],
         "text": {"elements": [{"text_run": {"content": "表头1"}}]}},
        {"block_id": "c1", "block_type": 32, "parent_id": "b5", "children": ["c1a"], "table_cell": {}},
        {"block_id": "c1a", "block_type": 2, "parent_id": "c1", "children": [],
         "text": {"elements": [{"text_run": {"content": "表头2"}}]}},
        {"block_id": "c2", "block_type": 32, "parent_id": "b5", "children": ["c2a"], "table_cell": {}},
        {"block_id": "c2a", "block_type": 2, "parent_id": "c2", "children": [],
         "text": {"elements": [{"text_run": {"content": "值|1"}}]}},
        {"block_id": "c3", "block_type": 32, "parent_id": "b5", "children": ["c3a"], "table_cell": {}},
        {"block_id": "c3a", "block_type": 2, "parent_id": "c3", "children": [],
         "text": {"elements": [{"text_run": {"content": "值2"}}]}},
        {"block_id": "b6", "block_type": 12, "parent_id": "b0", "children": ["b6a"],
         "bullet": {"elements": [{"text_run": {"content": "列表项"}}]}},
        {"block_id": "b6a", "block_type": 12, "parent_id": "b6", "children": [],
         "bullet": {"elements": [{"text_run": {"content": "嵌套项"}}]}},
        {"block_id": "b7", "block_type": 27, "parent_id": "b0", "children": [],
         "image": {"token": "img_tok_1", "alt": "截图"}},
    ],
}

def fetch(method, url, headers, body):
    """mock: 拦截所有飞书 API。"""
    if method == "POST" and url.endswith("/auth/v3/tenant_access_token/internal"):
        return 200, json.dumps({"code": 0, "tenant_access_token": "t-xxx", "expire": 7200}).encode(), {}
    auth = headers.get("Authorization", "")
    assert auth == "Bearer t-xxx", f"token 未注入: {auth}"
    if url.endswith(f"/wiki/v2/spaces/get_node?token=wikin_root_001&obj_type=wiki"):
        return 200, json.dumps({"code": 0, "data": {"node": NODES["wikin_root_001"]}}).encode(), {}
    if "/wiki/v2/spaces/" in url and "/nodes" in url:
        # 解析 parent_node_token
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        parent = q["parent_node_token"][0]
        items = CHILDREN.get(parent, [])
        return 200, json.dumps({"code": 0, "data": {"items": items, "has_more": False}}).encode(), {}
    for doc_id in ("doxcn111", "doxcn222", "doxcn444"):
        if f"/docx/v1/documents/{doc_id}" == url.split("?")[0].split("/open-apis")[1]:
            return 200, json.dumps({"code": 0, "data": {"document": {"document_id": doc_id,
                           "revision_id": 5, "title": {"doxcn222": "子文档A", "doxcn444": "组织指南"}.get(doc_id, "根目录")}}}).encode(), {}
        if f"/docx/v1/documents/{doc_id}/blocks" in url:
            return 200, json.dumps({"code": 0, "data": {"items": DOCX_BLOCKS[doc_id], "has_more": False}}).encode(), {}
    if "/sheets/v2/spreadsheets/shtcn333/metainfo" in url:
        return 200, json.dumps({"code": 0, "data": {"properties": {"title": "数据表B"},
            "sheets": [{"sheetId": "sid1", "title": "Sheet1", "rowCount": 2, "columnCount": 2}]}}).encode(), {}
    if "/sheets/v2/spreadsheets/shtcn333/values/" in url:
        return 200, json.dumps({"code": 0, "data": {"valueRange": {"values": [["列1", "列2"], ["a", "b"]]}}}).encode(), {}
    if "/drive/v1/medias/img_tok_1/download" in url:
        # 1x1 PNG
        png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                            "1f15c4890000000d49444154789c626001000000ffff030000060005"
                            "57bfabd40000000049454e44ae426082")
        return 200, png, {"Content-Type": "image/png"}
    raise AssertionError(f"mock 未覆盖: {method} {url}")


def main():
    client = FeishuClient("cli_test", "secret_test", fetcher=fetch)
    out = Path(tempfile.mkdtemp(prefix="feishu_sync_test_"))
    syncer = WikiSyncer(client, out, WIKI_SPACE, download_images=True)
    stats = syncer.sync_from("wikin_root_001")

    print("=== 统计 ===")
    print(f"total={stats.total} written={stats.written} skipped={stats.skipped} "
          f"failed={stats.failed} images={stats.images}")
    print("=== 目录树 ===")
    for p in sorted(out.rglob("*")):
        print(" ", p.relative_to(out))
    doc_a = out / "根目录" / "子文档A.md"
    print("=== 子文档A.md ===")
    print(doc_a.read_text("utf-8"))
    doc_b = out / "根目录" / "数据表B.md"
    print("=== 数据表B.md ===")
    print(doc_b.read_text("utf-8"))
    img = out / "根目录" / "assets"
    print("=== 图片 ===")
    print(list(img.glob("*.png")) if img.exists() else "无图片目录!")

    # 第二次同步: 应全部 skipped（增量）
    syncer2 = WikiSyncer(client, out, WIKI_SPACE, download_images=True)
    stats2 = syncer2.sync_from("wikin_root_001")
    print("=== 二次同步(增量) ===")
    print(f"written={stats2.written} skipped={stats2.skipped} failed={stats2.failed}")
    assert stats2.written == 0, "增量失效：无变化仍重写"
    assert stats.failed == 0, "存在失败节点"
    assert doc_a.exists() and doc_b.exists()
    assert img.exists() and list(img.glob("*.png")), "图片未落盘"
    md_a = doc_a.read_text("utf-8")
    assert "```python" in md_a and "print('hi')" in md_a, "代码块丢失"
    assert "| 表头1 | 表头2 |" in md_a, "表头丢失"
    assert "\\|" in md_a, "竖杠未转义"
    assert "- 列表项" in md_a and "  - 嵌套项" in md_a, "嵌套列表丢失"
    assert "[行内代码](https://example.com)" not in md_a, "inline_code 被链接误覆盖(链接顺序bug)"
    assert "`行内代码`" in md_a, "行内代码丢失"
    assert "assets/img_tok_1.png" in md_a, "图片未重写为相对路径"
    # 内嵌属性块合并验证
    doc_c = out / "根目录" / "组织指南.md"
    md_c = doc_c.read_text("utf-8")
    print("=== 组织指南.md (属性合并) ===")
    print(md_c[:900])
    fm = md_c.split("---")[1]
    assert "type: 指南" in fm, "type 未合并"
    assert "department: 工程与交付" in fm, "department 未合并"
    assert "owner: 陈蓓" in fm, "owner 未合并"
    assert "reviewer: 郑兴" in fm, "reviewer 未合并"
    assert "status: 草稿" in fm, "status 未合并"
    assert "SOP" in fm, "tags 数组未合并"
    assert "组织架构" in fm, "tags 数组元素丢失"
    assert fm.index("title:") < fm.index("type:"), "title 未在业务字段之前"
    assert "imported_at:" in fm, "imported_at 丢失"
    # 正文中的属性代码块应被移除
    body_c = md_c.split("---", 2)[2]
    assert "department: 工程与交付" not in body_c, "属性代码块未从正文移除"
    assert "# 部门总览" in body_c, "正文标题丢失"
    assert "正文内容" in body_c, "正文内容丢失"
    print("=== 全部断言通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())