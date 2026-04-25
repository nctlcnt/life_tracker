"""MCP server: Obsidian vault 全文检索（stdio transport）。

被 mcp_bot.main 作为子进程 spawn，通过 stdio 暴露 search_notes / read_note 两个工具。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import config
from mcp_bot import obsidian_search

mcp = FastMCP("obsidian")


@mcp.tool()
def search_notes(
    query: str,
    folder: str | None = None,
    tags: list[str] | None = None,
    max_results: int = 8,
) -> list[dict]:
    """在 Obsidian 笔记库做大小写不敏感关键词检索。

    Args:
        query: 关键词（匹配标题和正文）。空字符串表示不按关键词过滤。
        folder: 限制在 vault 下某子目录，例如 "COMP9417"。
        tags: 必须全部包含的 frontmatter tag 列表。
        max_results: 返回数量上限，默认 8。

    Returns:
        匹配笔记列表，每项含 path / title / tags / snippet / matched_lines。
    """
    return obsidian_search.search_notes(
        config.OBSIDIAN_VAULT_PATH,
        query,
        folder=folder,
        tags=tags,
        max_results=max_results,
    )


@mcp.tool()
def read_note(path: str) -> dict:
    """读取一篇笔记完整内容（截断至 3000 字）。先用 search_notes 拿到 path 再调本工具。

    Args:
        path: 相对于 vault 根的路径，由 search_notes 返回。

    Returns:
        {path, title, content, tags}；失败时含 error 字段。
    """
    return obsidian_search.read_note(config.OBSIDIAN_VAULT_PATH, path)


if __name__ == "__main__":
    mcp.run()
