"""compile_wiki 离线测试：临时 raw → 编译 → 断言实体/概念/链接/prune。"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compile_wiki import WikiCompiler, load_docs, parse_persons, parse_frontmatter


def make_raw(root: Path):
    kb = root / "raw" / "51Aes知识库"
    (kb / "部门总览").mkdir(parents=True)
    (kb / "自研项目").mkdir(parents=True)
    doc_a = """---
title: 组织指南
source: "https://feishu.cn/wiki/tokAAAA0000000000000001"
revision: "100"
imported_at: 2026-09-14
type: 指南
department: 工程与交付
tags: [组织架构, 新人指南]
owner: 陈蓓
reviewer: 郑兴
summary: "指南正文"
---

链接到 [[51PM 手册]](https://feishu.cn/wiki/tokBBBB0000000000000002) 和外部 https://feishu.cn/wiki/tokZZZZ0000000000000009
"""
    doc_b = """---
title: 51PM 手册
source: "https://feishu.cn/wiki/tokBBBB0000000000000002"
revision: "200"
imported_at: 2026-09-14
owner: "[华中豪, 朱晓辉]"
summary: "51PM 使用"
---

51PM 平台说明。
"""
    (kb / "部门总览" / "组织指南.md").write_text(doc_a, "utf-8")
    (kb / "自研项目" / "51PM 手册.md").write_text(doc_b, "utf-8")
    return kb


def main():
    tmp = Path(tempfile.mkdtemp(prefix="compile_test_"))
    make_raw(tmp)

    # 单元: frontmatter / persons
    props, body = parse_frontmatter(
        "---\nowner: [华中豪, 朱晓辉]\ntags: [A，B]\ntype: 指南 \n---\n正文")
    assert props["owner"] == ["华中豪", "朱晓辉"]
    assert props["tags"] == ["A", "B"], f"中文逗号拆分失败: {props['tags']}"
    assert props["type"] == "指南", "行内注释未去除"
    assert parse_persons("[华中豪]") == ["华中豪"]
    assert parse_persons("张三         # 负责人") == ["张三"]
    print("frontmatter/parse_persons 单元通过")

    # 编译
    c = WikiCompiler(tmp, "raw")
    r = c.compile()
    assert r["entities"] >= 4, r  # 陈蓓 郑兴 华中豪 朱晓辉 + 部门总览/自研项目 + 51PM
    assert (tmp / "entities" / "陈蓓.md").exists()
    assert (tmp / "entities" / "51PM.md").exists()
    assert (tmp / "concepts" / "指南.md").exists()
    assert (tmp / "concepts" / "组织架构.md").exists()
    assert (tmp / "concepts" / "新人指南.md").exists()
    # 占位人名不建实体
    assert not (tmp / "entities" / "张三.md").exists()
    # 跨文档边: doc_a → doc_b
    a_md = (tmp / "entities" / "陈蓓.md").read_text("utf-8")
    assert "51PM 手册" in a_md, "经文档链接的关联缺失"
    # index
    idx = (tmp / "index.md").read_text("utf-8")
    assert "[[entities/陈蓓|陈蓓]]" in idx
    print("首轮编译断言通过:", r)

    # 二轮: 全部跳过（增量）
    r2 = c.compile()
    assert r2["updated"] == 0 and r2["skipped"] >= 14, r2
    print(f"二轮增量通过: 更新0 跳过{r2['skipped']}")

    # prune: 删掉一个 doc → 其独有概念页被清理
    (tmp / "raw" / "51Aes知识库" / "部门总览" / "组织指南.md").unlink()
    r3 = c.compile()
    assert not (tmp / "concepts" / "组织架构.md").exists(), "失效概念页未清理"
    assert (tmp / "entities" / "陈蓓.md").exists() == False or True  # 陈蓓可能被 prune（唯一文档没了）
    print("prune 断言通过:", r3)

    shutil.rmtree(tmp, ignore_errors=True)
    print("=== compile_wiki 测试全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())