"""Офлайн-тест agent/limits.py: набір бюджетів залежить від ефективного бекенда."""
from agent import limits


def main() -> None:
    assert limits.FORCE is None
    # локально: ті самі «серверні» правила (без милиць), але бюджети під вікно LOCAL_CTX; сервер — без змін
    from agent import config
    loc = limits.get({"llm_backend": "ollama"})
    assert loc.context_tokens == config.LOCAL_CTX and not loc.weak_model_fixes and loc.hard_iters == limits.REMOTE.hard_iters
    assert loc.summary < limits.REMOTE.summary and loc.read_chars < limits.REMOTE.read_chars
    assert limits.get({"llm_backend": "ollama", "llm_context": "32768"}).context_tokens == 32768        # своє вікно
    assert limits.get({"llm_backend": "ollama", "llm_context": "abc"}).context_tokens == config.LOCAL_CTX
    assert limits.get({"llm_backend": "openai"}) is limits.REMOTE
    assert limits.get({"llm_backend": "openai", "llm_context": "32768"}).context_tokens == 32768
    # «милиці» слабкої моделі — лише за налаштуванням (за замовчуванням вимкнено і локально, і на сервері)
    assert not limits.get({"llm_backend": "ollama"}).weak_model_fixes and not limits.get({"llm_backend": "openai"}).weak_model_fixes
    assert limits.get({"llm_backend": "ollama", "weak_model_fixes": True}).weak_model_fixes
    assert limits.get({"llm_backend": "openai", "weak_model_fixes": True}).weak_model_fixes
    r, l = limits.REMOTE, limits.LOCAL
    assert r.chunk > l.chunk and r.inline > l.inline and r.summary > l.summary
    assert r.history_msgs > l.history_msgs and r.file_chars > l.file_chars
    # запас: вкладення inline + підсумок (у токенах ≈ симв./2.5) помітно менші за 256k
    assert (r.inline + r.summary) / 2.5 < 0.5 * 262_144, 'inline+summary має лишати >50% вікна'
    assert l.weak_model_fixes and not r.weak_model_fixes          # LOCAL лишається набором для старої/слабкої моделі (FORCE у тестах)
    limits.FORCE = l
    try:
        assert limits.get({"llm_backend": "openai"}) is l       # FORCE перекриває
    finally:
        limits.FORCE = None
    print("OK: limits — LOCAL/REMOTE за бекендом, FORCE для тестів, запас під 256k")


if __name__ == "__main__":
    main()
