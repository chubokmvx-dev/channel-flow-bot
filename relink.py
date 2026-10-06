"""Замена ссылки во всём посте: видимый текст, скрытые ссылки под словами, кнопки (текст и адрес).
Форматирование сохраняется: смещаем границы entities (в UTF-16, как в Telegram) после замены."""
import html
import json
import re

URL_RE = re.compile(r"^(?:https?://)?(.+?)/?$")


def core(url: str) -> str:
    """Ссылка без схемы и хвостового слеша: t.me/+AbC → для сравнения и поиска."""
    m = URL_RE.match((url or "").strip())
    return m.group(1) if m else (url or "").strip()


def full(url: str) -> str:
    u = (url or "").strip()
    return u if re.match(r"^(?:https?|tg)://", u) else "https://" + u


def _pat(old: str):
    return re.compile(re.escape(core(old)) + r"(?![\w\-])")


def _u16(s: str, i: int) -> int:
    return len(s[:i].encode("utf-16-le")) // 2


def replace_text(plain: str, ents: list[dict], olds: list[str], new: str):
    """→ (plain, ents, changed). ents — список словарей как в aiogram (type, offset, length, url, ...)."""
    new_core, new_full = core(new), full(new)
    reps = []  # (start16, end16, newlen16) по возрасту в исходном тексте
    spans = []
    for old in olds:
        for mt in _pat(old).finditer(plain):
            spans.append((mt.start(), mt.end()))
    spans.sort()
    clean, last = [], -1
    for s, e in spans:          # без пересечений
        if s >= last:
            clean.append((s, e))
            last = e
    out, pos = [], 0
    for s, e in clean:
        out.append(plain[pos:s])
        out.append(new_core)
        reps.append((_u16(plain, s), _u16(plain, e), len(new_core.encode("utf-16-le")) // 2))
        pos = e
    out.append(plain[pos:])
    new_plain = "".join(out)
    changed = bool(clean)

    def mp(p: int, is_end: bool) -> int:
        shift = 0
        for s, e, nl in reps:
            if e <= p:
                shift += nl - (e - s)
            elif s < p < e:
                return s + shift + (nl if is_end else 0)
        return p + shift

    old_cores = {core(o) for o in olds}
    res = []
    for en in ents or []:
        en = dict(en)
        o, l = en["offset"], en["length"]
        no, ne = mp(o, False), mp(o + l, True)
        en["offset"], en["length"] = no, max(0, ne - no)
        if en.get("url") and core(en["url"]) in old_cores:
            en["url"] = new_full
            changed = True
        res.append(en)
    return new_plain, res, changed


def replace_html(text_html: str, olds: list[str], new: str):
    """Для text_html (aiogram): заменяет и в href, и в видимом тексте (с учётом &amp;)."""
    new_core, new_full = core(new), full(new)
    changed = False
    for old in olds:
        for variant in {core(old), html.escape(core(old), quote=False), html.escape(core(old))}:
            pat = re.compile(re.escape(variant) + r"(?![\w\-])")
            rep = new_core if variant == core(old) else html.escape(new_core, quote=False)
            text_html, n = pat.subn(rep.replace("\\", "\\\\"), text_html)
            changed = changed or n > 0
    return text_html, changed


def replace_btns(btns: list[dict], olds: list[str], new: str):
    new_core, new_full = core(new), full(new)
    old_cores = {core(o) for o in olds}
    changed, out = False, []
    for b in btns or []:
        b = dict(b)
        if core(b.get("url", "")) in old_cores:
            b["url"] = new_full
            changed = True
        t, _, ch = replace_text(b.get("text", ""), [], olds, new)
        if ch:
            b["text"] = t[:60]
            changed = True
        out.append(b)
    return out, changed


def relink_fields(d: dict, olds: list[str], new: str) -> dict:
    """Принимает поля поста (text_html, plain, ents, btns, btn_url, btn_text), возвращает только изменившиеся."""
    upd = {}
    olds = [o for o in olds if o and core(o) != core(new)]
    if not olds or not new:
        return upd
    if d.get("text_html"):
        t, ch = replace_html(d["text_html"], olds, new)
        if ch:
            upd["text_html"] = t
    if d.get("plain") is not None:
        try:
            ents = json.loads(d["ents"]) if d.get("ents") else []
        except ValueError:
            ents = []
        p, e, ch = replace_text(d["plain"], ents, olds, new)
        if ch:
            upd["plain"], upd["ents"] = p, json.dumps(e)
    if d.get("btns"):
        try:
            bl = json.loads(d["btns"])
        except ValueError:
            bl = []
        b, ch = replace_btns(bl, olds, new)
        if ch:
            upd["btns"] = json.dumps(b, ensure_ascii=False)
    if d.get("btn_url") and core(d["btn_url"]) in {core(o) for o in olds}:
        upd["btn_url"] = full(new)
        if d.get("btn_text"):
            upd["btn_text"] = replace_text(d["btn_text"], [], olds, new)[0]
    if upd:
        upd["src_msg"] = None   # копирование исходного сообщения принесло бы старую ссылку
    return upd


def find_links(d: dict) -> set[str]:
    """Приглашения t.me/+… (и joinchat) в поле поста: чтобы угадать «старую» ссылку при первом запуске."""
    found = set()
    inv = re.compile(r"t\.me/(?:\+|joinchat/)[\w\-]+")
    blob = " ".join(filter(None, [d.get("plain"), d.get("text_html"), d.get("btns"), d.get("btn_url"), d.get("ents")]))
    found.update(inv.findall(blob.replace("\\/", "/")))
    return found
