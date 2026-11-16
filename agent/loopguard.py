"""Детектор зациклення tool-loop'у — замість жорсткого ліміту ітерацій.

Фідбек розробника (діагностика b_0.0.1): агент 7 разів поспіль виконував `run_shell` і
щоразу отримував той самий результат, а крок обірвав ЛІМІТ у 10 ітерацій — ліміт
однаково ріже і справжні довгі задачі, і справжні цикли. Тепер довга робота не
обмежується: ми стежимо ЗА ПРОГРЕСОМ, а не за кількістю ітерацій.

Ознаки «немає прогресу» (результати порівнюємо за хешем; надто короткі, як "rc=0",
не рахуємо — вони законно збігаються):
  A) однакова дія (тул + аргументи) з однаковим результатом 3 рази поспіль;
  B) ≥4 з останніх 6 викликів дали ОДИН І ТОЙ САМИЙ результат (різні команди, та сама стіна);
  C) цикл A-B-A-B / A-B-C-A-B-C, де і дії, і результати повторюються (нової інформації нема).

Реакція у два етапи: перше виявлення → «warn» (caller додає модель-підказку змінити
підхід або пояснити блокер); повторне ПІСЛЯ підказки → «stop» (крок зупиняється з
поясненням причини). Після warn історія скидається, тож для stop потрібні свіжі докази.
"""
from __future__ import annotations

import hashlib
import json

MIN_RESULT_LEN = 12          # коротші результати ("rc=0", "ok") можуть збігатися законно
WINDOW = 24

WARN_TEXT = (
    "You are repeating the same actions without making progress: {reason}. Do NOT repeat "
    "them again. Last result you received: {last!r}. Work out why it fails and try a "
    "DIFFERENT approach (different tool, different command). If you are genuinely blocked, "
    "explain the blocker to the user and finish the step WITHOUT further tool calls."
)
STUCK_PREFIX = "крок зупинено: агент зациклився"       # callers трактують як невдачу кроку
CONTEXT_FULL_PREFIX = "крок зупинено: контекст майже заповнений"


def _sig(name: str, args: dict) -> str:
    try:
        raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(args)
    return name + ":" + (hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()[:10] if len(raw) > 200 else raw)


def _h(result: str) -> str:
    return hashlib.md5((result or "").encode("utf-8", "replace")).hexdigest()[:12]


class LoopGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []     # (sig, result_hash, result_len)
        self.warned = False
        self.last_result = ""

    def observe(self, name: str, args: dict, result: str) -> None:
        self.calls.append((_sig(name, args), _h(result), len(result or "")))
        self.calls = self.calls[-WINDOW:]
        self.last_result = result or ""

    def _repeated(self) -> str | None:
        c = self.calls
        # A: однакова дія з однаковим результатом 3 рази поспіль
        if len(c) >= 3 and len({(s, h) for s, h, _ in c[-3:]}) == 1 and c[-1][2] >= MIN_RESULT_LEN:
            return f"однакова дія з однаковим результатом ×3 ({c[-1][0][:60]})"
        # B: ≥4 з останніх 6 — один і той самий (змістовний) результат
        tail = [h for _, h, n in c[-6:] if n >= MIN_RESULT_LEN]
        if len(c) >= 6 and tail:
            top = max(set(tail), key=tail.count)
            if tail.count(top) >= 4:
                return f"{tail.count(top)} з останніх 6 викликів дали той самий результат"
        # C: цикл періоду 2 чи 3 (і дії, і результати повторюються) — нової інформації немає
        for p in (2, 3):
            need = p * 3
            if len(c) >= need:
                t = c[-need:]
                if (all(t[i][:2] == t[i + p][:2] for i in range(need - p))
                        and len({s for s, _, _ in t}) > 1 and all(n >= MIN_RESULT_LEN for _, _, n in t)):
                    return f"цикл із {p} дій, що повторюється без нових результатів"
        return None

    def check(self) -> tuple[str, str] | None:
        """None — прогрес є; ("warn", причина) — перше виявлення; ("stop", причина) — після підказки."""
        reason = self._repeated()
        if reason is None:
            return None
        if not self.warned:
            self.warned = True
            self.calls.clear()                          # свіжі докази потрібні для stop
            return ("warn", reason)
        return ("stop", reason)

    def stuck_message(self, reason: str) -> str:
        tail = self.last_result.strip().replace("\n", " ")[:160]
        return f"{STUCK_PREFIX}: {reason}. Останній результат: {tail!r}"


def context_full(prompt_tokens: int, window: int, ratio: float = 0.9) -> bool:
    """Чи контекст майже заповнений (prompt з останнього виклику vs вікно моделі)."""
    return bool(window) and prompt_tokens >= ratio * window
