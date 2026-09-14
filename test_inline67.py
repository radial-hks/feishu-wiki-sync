"""验证 ```67 乱序语言标识场景（真实数据中发现）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from feishu_wiki_sync import extract_inline_props

body = "```67\n---\ntype: 指南 \ndepartment: 工程与交付\nowner: 陈蓓\n---\n```\n\n# 正文"
props, cleaned = extract_inline_props(body)
print("props:", props)
assert props.get("type") == "指南", "type 解析失败"
assert props.get("owner") == "陈蓓", "owner 解析失败"
assert "```" not in cleaned, "代码块未从正文移除"
assert "# 正文" in cleaned
print("67 乱序标识场景通过")
