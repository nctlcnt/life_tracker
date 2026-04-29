"""
Dispatch POC 步骤 0: 解析 dispatch-escalation-samples.md 的人工标注,
输出 JSONL fixture + 统计报告。

用法:
    python scripts/parse_dispatch_labels.py
    python scripts/parse_dispatch_labels.py --out data/fixtures/escalation-labeled.jsonl

输出:
  - JSONL fixture (每行一条 sample, 含 label/ts/source/msg/tool_use_proxy)
  - stdout: 标签分布 + L1 覆盖率
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_PATH = ROOT / "plans" / "2-specs" / "dispatch-escalation-samples.md"
DEFAULT_OUT = ROOT / "data" / "fixtures" / "escalation-labeled.jsonl"

VALID_LABELS = {"Y", "L", "N", "?", " "}

# `[X] #001  ts=...  source=...  tool_use=YES (3 轮: name1,name2)` 或 `tool_use=NO`
HEADER_RE = re.compile(
    r"^\[(?P<label>.)\]\s+#(?P<id>\d+)\s+"
    r"ts=(?P<ts>\S+)\s+"
    r"source=(?P<source>\S+)\s+"
    r"tool_use=(?P<tool_use>YES|NO)"
    r"(?:\s+\((?P<round_count>\d+)\s*轮:\s*(?P<tools>[^)]+)\))?"
)
MSG_RE = re.compile(r"^\s+msg:\s*(?P<msg>.+)$")


def parse(text: str) -> list[dict]:
    samples = []
    current = None
    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            if current is not None:
                samples.append(current)
            label = m["label"]
            if label not in VALID_LABELS:
                print(f"[WARN] 未知标签 '{label}' on #{m['id']}", file=sys.stderr)
            current = {
                "id": int(m["id"]),
                "label": label.strip() or None,  # ' ' → None (未标注)
                "ts": m["ts"],
                "source": m["source"],
                "tool_use_proxy": {
                    "called": m["tool_use"] == "YES",
                    "rounds": int(m["round_count"]) if m["round_count"] else 1,
                    "tools": [t.strip() for t in m["tools"].split(",")] if m["tools"] else [],
                },
                "msg": "",
            }
            continue
        m2 = MSG_RE.match(line)
        if m2 and current is not None and not current["msg"]:
            current["msg"] = m2["msg"]
    if current is not None:
        samples.append(current)
    return samples


def report(samples: list[dict]):
    from collections import Counter

    labels = Counter(s["label"] for s in samples)
    total = len(samples)

    print(f"总样本数: {total}")
    print()
    print("标签分布:")
    for label in ("Y", "L", "N", "?", None):
        cnt = labels.get(label, 0)
        pct = cnt / total * 100 if total else 0
        name = "未标注" if label is None else f"[{label}]"
        print(f"  {name:>6}: {cnt:3d}  ({pct:5.1f}%)")
    print()

    y_count = labels.get("Y", 0)
    l_count = labels.get("L", 0)
    escalate_total = y_count + l_count
    if escalate_total > 0:
        l1_coverage = l_count / escalate_total * 100
        print(f"SMALL_DECIDE escalate 样本: {escalate_total} (Y={y_count} + L={l_count})")
        print(f"  L1 覆盖率 (L / escalate): {l1_coverage:.1f}%")
        if l1_coverage >= 40:
            print("  → 决策依据: ≥40%, 推荐 POC v2 做 L1 tools (受限 update_timeline_event)")
        elif l1_coverage < 20:
            print("  → 决策依据: <20%, POC v2 不做 L1 tools")
        else:
            print("  → 决策依据: 20-40% 灰区, v1 跑通后再看延迟/成本数据决定")

    unlabeled = labels.get(None, 0)
    if unlabeled > 0:
        print(f"\n[WARN] 还有 {unlabeled} 条未标注:")
        for s in samples:
            if s["label"] is None:
                print(f"  #{s['id']:03d}  {s['msg'][:80]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=SAMPLES_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.samples.exists():
        print(f"[ERROR] 标注文件不存在: {args.samples}", file=sys.stderr)
        sys.exit(1)

    samples = parse(args.samples.read_text(encoding="utf-8"))
    if not samples:
        print("[ERROR] 没解析出任何样本", file=sys.stderr)
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    report(samples)
    print(f"\n输出: {args.out}")


if __name__ == "__main__":
    main()
