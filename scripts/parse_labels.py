#!/usr/bin/env python3
"""
解析人工标注的消息,导出为 dispatch-labels.json

用法:
    python parse_labels.py <input_file> [-o output.json]
    cat annotations.txt | python parse_labels.py

标注格式: 在 id 行前加 x 表示 need_tool=true,否则 false
    x id=1944  ts=2026-04-21 07:08:28    -> {"id": 1944, "need_tool": true}
      id=1267  ts=2026-04-17 08:17:41    -> {"id": 1267, "need_tool": false}
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 匹配可选的 x 标注 + id=数字
# ^\s*(x\s+)? 开头可选 x,  id=(\d+) 捕获 id
LINE_RE = re.compile(r'^\s*(x\s+)?id=(\d+)\b', re.IGNORECASE)


def parse(text: str) -> list[dict]:
    labels = []
    seen = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        m = LINE_RE.match(line)
        if not m:
            continue
        has_x = m.group(1) is not None
        msg_id = int(m.group(2))
        if msg_id in seen:
            print(f"警告: id={msg_id} 重复 (第 {lineno} 行)", file=sys.stderr)
            continue
        seen.add(msg_id)
        labels.append({"id": msg_id, "need_tool": has_x})
    return labels


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="输入文件 (省略则从 stdin 读)")
    ap.add_argument("-o", "--output", default="plans/dispatch-labels.json", help="输出路径")
    args = ap.parse_args()

    text = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    labels = parse(text)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    n_true = sum(l["need_tool"] for l in labels)
    print(f"✓ 写入 {out}: 共 {len(labels)} 条 (need_tool=true: {n_true}, false: {len(labels) - n_true})")


if __name__ == "__main__":
    main()