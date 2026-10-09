"""Token estimation shared by the hooks and the MCP tool boundary."""


def approx_tokens(text: str | None) -> int:
    """Rough token estimate (~4 chars/token). Cheap on purpose; no tokenizer dependency."""
    return (len(text) + 3) // 4 if text else 0
