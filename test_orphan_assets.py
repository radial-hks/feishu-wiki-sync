"""prune 孤儿资源清理验证。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_offline as T


def main():
    client = T.FeishuClient("cli_test", "secret_test", fetcher=T.fetch)
    out = Path(tempfile.mkdtemp(prefix="orphan_test_"))

    # 正常同步一轮（子文档A 引用 img_tok_1.png）
    syncer = T.WikiSyncer(client, out, T.WIKI_SPACE, skip_tables=True)
    syncer.sync_from("wikin_root_001", prune=True)
    assets_dir = out / "根目录" / "assets"
    assert assets_dir.exists(), "应有图片"
    # 手动塞一个孤儿图片 + 一个孤儿 md
    orphan_png = assets_dir / "orphan_deadbeef.png"
    orphan_png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    orphan_md = out / "根目录" / "已删除文档.md"
    orphan_md.write_text("引用了 orphan: ![x](assets/orphan_deadbeef.png)", "utf-8")

    # 场景1: 云端删除"子文档A"（从树里摘掉）→ 其图片应被清
    T.CHILDREN["wikin_root_001"] = [
        n for n in T.CHILDREN["wikin_root_001"]
        if n["node_token"] != "wikin_child_doc1"
    ]
    syncer2 = T.WikiSyncer(client, out, T.WIKI_SPACE, skip_tables=True)
    stats2 = syncer2.sync_from("wikin_root_001", prune=True)

    assert not (out / "根目录" / "子文档A.md").exists(), "被删文档的 md 应被 prune"
    assert not (assets_dir / "img_tok_1.png").exists(), "孤儿图片 img_tok_1 应被清理"
    assert not orphan_md.exists(), "孤儿 md 应被 prune"
    assert not orphan_png.exists(), "孤儿图片 orphan 应被清理"
    assert not assets_dir.exists() or not list(assets_dir.iterdir()), "assets 应为空/回收"
    print("场景1 通过: 删除文档 → 其图片/孤儿 md/孤儿图片 全部清理")

    # 场景2: 存活文档引用的图片必须保留
    # 恢复 child_doc1 再同步 → img_tok_1 回来; 放一个其他孤儿 → 只清孤儿
    T.CHILDREN["wikin_root_001"].append(T.NODES["wikin_child_doc1"])
    syncer3 = T.WikiSyncer(client, out, T.WIKI_SPACE, skip_tables=True)
    syncer3.sync_from("wikin_root_001", prune=True)
    assert (assets_dir / "img_tok_1.png").exists(), "存活文档引用的图片应保留"
    keep_png = assets_dir / "keep_referenced.png"
    keep_png.write_bytes(b"\x89PNG\r\n\x1a\nkeep")
    alive = out / "根目录" / "组织指南.md"
    text = alive.read_text("utf-8")
    alive.write_text(text + "\n\n![keep](assets/keep_referenced.png)\n", "utf-8")
    syncer4 = T.WikiSyncer(client, out, T.WIKI_SPACE, skip_tables=True)
    syncer4.sync_from("wikin_root_001", prune=True)
    assert keep_png.exists(), "被存活文档引用的图片不应被清理"
    print("场景2 通过: 存活文档引用的图片保留")
    print("=== 孤儿资源测试全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())