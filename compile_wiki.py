#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM Wiki 编译器：把飞书同步的 raw 文档编译为知识图谱（机械建图，零 LLM）。

输入: ~/wiki/raw/ 下的同步文档（统一 frontmatter，见 feishu-wiki-sync）
输出: ~/wiki/ 知识图谱层:
  entities/   实体页（人员/部门/产品/系统），含出现文档 backlinks
  concepts/   概念页（tags 展开 + 目录主题），含成员文档
  index.md    分区目录（按 type/部门/产品）
  log.md      编译日志

建图来源（全部机械，确定性）:
  1. frontmatter 属性: owner/reviewer → 人员实体; department → 部门实体;
     tags/type → 概念页; title 中的产品名 → 产品实体
  2. 跨文档链接: 正文 feishu wiki 链接 → node_token 映射到本地文档 → [[wikilink]]
  3. 目录树: 文档的目录归属 → 目录概念页成员

增量: raw 文档的 revision+sha 未变 → 输出页不重渲染（编译器自己的 state）。

用法:
  python3 compile_wiki.py                 # 编译到 ~/wiki
  python3 compile_wiki.py --raw PATH --out PATH --rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOG_PREFIX = "[compile]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# frontmatter 解析（与同步器同构的扁平 YAML，复用独立实现避免 import 同义词）
# ---------------------------------------------------------------------------

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    props: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # 去行内注释（值里的 " #" 分隔）
        if " #" in value and not value.startswith("["):
            value = value.split(" #")[0].strip()
        if value.startswith("[") and value.endswith("]"):
            # tags 用中文逗号也常见——统一拆
            inner = value[1:-1]
            parts = re.split(r"[，,]", inner)
            props[key] = [p.strip().strip("'\"") for p in parts if p.strip()]
        else:
            props[key] = value.strip("'\"")
    return props, text[m.end():]


PERSON_LIST_RE = re.compile(r"[\[（(]?\[?([^\]）)]+)\]?[）)]?")


def parse_persons(raw) -> list:
    """owner/reviewer 值可能是 '陈蓓'、'[华中豪]'、'[华中豪, 朱晓辉]'。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [p.strip() for p in raw if p and p.strip()]
    s = str(raw).strip().strip("[]()（）")
    # 去行内注释
    if " #" in s:
        s = s.split(" #")[0].strip()
    return [p.strip() for p in re.split(r"[，,、]", s) if p.strip()]


# 占位人名（模板文档里的示例值，不建实体页）
PLACEHOLDER_PERSONS = {"张三", "李四", "王五", "待定", "各级负责人", "XXX"}

# ---------------------------------------------------------------------------
# 文档模型
# ---------------------------------------------------------------------------

@dataclass
class Doc:
    path: Path                 # 相对 wiki 根的路径
    slug: str                  # wikilink 用（无扩展名相对路径）
    title: str
    source_token: str          # feishu node_token
    revision: str
    sha: str                   # 正文+frontmatter 哈希
    props: dict
    dir_path: tuple            # 目录元组（不含文件名）
    out_links: list = field(default_factory=list)   # [Doc]（已解析的本地链接）
    raw_links: list = field(default_factory=list)   # 原始 node_token（未命中本地的）
    tags: list = field(default_factory=list)


def load_docs(raw_root: Path) -> list:
    docs = []
    for f in sorted(raw_root.rglob("*.md")):
        text = f.read_text("utf-8", errors="replace")
        props, body = parse_frontmatter(text)
        rel = f.relative_to(raw_root.parent)
        # 剥离 raw 段: dir_path 不含 "raw"，slug 不含 "raw/"
        rel_no_raw = rel.parts[1:] if rel.parts and rel.parts[0] == "raw" else rel.parts
        rel_path = Path(*rel_no_raw) if rel_no_raw else rel
        m = re.search(r"wiki/([A-Za-z0-9]+)", str(props.get("source", "")))
        source_token = m.group(1) if m else ""
        # 正文内 feishu wiki 链接
        raw_links = re.findall(
            r"feishu\.cn/wiki/([A-Za-z0-9]{20,})", body)
        docs.append(Doc(
            path=f,
            slug=str(rel_path.with_suffix("")),
            title=str(props.get("title") or f.stem),
            source_token=source_token,
            revision=str(props.get("revision", "")),
            sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            props=props,
            dir_path=rel_path.parts[:-1] if len(rel_path.parts) > 1 else (),
            raw_links=raw_links,
        ))
    return docs


# ---------------------------------------------------------------------------
# 实体抽取
# ---------------------------------------------------------------------------

# 知识库中的产品/系统名（从语料 tags 与标题中挖掘，机械匹配标题/正文）
KNOWN_PRODUCTS = [
    "51PM", "51CMP", "BuilderTools", "UGA", "AntAgent", "Ant Agent",
    "XiaoBuild", "ECP", "DTA.AI", "Perforce", "P4", "51Aes",
]
PRODUCT_ALIASES = {"Ant Agent": "AntAgent", "P4": "Perforce"}


def normalize_product(name: str) -> str:
    return PRODUCT_ALIASES.get(name, name)


def doc_products(doc: Doc) -> list:
    found = []
    haystack = doc.title
    for p in KNOWN_PRODUCTS:
        if p.lower() in haystack.lower():
            found.append(normalize_product(p))
    # tags 里的产品也计入
    for t in doc.tags:
        for p in KNOWN_PRODUCTS:
            if p.lower() == t.lower():
                found.append(normalize_product(p))
    return sorted(set(found))


def doc_tags(doc: Doc) -> list:
    return [t for t in doc.props.get("tags", []) if isinstance(t, str)]


# ---------------------------------------------------------------------------
# 编译器主体
# ---------------------------------------------------------------------------

class WikiCompiler:
    def __init__(self, wiki_root: Path, raw_rel: str = "raw", rebuild: bool = False):
        self.root = wiki_root
        self.raw_root = wiki_root / raw_rel
        self.rebuild = rebuild
        self.state_file = wiki_root / ".compile_state.json"
        self.state: dict = {"docs": {}}
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text("utf-8"))
            except (ValueError, OSError):
                log("编译 state 损坏，全量重建")

    def _save_state(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                       "utf-8")
        tmp.replace(self.state_file)

    # -- 主流程 ------------------------------------------------------------
    def compile(self) -> dict:
        docs = load_docs(self.raw_root)
        log(f"载入 raw 文档 {len(docs)} 个")

        # 1) 链接解析: source_token -> Doc
        token_map = {d.source_token: d for d in docs if d.source_token}
        unresolved = 0
        for d in docs:
            d.out_links = []
            seen_targets = set()
            for tok in d.raw_links:
                target = token_map.get(tok)
                if target and target.slug != d.slug and target.slug not in seen_targets:
                    d.out_links.append(target)
                    seen_targets.add(target.slug)
                elif not target:
                    unresolved += 1
            d.tags = doc_tags(d)
        log(f"跨文档边 {sum(len(d.out_links) for d in docs)} 条, "
            f"未命中云链接 {unresolved} 个（表格外/外空间）")

        # 2) 实体收集
        entities = self._collect_entities(docs)
        concepts = self._collect_concepts(docs)
        log(f"实体 {len(entities)} 个, 概念页 {len(concepts)} 个")

        # 3) 渲染输出
        changed = self._render(docs, entities, concepts)
        self._save_state()
        log(f"输出: 更新 {changed['updated']} / 跳过 {changed['skipped']}")
        return changed

    # -- 实体收集 ------------------------------------------------------------
    def _collect_entities(self, docs: list) -> dict:
        """{实体名: {kind, 出现文档列表(带角色)}}"""
        entities: dict = defaultdict(lambda: {"kind": "", "docs": []})

        def add(name, kind, doc, role):
            e = entities[name]
            if not e["kind"]:
                e["kind"] = kind
            e["docs"].append((doc, role))

        for d in docs:
            for p in parse_persons(d.props.get("owner")):
                if p not in PLACEHOLDER_PERSONS:
                    add(p, "人员", d, "owner")
            for p in parse_persons(d.props.get("reviewer")):
                if p not in PLACEHOLDER_PERSONS:
                    add(p, "人员", d, "reviewer")
            dept = d.props.get("department")
            if isinstance(dept, str) and dept:
                dept_clean = dept.split(" #")[0].strip()
                if dept_clean:
                    add(dept_clean, "部门", d, "department")
            # 目录维度: dir_path 首段是知识库根目录名（空间整体，不建实体），
            # 第二段才是真正的组织顶层
            depth_top = 1 if len(d.dir_path) >= 2 else None
            if depth_top is not None:
                top = d.dir_path[depth_top]
                if top not in entities or not entities[top]["kind"]:
                    add(top, "部门", d, "目录")
                elif d not in [x[0] for x in entities[top]["docs"]]:
                    entities[top]["docs"].append((d, "目录"))
            for prod in doc_products(d):
                add(prod, "产品", d, "产品相关")
        return dict(entities)

    # -- 概念收集 ------------------------------------------------------------
    def _collect_concepts(self, docs: list) -> dict:
        """{概念名: {来源, 成员文档}}  来源: tags / 目录主题"""
        concepts: dict = defaultdict(lambda: {"sources": set(), "docs": []})
        for d in docs:
            for t in d.tags:
                if not t or not t.strip():
                    continue
                c = concepts[t.strip()]
                c["sources"].add("tags")
                c["docs"].append(d)
            # 二级目录作为主题概念（dir_path[2]，因为 [0]=知识库根 [1]=组织顶层）
            if len(d.dir_path) >= 3:
                theme = d.dir_path[2]
                c = concepts[theme]
                c["sources"].add("目录")
                c["docs"].append(d)
            elif len(d.dir_path) == 2:
                # 组织顶层目录本身也作为概念（如 部门总览/项目管理）
                theme = d.dir_path[1]
                c = concepts[theme]
                c["sources"].add("目录")
                c["docs"].append(d)
            typ = d.props.get("type")
            if isinstance(typ, str) and typ:
                typ_clean = typ.split(" #")[0].strip()
                if typ_clean:
                    c = concepts[typ_clean]
                    c["sources"].add("type")
                    c["docs"].append(d)
        # 去重 docs
        for c in concepts.values():
            seen = set()
            uniq = []
            for d in c["docs"]:
                if d.slug not in seen:
                    seen.add(d.slug)
                    uniq.append(d)
            c["docs"] = uniq
        return {k: v for k, v in concepts.items() if len(v["docs"]) >= 1}

    # -- 渲染 ------------------------------------------------------------
    def _render(self, docs: list, entities: dict, concepts: dict) -> dict:
        updated = skipped = 0
        pages = {}

        for name, e in sorted(entities.items()):
            pages[f"entities/{self._slug(name)}.md"] = self._entity_md(name, e, docs)
        for name, c in sorted(concepts.items()):
            pages[f"concepts/{self._slug(name)}.md"] = self._concept_md(name, c, docs)
        pages["index.md"] = self._index_md(docs, entities, concepts)

        for rel, content in pages.items():
            path = self.root / rel
            key = rel
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if not self.rebuild and self.state["docs"].get(key) == sha and path.exists():
                skipped += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.state["docs"][key] = sha
            updated += 1

        # prune: raw 已无成员的实体/概念页删除（含无后缀的旧格式残留）
        pruned_pages = 0
        expected = set(pages) | {"log.md"}
        for sub in ("entities", "concepts"):
            d = self.root / sub
            if not d.exists():
                continue
            for old in d.iterdir():
                rel = f"{sub}/{old.name}"
                if rel not in expected:
                    old.unlink()
                    pruned_pages += 1
        if pruned_pages:
            log(f"清理失效图谱页 {pruned_pages} 个")
            self.state["docs"] = {k: v for k, v in self.state["docs"].items()
                                  if k in expected}

        # 追加编译日志
        log_path = self.root / "log.md"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{time.strftime('%Y-%m-%d %H:%M')}] compile | "
                    f"{len(docs)} docs, {len(entities)} entities, "
                    f"{len(concepts)} concepts, 更新 {updated} 跳过 {skipped}, "
                    f"清理 {pruned_pages}\n")
        return {"updated": updated, "skipped": skipped, "pruned": pruned_pages,
                "entities": len(entities), "concepts": len(concepts)}

    @staticmethod
    def _slug(name: str) -> str:
        safe = re.sub(r'[\x00-\x1f/*?:"<>|]+', "_", str(name)).strip(" ._")
        return safe or "untitled"

    def _wikilink(self, doc: Doc) -> str:
        return f"[[{doc.slug}|{doc.title}]]"

    def _entity_md(self, name: str, ent: dict, all_docs: list) -> str:
        lines = [f"# {name}", "",
                 f"> 类型: {ent['kind']}", ""]
        roles = defaultdict(list)
        for d, role in ent["docs"]:
            roles[role].append(self._wikilink(d))
        for role in ("owner", "reviewer", "department", "目录", "产品相关"):
            if roles.get(role):
                lines.append(f"## 作为 {role}" if role in ("owner", "reviewer")
                             else f"## 相关文档（{role}）")
                for link in sorted(set(roles[role])):
                    lines.append(f"- {link}")
                lines.append("")
        # 出度链接要求: 每页至少链到 2 个其他图谱页
        links_added = 0
        related = []
        for d, _ in ent["docs"]:
            for t in d.out_links:
                related.append(f"[[{t.slug}|{t.title}]]")
            links_added += len(d.out_links)
        # frontmatter
        fm = ["---",
              f"title: {name}",
              f"created: {time.strftime('%Y-%m-%d')}",
              f"updated: {time.strftime('%Y-%m-%d')}",
              f"type: entity",
              f"entity_kind: {ent['kind']}",
              f"doc_count: {len(set(d.slug for d, _ in ent['docs']))}",
              "---", ""]
        body = "\n".join(fm) + "\n".join(lines)
        if related:
            uniq = sorted(set(related))[:10]
            body += "\n## 关联（经文档链接）\n" + "\n".join(f"- {l}" for l in uniq) + "\n"
        return body

    def _concept_md(self, name: str, c: dict, all_docs: list) -> str:
        src = "、".join(sorted(c["sources"]))
        lines = [f"# {name}", "",
                 f"> 来源: {src} | 成员文档 {len(c['docs'])} 个", "",
                 "## 成员文档", ""]
        for d in sorted(c["docs"], key=lambda x: x.slug):
            summary = str(d.props.get("summary", "")).strip()
            if summary and len(summary) > 80:
                summary = summary[:80] + "…"
            lines.append(f"- {self._wikilink(d)}"
                         + (f" — {summary}" if summary else ""))
        fm = ["---",
              f"title: {name}",
              f"created: {time.strftime('%Y-%m-%d')}",
              f"updated: {time.strftime('%Y-%m-%d')}",
              f"type: concept",
              f"sources: [{src}]",
              f"doc_count: {len(c['docs'])}",
              "---", ""]
        return "\n".join(fm) + "\n" + "\n".join(lines) + "\n"

    def _index_md(self, docs: list, entities: dict, concepts: dict) -> str:
        persons = {k: v for k, v in entities.items() if v["kind"] == "人员"}
        depts = {k: v for k, v in entities.items() if v["kind"] == "部门"}
        prods = {k: v for k, v in entities.items() if v["kind"] == "产品"}
        L = [f"# Wiki Index", "",
             f"> raw 文档 {len(docs)} 个 | 实体 {len(entities)} | 概念 {len(concepts)}",
             f"> 由 compile_wiki.py 编译自飞书同步层。最后更新 {time.strftime('%Y-%m-%d')}",
             ""]
        L.append("## 实体 — 人员")
        L += [f"- [[entities/{self._slug(k)}|{k}]] "
              f"({len(set(d.slug for d, _ in v['docs']))} 篇)"
              for k, v in sorted(persons.items())] or ["- （无）"]
        L.append("")
        L.append("## 实体 — 部门")
        L += [f"- [[entities/{self._slug(k)}|{k}]] "
              f"({len(set(d.slug for d, _ in v['docs']))} 篇)"
              for k, v in sorted(depts.items())] or ["- （无）"]
        L.append("")
        L.append("## 实体 — 产品")
        L += [f"- [[entities/{self._slug(k)}|{k}]] "
              f"({len(set(d.slug for d, _ in v['docs']))} 篇)"
              for k, v in sorted(prods.items())] or ["- （无）"]
        L.append("")
        L.append("## 概念")
        L += [f"- [[concepts/{self._slug(k)}|{k}]] ({len(v['docs'])} 篇)"
              for k, v in sorted(concepts.items())] or ["- （无）"]
        return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="LLM Wiki 编译器（机械建图）")
    parser.add_argument("--raw", help="raw 目录（默认 <wiki>/raw）")
    parser.add_argument("--out", help="wiki 根目录（默认 ~/wiki）")
    parser.add_argument("--rebuild", action="store_true", help="忽略编译 state 全量重渲染")
    parser.add_argument("--prune-entities", action="store_true",
                        help="删除 raw 中已无成员的实体/概念页")
    args = parser.parse_args(argv)

    wiki_root = Path(args.out or "~/wiki").expanduser()
    raw_rel = "raw"
    if args.raw:
        raw_path = Path(args.raw).expanduser()
        try:
            raw_rel = str(raw_path.relative_to(wiki_root))
        except ValueError:
            raw_rel = str(raw_path)
    compiler = WikiCompiler(wiki_root, raw_rel, rebuild=args.rebuild)
    result = compiler.compile()
    print(f"编译完成: 实体 {result['entities']}, 概念 {result['concepts']}, "
          f"更新 {result['updated']}, 跳过 {result['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())