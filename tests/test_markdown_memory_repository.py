from pathlib import Path

from bot.database import Database
from bot.memory import MarkdownMemoryRepository, estimate_tokens


def _repository(tmp_path, *, budget=4000):
    db = Database(str(tmp_path / "memory.db"))
    db.add_memory("喜欢简短回复", memory_type="preference")
    db.add_memory("正在准备 PTE trial", memory_type="plan")
    repository = MarkdownMemoryRepository(
        str(tmp_path / "memory.md"),
        legacy_repository=db,
        token_budget=budget,
    )
    return db, repository


def test_first_start_migrates_legacy_rows_to_markdown(tmp_path):
    _db, repository = _repository(tmp_path)

    document = repository.read_document()
    memories = repository.list(include_expired=True)

    assert document.startswith("# User Memory")
    assert "## Preferences" in document
    assert "## Current Plans" in document
    assert {item["content"] for item in memories} == {
        "喜欢简短回复",
        "正在准备 PTE trial",
    }
    assert all("<!-- memory:" not in item["content"] for item in memories)


def test_crud_is_atomic_and_keeps_legacy_shadow_in_sync(tmp_path):
    db, repository = _repository(tmp_path)

    memory_id = repository.save("喜欢柔和的睡前提醒", memory_type="interaction_style")
    repository.update(memory_id, content="睡前提醒不要倒计时")

    markdown_item = next(item for item in repository.list() if item["id"] == memory_id)
    shadow_item = next(
        item for item in db.get_all_memories(include_expired=True)
        if item["id"] == memory_id
    )
    assert markdown_item["content"] == "睡前提醒不要倒计时"
    assert shadow_item["content"] == "睡前提醒不要倒计时"
    assert not list(Path(tmp_path).glob(".memory.md.*.tmp"))

    repository.delete(memory_id)
    assert all(item["id"] != memory_id for item in repository.list())
    assert all(
        item["id"] != memory_id
        for item in db.get_all_memories(include_expired=True)
    )


def test_prompt_markdown_strips_metadata_and_respects_budget(tmp_path):
    db = Database(str(tmp_path / "memory.db"))
    for index in range(4):
        db.add_memory(f"记忆 {index}: " + "很长的内容" * 25, memory_type="preference")
    repository = MarkdownMemoryRepository(
        str(tmp_path / "memory.md"), legacy_repository=db, token_budget=150
    )

    prompt = repository.prompt_markdown()

    assert "<!-- memory:" not in prompt
    assert estimate_tokens(prompt) <= 150
    assert "Some memories were omitted" in prompt
    assert prompt.count("记忆 ") < 4


def test_replace_document_rejects_malformed_metadata(tmp_path):
    _db, repository = _repository(tmp_path)
    original = repository.read_document()

    try:
        repository.replace_document("# User Memory\n\n<!-- memory:not-json -->\n- broken")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed managed blocks must not be accepted")

    assert repository.read_document() == original
