"""Детектор зациклення tool-loop'у — замість жорсткого ліміту ітерацій.

Фідбек розробника (діагностика b_0.0.1): агент 7 разів поспіль виконував `run_shell` і
щоразу отримував той самий результат, а крок обірвав ЛІМІТ у 10 ітерацій — ліміт
однаково ріже і справжні довгі задачі, і справжні цикли. Тепер довга робота не
обмежується: ми стежимо ЗА ПРОГРЕСОМ, а не за кількістю ітерацій.

Ознаки «немає прогресу» (результати порівнюємо за хешем; надто короткі, як "rc=0",
не рахуємо — вони законно збігаються):
  A) однакова дія (тул + аргументи) з однаковим результатом 3 рази поспіль;
  B) ≥4 з останніх 6 викликів дали ОДИН І ТОЙ САМИЙ результат (різні команди, та сама стіна);
  C) цикл A-B-A-B / A-B-C-A-B-C, де і дії, і результати повторюються (нової інформації нема);
  D) «жодної нової інформації»: у останніх 12 викликах кожен результат уже траплявся раніше в цьому
     кроці, а різних дій ≤4 (ловить цикли, де результат трохи «плаває», напр. 96/101 символів, або
     короткі відповіді типу "[]" чергуються з довшими — C цього не бачить).

  E) діагностика b_0.0.19: 3 команди по колу ~26 хв, результати щоразу трохи інші (хеші різні) — A–D не
     спрацювали. Тепер: повне вікно з 24 викликів, у якому лише ≤3 різні дії (цифри в аргументах ігноруємо).

Реакція у два етапи: перше виявлення → «warn» (caller додає модель-підказку змінити
підхід або пояснити блокер); повторне ПІСЛЯ підказки → «stop» (крок зупиняється з
поясненням причини). Після warn історія скидається, тож для stop потрібні свіжі докази.
"""
from __future__ import annotations

import hashlib
import json
import re

MIN_RESULT_LEN = 12          # коротші результати ("rc=0", "ok") можуть збігатися законно
WINDOW = 24
NOVELTY_WINDOW = 12         # D: стільки останніх викликів без жодного нового результату
IGNORED_TOOLS = {"project_note"}     # службові: однакові відповіді тут — норма

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
        self.calls: list[tuple[str, str, int, bool]] = []   # (sig, result_hash, result_len, новий_результат)
        self.nsigs: list[str] = []                      # дії без чисел (для E), паралельно з calls
        self.seen: set[str] = set()                     # усі результати цього кроку (не скидається на warn)
        self.warned = False
        self.last_result = ""

    def observe(self, name: str, args: dict, result: str) -> None:
        if name in IGNORED_TOOLS:
            return
        h = _h(result)
        self.calls.append((_sig(name, args), h, len(result or ""), h not in self.seen))
        sg = _sig(name, args)
        self.nsigs.append(re.sub(r"\d+", "#", sg) if name == "run_shell" else sg)   # цифри ігноруємо лише в командах
        self.seen.add(h)
        self.calls = self.calls[-WINDOW:]
        self.nsigs = self.nsigs[-WINDOW:]
        self.last_result = result or ""

    def _repeated(self) -> str | None:
        c = self.calls
        # A: однакова дія з однаковим результатом 3 рази поспіль
        if len(c) >= 3 and len({(s, h) for s, h, _, _ in c[-3:]}) == 1 and c[-1][2] >= MIN_RESULT_LEN:
            return f"однакова дія з однаковим результатом ×3 ({c[-1][0][:60]})"
        # B: ≥4 з останніх 6 — один і той самий (змістовний) результат
        tail = [h for _, h, n, _ in c[-6:] if n >= MIN_RESULT_LEN]
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
                        and len({s for s, _, _, _ in t}) > 1 and all(n >= MIN_RESULT_LEN for _, _, n, _ in t)):
                    return f"цикл із {p} дій, що повторюється без нових результатів"
        # D: давно жодної нової інформації — усі результати вже були, дій мало
        if len(c) >= NOVELTY_WINDOW:
            t = c[-NOVELTY_WINDOW:]
            if not any(novel for _, _, _, novel in t) and len({s for s, _, _, _ in t}) <= 4:
                return f"останні {NOVELTY_WINDOW} викликів не дали жодного нового результату"
        # E: повне вікно викликів складається лише з 1–3 різних дій (числа в аргументах не рахуємо), хай які
        #    результати: вивід «плаває» (час, лічильники), тож A–D його не бачать, але агент крутить те саме
        if len(self.nsigs) >= WINDOW and len(set(self.nsigs)) <= 3:
            return f"останні {WINDOW} викликів — лише {len(set(self.nsigs))} різні дії, що повторюються"
        return None

    def check(self) -> tuple[str, str] | None:
        """None — прогрес є; ("warn", причина) — перше виявлення; ("stop", причина) — після підказки."""
        reason = self._repeated()
        if reason is None:
            return None
        if not self.warned:
            self.warned = True
            self.calls.clear()                          # свіжі докази потрібні для stop
            self.nsigs.clear()
            return ("warn", reason)
        return ("stop", reason)

    def stuck_message(self, reason: str) -> str:
        tail = self.last_result.strip().replace("\n", " ")[:160]
        return f"{STUCK_PREFIX}: {reason}. Останній результат: {tail!r}"


def context_full(prompt_tokens: int, window: int, ratio: float = 0.9) -> bool:
    """Чи контекст майже заповнений (prompt з останнього виклику vs вікно моделі)."""
    return bool(window) and prompt_tokens >= ratio * window
