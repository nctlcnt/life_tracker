"""
校准语义检索的 min_relevance 阈值（换 embedding 模型后必须重跑）。

背景：get_relevant_conversation_snippets 的 min_relevance 与 embedding 模型强绑定
——不同模型的 cosine 分数分布完全不同（智谱 embedding-3 无关配对地板 0.45~0.52、
相关 0.65+；Qwen3-VL-Embedding-8B 整体低一截）。阈值定错的代价不对称：
偏高 = 检索永远不命中（静默失效），偏低 = 注入少量噪音（prompt 里已提示 AI 自行取舍）。

方法（对真实数据打分，三组 query）：
1. 相关探针：人工写的、在历史里有明确目标的问题（如"5881 退课"）→ 目标命中分数
   给出阈值上界（阈值必须低于这些分数才能召回）。
2. 无关探针：历史里确定没聊过的话题 → 全库最高分给出阈值下界（噪音地板）。
3. 随机真实用户消息：模拟线上 query 分布，给出各候选阈值下的"注入率"参考。

query 与线上 chat 路径保持一致：带 "[YYYY-MM-DD HH:MM] " 时间戳前缀的裸消息
（库里是拼接过上下文的 embedding_context）；随机样本只取 user 消息，
且只与"当时已存在、且在 20 条工作窗口之外"的历史比对，模拟真实检索条件。

用法（只依赖标准库，宿主机直接跑，DB 只读）：
    python3 scripts/calibrate_embedding_threshold.py [--db data/life_tracker.db] [--config config.json]
"""
import argparse
import json
import math
import random
import sqlite3
import urllib.request

# 与线上一致的相关探针：问题 → 预期能命中的历史话题关键词（用于人工核对）
RELEVANT_PROBES = [
    ("5881退课的事你还记得吗", "MATH5881 退课"),
    ("ECON5111的seminar是什么时候来着", "ECON5111 seminar"),
    ("ADHD评估现在进行到哪一步了", "ADHD / Myhealth"),
    ("上次做蛋糕做得怎么样", "06-25 做蛋糕"),
    ("去看极光那天玩得开心吗", "07-04 极光"),
    ("上次去GP看诊带Medicare卡的事", "06-21 GP/Medicare"),
    ("作业文件夹我是不是已经建好了", "作业文件夹"),
    ("最近心情很down的时候你安慰过我什么", "07-04 情绪低落"),
]

# 无关探针：确定没聊过的话题，全库最高分即噪音地板
UNRELATED_PROBES = [
    "今天美股行情怎么样",
    "推荐几部科幻电影呗",
    "红烧肉怎么做比较好吃",
    "帮我写一段快速排序的代码",
    "巴黎有什么值得去的博物馆",
]

CANDIDATE_THRESHOLDS = [0.35, 0.40, 0.42, 0.45, 0.48, 0.50, 0.55]
RANDOM_QUERY_COUNT = 25


def embed(cfg: dict, text: str) -> list[float]:
    body = {"model": cfg["model"], "input": text[:2000]}
    if cfg.get("dimensions"):
        body["dimensions"] = cfg["dimensions"]
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/embeddings",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"][0]["embedding"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def top_hits(qemb, corpus, max_id: int | None = None, k: int = 5):
    scored = []
    for row_id, created, content, emb in corpus:
        # 模拟真实检索：只看 query 发出时已在工作窗口之外的历史
        if max_id is not None and row_id >= max_id:
            continue
        scored.append((cosine(qemb, emb), row_id, created, content))
    scored.sort(reverse=True)
    return scored[:k]


def ts_prefix(created_at: str) -> str:
    """线上 query 带 '[YYYY-MM-DD HH:MM] ' 前缀（discord_bot current_content 格式）"""
    return f"[{created_at[:10]} {created_at[11:16]}] "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/life_tracker.db")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)["ai"]["embedding"]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, created_at, content, embedding, role FROM conversation_messages "
        "WHERE embedding IS NOT NULL AND embedding_model = ? ORDER BY id",
        (cfg["model"],),
    ).fetchall()
    corpus = [(r[0], r[1][:10], r[2], json.loads(r[3])) for r in rows]
    print(f"语料 {len(corpus)} 条（model={cfg['model']}）\n")

    print("=" * 30, "相关探针（阈值必须低于目标命中分）", "=" * 30)
    relevant_hit_scores = []
    for query, expect in RELEVANT_PROBES:
        hits = top_hits(embed(cfg, "[2026-07-08 14:30] " + query), corpus)
        print(f"\nQ: {query}   [预期: {expect}]")
        for s, rid, d, c in hits:
            print(f"   {s:.3f}  id={rid} {d}  {c[:45]!r}")
        relevant_hit_scores.append(hits[0][0])

    print("\n" + "=" * 30, "无关探针（噪音地板，阈值必须高于此）", "=" * 30)
    noise_tops = []
    for query in UNRELATED_PROBES:
        hits = top_hits(embed(cfg, "[2026-07-08 14:30] " + query), corpus, k=2)
        noise_tops.append(hits[0][0])
        print(f"Q: {query:24s} top1={hits[0][0]:.3f}  {hits[0][3][:30]!r}")

    print("\n" + "=" * 30, "随机真实 user 消息的注入率（各候选阈值）", "=" * 30)
    user_rows = [
        (r[0], r[1], r[2]) for r in rows
        if r[4] == "user" and len((r[2] or "").strip()) >= 6
    ]
    all_ids = [r[0] for r in rows]
    random.seed(42)
    samples = random.sample(user_rows, min(RANDOM_QUERY_COUNT, len(user_rows)))
    top1s = []
    for row_id, created_at, content in samples:
        # 工作窗口 = query 前 20 条；只与窗口外、当时已存在的历史比对
        older = [i for i in all_ids if i <= row_id]
        if len(older) <= 21:
            continue
        window_start = older[-21]
        hits = top_hits(embed(cfg, ts_prefix(created_at) + content),
                        corpus, max_id=window_start, k=1)
        if hits:
            top1s.append(hits[0][0])
    top1s.sort()
    for t in CANDIDATE_THRESHOLDS:
        rate = sum(1 for s in top1s if s >= t) / len(top1s)
        print(f"  阈值 {t:.2f} → {rate:.0%} 的消息会注入至少 1 条片段")

    print("\n" + "=" * 30, "汇总", "=" * 30)
    print(f"相关探针 top1 分数: min={min(relevant_hit_scores):.3f} "
          f"median={sorted(relevant_hit_scores)[len(relevant_hit_scores)//2]:.3f} "
          f"max={max(relevant_hit_scores):.3f}")
    print(f"无关探针噪音地板:   min={min(noise_tops):.3f} max={max(noise_tops):.3f}")
    print("建议阈值取「无关探针最高分」与「相关探针最低分」之间，向下界靠（漏检比噪音更伤）。")


if __name__ == "__main__":
    main()
