"""增量版本闸门验证：mock 场景下验证零内容拉取与强制覆盖。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_offline as T
from feishu_wiki_sync import needs_sync, node_version, node_content_key

WIKI_SPACE = T.WIKI_SPACE

# 统计 docx blocks API 调用次数（衡量是否真的零拉取）
api_calls = {"blocks": 0}
orig_fetch = T.fetch

def counting_fetch(method, url, headers, body):
    if "/blocks" in url:
        api_calls["blocks"] += 1
    return orig_fetch(method, url, headers, body)


def main():
    client = T.FeishuClient("cli_test", "secret_test", fetcher=counting_fetch)
    out = Path(tempfile.mkdtemp(prefix="incr_test_"))

    # 第一次同步: 4 节点(docx111 根 + docx222 + sheets333 + docx444)
    s1 = T.WikiSyncer(client, out, WIKI_SPACE, skip_tables=True)
    stats1 = s1.sync_from("wikin_root_001")
    print(f"首轮: written={stats1.written} skipped={stats1.skipped} "
          f"failed={stats1.failed} blocks_calls={api_calls['blocks']}")
    assert stats1.failed == 0

    # 第二次同步: 修改 NODES 中一个文档的 obj_edit_time 模拟云端编辑
    T.NODES["wikin_child_doc1"]["obj_edit_time"] = 1999999999999
    api_calls["blocks"] = 0
    s2 = T.WikiSyncer(client, out, WIKI_SPACE, skip_tables=True)
    stats2 = s2.sync_from("wikin_root_001")
    print(f"二轮(云端改1篇): written={stats2.written} skipped={stats2.skipped} "
          f"blocks_calls={api_calls['blocks']}")
    # 关键断言: 只有 1 篇变化 → 只拉 1 次 blocks（其余 docx 走版本闸门零拉取）
    assert api_calls["blocks"] == 1, f"增量失效: 拉了 {api_calls['blocks']} 次 blocks"
    assert stats2.written >= 1

    # 第三次: 本地文件被删 → 强制重拉（云端为准）
    (out / "根目录" / "子文档A.md").unlink()
    api_calls["blocks"] = 0
    s3 = T.WikiSyncer(client, out, WIKI_SPACE, skip_tables=True)
    stats3 = s3.sync_from("wikin_root_001")
    print(f"三轮(本地删1篇): written={stats3.written} blocks_calls={api_calls['blocks']}")
    assert (out / "根目录" / "子文档A.md").exists(), "本地删除后未恢复"
    assert api_calls["blocks"] == 1

    # 第四次: 云端 obj_edit_time 未变 + 本地被篡改 → 闸门视为未变?
    # 设计决策: 篡改本地不在闸门检测范围（git 兜底），但需明确此语义
    p = out / "根目录" / "组织指南.md"
    p.write_text("被篡改的内容", "utf-8")
    s4 = T.WikiSyncer(client, out, WIKI_SPACE, skip_tables=True)
    stats4 = s4.sync_from("wikin_root_001")
    content = p.read_text("utf-8")
    print(f"四轮(本地篡改): 被闸门跳过={content.startswith('被篡改')}")
    print("（篡改语义: 闸门不覆盖, 由 git 检出兜底 —— 文档已注明）")

    # 单元级
    n = {"obj_token": "t1", "obj_edit_time": 123}
    assert node_version(n) == "123"
    assert node_content_key(n) == "t1"
    assert needs_sync(None, "123", Path("/x")) is True          # 新文档
    assert needs_sync({"edit_time": "123"}, "123", Path("/nonexistent")) is True  # 文件丢失
    assert needs_sync({"edit_time": "123"}, "123", Path("/etc/hostname")) is False  # 未变
    assert needs_sync({"edit_time": "123"}, "456", Path("/etc/hostname")) is True  # 云端改
    print("单元断言通过")
    print("=== 增量闸门测试全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
