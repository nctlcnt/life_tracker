"""main_template 渲染器的边界行为（LT-129）。"""
from bot.prompts import PromptParts, build_prompt, synthesize_main_template

BASE_VALUES = {
    "tools": "工具说明",
    "projects": "【项目】\n- 无",
    "memories": "【记忆】\n- 一条",
    "relevant_history": "",
    "today_timeline": "【Timeline】\n- 空",
    "pending_reminders": "",
    "deadlines": "",
    "weather": "",
    "calendar": "",
}


def _render(template: str, **overrides) -> PromptParts:
    return PromptParts(mode="chat", template=template,
                       values={**BASE_VALUES, **overrides})


def test_literal_braces_preserved():
    """颜文字 / JSON 示例 / 未知 token 的花括号原样保留，不被当占位符。"""
    p = _render('前缀 {>_<} 中缀 {"json": true} {tolos} {memories} 后缀')
    flat = p.flatten()
    assert "{>_<}" in flat
    assert '{"json": true}' in flat
    assert "{tolos}" in flat            # 拼写错误的占位符保持字面（API 校验层负责拦截）
    assert "【记忆】" in flat           # 真占位符正常展开
    assert "{memories}" not in flat


def test_values_not_rescanned():
    """占位符的值只替换一次：值里出现 {memories} 字样不会被二次展开。"""
    p = _render("{tools}\n\n{memories}", tools="说明：{memories} 是记忆占位符")
    flat = p.flatten()
    assert "说明：{memories} 是记忆占位符" in flat
    assert "【记忆】" in flat


def test_blocks_monotonic_and_capped():
    """乱序模板：tier 仍单调、块数 ≤4，内容不丢。"""
    p = _render("{weather}\n\n散文在最后\n\n{tools}\n\n{projects}",
                weather="晴", tools="T", projects="P")
    blocks = p.render_blocks()
    tiers = [t for t, _ in blocks]
    assert tiers == sorted(tiers) and len(blocks) <= 4
    assert p.flatten().count("散文在最后") == 1
    # weather(tier4) 先出现 → 后续内容全并进 tier4 段（正确但 cache 效率降级）
    assert tiers == [4]


def test_duplicate_placeholder():
    p = _render("{memories}\n\n中间\n\n{memories}")
    assert p.flatten().count("【记忆】") == 2


def test_template_without_placeholders():
    p = _render("纯散文，没有任何占位符")
    assert p.render_blocks() == [(1, "纯散文，没有任何占位符")]


def test_empty_values_collapse():
    """空值占位符不留悬空空行。"""
    p = _render("A\n\n{weather}\n\n{deadlines}\n\nB")
    assert p.render_blocks() == [(1, "A"), (4, "B")]


def test_build_prompt_empty_template_falls_back_to_synthesize():
    sections = {"identity": "我是日和", "tools": "工具", "main_template": ""}
    p = build_prompt("chat", sections=sections)
    expected = synthesize_main_template({"identity": "我是日和", "tools": "工具"})
    assert p.template == expected
    assert p.flatten().startswith("我是日和")


def test_concise_drops_tools_only():
    p = _render("散文\n\n{tools}\n\n{memories}")
    full, concise = p.flatten(), p.concise().flatten()
    assert "工具说明" in full and "工具说明" not in concise
    assert "【记忆】" in concise
    # concise 是副本，原 PromptParts 不受影响
    assert "工具说明" in p.flatten()
