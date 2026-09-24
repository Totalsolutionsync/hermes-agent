from agent.display import build_tool_preview, get_tool_emoji


def test_kynver_memory_write_has_first_class_preview_and_emoji():
    preview = build_tool_preview(
        "kynver_memory_write",
        {
            "content": "Kynver AgentOS is the authoritative memory substrate.",
            "memoryType": "fact",
            "key": "agentos-memory-policy",
        },
    )

    assert preview is not None
    assert preview.startswith('fact/agentos-memory-po')
    assert "Kynver AgentOS is the" in preview
    assert get_tool_emoji("kynver_memory_write") == "🧠"
