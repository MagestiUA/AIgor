"""Офлайн-тест agent/limits.py: набір бюджетів залежить від ефективного бекенда."""
from agent import limits


def main() -> None:
    assert limits.FORCE is None
    assert limits.get({"llm_backend": "ollama"}) is limits.LOCAL
    assert limits.get({"llm_backend": "openai"}) is limits.REMOTE
    r, l = limits.REMOTE, limits.LOCAL
    assert r.chunk > l.chunk and r.inline > l.inline and r.summary > l.summary
    assert r.history_msgs > l.history_msgs and r.file_chars > l.file_chars
    # запас: вкладення inline + підсумок (у токенах ≈ симв./2.5) помітно менші за 256k
    assert (r.inline + r.summary) / 2.5 < 0.5 * 262_144
    limits.FORCE = l
    try:
        assert limits.get({"llm_backend": "openai"}) is l       # FORCE перекриває
    finally:
        limits.FORCE = None
    print("OK: limits — LOCAL/REMOTE за бекендом, FORCE для тестів, запас під 256k")


if __name__ == "__main__":
    main()
