"""Ставка со скрина → пост по шаблону: распознавание (Claude Vision), форматирование, подстановка меток, банк."""
import base64
import html
import json
import os
import re



# метки в тексте шаблона
TOKENS = ("{матч}", "{флаг1}", "{флаг2}", "{команда1}", "{команда2}", "{исход}", "{ставка}", "{банк}", "{коэф}")

PROMPT = """На скриншоте купон ставки (БК). Достань данные только из самой ставки и верни ТОЛЬКО JSON без пояснений:
{
 "team1": "первая команда как на скрине",
 "team2": "вторая команда как на скрине",
 "league": "турнир как на скрине, например «Футбол. Чемпионат Норвегии. Элитсерия»",
 "iso1": "двухбуквенный код страны КОМАНДЫ 1 (ISO 3166-1 alpha-2, например NO, ES); для Англии GB-ENG, Шотландии GB-SCT, Уэльса GB-WLS",
 "iso2": "то же для КОМАНДЫ 2",
 "market": "рынок как написан на скрине, например «Тотал. (1) Б. 1-й тайм»",
 "kind": "total | other",
 "period": "1-й тайм | 2-й тайм | матч",
 "line": число линии тотала (для «Тотал (1) Б» это 1; для «(2.5)» это 2.5), иначе null,
 "side": "Б" для больше, "М" для меньше, иначе null,
 "stake": число, сумма ставки,
 "odds": число, коэффициент,
 "currency": "символ валюты, например ₽"
}
Про страны: если турнир национальный (чемпионат/кубок одной страны), у обеих команд страна этого турнира.
Если турнир международный (Лига чемпионов, Лига Европы, Лига конференций, сборные и т.п.), определи страну каждого клуба по названию и логотипу.
Если страну определить не можешь, поставь null."""


def flag(iso: str | None) -> str:
    if not iso:
        return ""
    iso = iso.strip().upper()
    sub = {"GB-ENG": "gbeng", "GB-SCT": "gbsct", "GB-WLS": "gbwls"}
    if iso in sub:   # флаги Англии, Шотландии, Уэльса: чёрный флаг + теговые символы
        return "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in sub[iso]) + "\U000E007F"
    if len(iso) == 2 and iso.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)
    return ""


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else (f"{x:.2f}").rstrip("0").rstrip(".")


def outcome(d: dict) -> str:
    """«Тотал (1) Б, 1-й тайм» → «1-й тайм Тотал 0.5-1Б»; «Тотал 2 Б» → «1.5-2Б». Остальные рынки: как на скрине."""
    period = (d.get("period") or "").strip()
    pre = "" if period in ("", "матч") else period + " "
    if d.get("kind") == "total" and d.get("line") is not None and d.get("side") == "Б":
        n = float(d["line"])
        rng = f"{_num(n)}Б" if n % 1 == 0.5 else f"{_num(n - 0.5)}-{_num(n)}Б"
        return f"{pre}Тотал {rng}"
    if d.get("kind") == "total" and d.get("line") is not None:
        return f"{pre}Тотал {_num(float(d['line']))}{d.get('side') or ''}"
    return (pre + (d.get("market") or "")).strip()


def money(v: int, cur: str = "₽") -> str:
    return f"{int(v):,}".replace(",", ".") + (cur or "₽")


def parse_money(text: str) -> int | None:
    """«250.000₽», «250 000», «250000» → 250000. Копейки отбрасываем."""
    t = re.sub(r"[^\d.,\s]", "", text or "").strip()
    if not re.search(r"\d", t):
        return None
    m = re.fullmatch(r"\d{1,3}(?:[.\s]\d{3})+|\d+", t.split(",")[0].strip())
    if not m:
        return None
    return int(re.sub(r"\D", "", m.group(0)))


def bank_from_post(text: str) -> int | None:
    """Достаёт банк из текста поста вида «Ставим: 20.889₽ | Банк: 250.000₽»."""
    m = re.search(r"банк\W{0,3}([\d][\d.\s,]*)", text or "", re.I)
    return parse_money(m.group(1)) if m else None


def next_bank(state: dict, stake: int) -> tuple[int | None, str]:
    """Банк для нового поста. state: bank, last_stake, won. Возвращает (банк, пояснение)."""
    bank, last, won = state.get("bank"), state.get("last_stake"), state.get("won")
    if bank is None:
        return None, "банк не задан"
    if won or not last:
        return bank, f"сохранённый банк {money(bank)}"
    return bank - last, f"{money(bank)} − прошлая ставка {money(last)} (победы не было)"


def values(d: dict, bank: int | None) -> dict:
    f1, f2 = flag(d.get("iso1")), flag(d.get("iso2"))
    t1, t2 = d.get("team1", ""), d.get("team2", "")
    cur = d.get("currency") or "₽"
    return {
        "{матч}": f"{f1}{t1} - {t2}{f2}", "{флаг1}": f1, "{флаг2}": f2, "{команда1}": t1, "{команда2}": t2,
        "{исход}": outcome(d), "{ставка}": money(d["stake"], cur) if d.get("stake") is not None else "",
        "{банк}": money(bank, cur) if bank is not None else "", "{коэф}": _num(d["odds"]) if d.get("odds") else "",
    }


def _u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _subst_plain(plain: str, ents: list[dict], mp: dict):
    """Заменяет метки в тексте, сдвигая entities (UTF-16, как в Telegram). → (plain, ents, changed)."""
    pat = re.compile("|".join(re.escape(k) for k in sorted(mp, key=len, reverse=True)))
    reps, out, pos = [], [], 0
    for mt in pat.finditer(plain):
        out.append(plain[pos:mt.start()])
        out.append(mp[mt.group(0)])
        reps.append((_u16len(plain[:mt.start()]), _u16len(plain[:mt.end()]), _u16len(mp[mt.group(0)])))
        pos = mt.end()
    out.append(plain[pos:])
    if not reps:
        return plain, ents, False

    def at(p: int, is_end: bool) -> int:
        shift = 0
        for s, e, nl in reps:
            if e <= p:
                shift += nl - (e - s)
            elif s < p < e:
                return s + shift + (nl if is_end else 0)
        return p + shift

    res = []
    for en in ents or []:
        en = dict(en)
        no, ne = at(en["offset"], False), at(en["offset"] + en["length"], True)
        en["offset"], en["length"] = no, max(0, ne - no)
        res.append(en)
    return "".join(out), res, True


def fill_item(d: dict, mp: dict) -> dict:
    """Поля поста с подставленными метками. Возвращает только изменившиеся (+ src_msg=None, чтобы не копировался оригинал)."""
    upd = {}
    th = d.get("text_html") or ""
    new_h = th
    for k, v in mp.items():
        new_h = new_h.replace(k, html.escape(v, quote=False))
    if new_h != th:
        upd["text_html"] = new_h
    if d.get("plain") is not None:
        try:
            ents = json.loads(d["ents"]) if d.get("ents") else []
        except ValueError:
            ents = []
        p, e, ch = _subst_plain(d["plain"], ents, mp)
        if ch:
            upd["plain"], upd["ents"] = p, json.dumps(e)
    for fld in ("btn_text", "btn_url", "btns"):
        if d.get(fld):
            nv = d[fld]
            for k, v in mp.items():
                nv = nv.replace(k, v)
            if nv != d[fld]:
                upd[fld] = nv
    if upd:
        upd["src_msg"] = None
        upd["text_msg"] = None
        upd["text_ts"] = None
    return upd


def has_tokens(items: list[dict]) -> bool:
    for it in items:
        blob = " ".join(str(it.get(f) or "") for f in ("text_html", "plain", "btns", "btn_text", "btn_url"))
        if any(t in blob for t in TOKENS):
            return True
    return False


async def read_screenshot(image: bytes) -> dict:
    """Claude Vision: данные ставки со скрина. Бросает RuntimeError с понятным текстом."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Не задана переменная ANTHROPIC_API_KEY в Railway Variables у этого бота.")
    body = {
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 700,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": PROMPT}]}],
    }
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        async with s.post("https://api.anthropic.com/v1/messages", json=body,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"}) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Claude API {r.status}: {str(data)[:200]}")
    raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1 or b == -1:
        raise RuntimeError("Не смог прочитать ставку со скрина.")
    d = json.loads(raw[a:b + 1])
    if not d.get("team1") or not d.get("team2") or d.get("stake") is None:
        raise RuntimeError("На скрине не нашёл команды или сумму ставки. Пришли скрин ещё раз, чтобы ставка была видна целиком.")
    d["stake"] = int(round(float(d["stake"])))
    return d
