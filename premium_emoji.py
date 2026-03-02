from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from html.parser import HTMLParser

from aiogram import Bot
from aiogram.types import Message, MessageEntity


VS16 = "\ufe0f"


def _utf16_len(s: str) -> int:
    """Количество UTF-16 code units (как считает Telegram для entities)."""
    return len(s.encode("utf-16-le")) // 2


def _with_vs16_variants(emoji: str) -> List[str]:
    """
    Возвращает варианты эмодзи:
    - как есть
    - без VS16 (FE0F)
    - если базовый вариант 1 codepoint -> добавляем вариант с VS16
    """
    if not emoji:
        return []

    variants = []
    variants.append(emoji)

    no_vs = emoji.replace(VS16, "")
    if no_vs and no_vs not in variants:
        variants.append(no_vs)

    # Добавляем вариант "с VS16" только для простых одиночных символов (BMP типа ⚠, ❄, ⛔, ☑ и т.п.)
    # Для ZWJ/составных последовательностей не лезем — можно сломать.
    if VS16 not in no_vs and len(no_vs) == 1:
        v = no_vs + VS16
        if v not in variants:
            variants.append(v)

    return variants


@dataclass
class PremiumEmoji:
    """
    Premium emoji через custom_emoji entities.
    Также умеет отправлять HTML-текст, конвертируя <b>/<i>/<a href> в entities.
    """
    emoji_map: Dict[str, str]

    @classmethod
    async def from_sticker_sets(cls, bot: Bot, sticker_set_names: List[str]) -> "PremiumEmoji":
        """
        Авто-собирает маппинг emoji -> custom_emoji_id из emoji-паков по их short_name.
        + Дублирует варианты с/без FE0F (VS16), чтобы ⚠ и ⚠️ совпадали.
        """
        m: Dict[str, str] = {}

        for name in sticker_set_names:
            if not name:
                continue
            ss = await bot.get_sticker_set(name=name)
            for st in ss.stickers:
                emoji = getattr(st, "emoji", None)
                ceid = getattr(st, "custom_emoji_id", None)
                if not emoji or not ceid:
                    continue

                for v in _with_vs16_variants(emoji):
                    m.setdefault(v, ceid)

        return cls(emoji_map=m)

    # ---------------- premium emoji entities ----------------
    def build_custom_emoji_entities(self, plain_text: str) -> List[MessageEntity]:
        entities: List[MessageEntity] = []
        if not plain_text or not self.emoji_map:
            return entities

        # Длинные ключи раньше, чтобы ❤️ не перебивалось ❤ и т.п.
        items: List[Tuple[str, str]] = sorted(
            self.emoji_map.items(),
            key=lambda x: len(x[0]),
            reverse=True
        )

        occupied = [False] * (len(plain_text) + 1)

        def find_all(hay: str, needle: str):
            start = 0
            while True:
                i = hay.find(needle, start)
                if i == -1:
                    return
                yield i
                start = i + len(needle)

        for uni, ceid in items:
            for pos in find_all(plain_text, uni):
                end = pos + len(uni)
                if any(occupied[pos:end]):
                    continue
                for k in range(pos, end):
                    occupied[k] = True

                entities.append(
                    MessageEntity(
                        type="custom_emoji",
                        offset=_utf16_len(plain_text[:pos]),
                        length=_utf16_len(uni),
                        custom_emoji_id=ceid,
                    )
                )

        entities.sort(key=lambda e: e.offset)
        return entities

    # ---------------- HTML -> entities ----------------
    class _HTMLToEntities(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out: List[str] = []
            self.entities: List[MessageEntity] = []
            self.stack: List[Tuple[str, int, Optional[str]]] = []

        def _current_utf16(self) -> int:
            return _utf16_len("".join(self.out))

        def handle_starttag(self, tag: str, attrs):
            tag = tag.lower()
            if tag == "br":
                self.out.append("\n")
                return

            if tag in ("b", "strong"):
                self.stack.append(("bold", self._current_utf16(), None))
                return

            if tag in ("i", "em"):
                self.stack.append(("italic", self._current_utf16(), None))
                return

            if tag == "u":
                self.stack.append(("underline", self._current_utf16(), None))
                return

            if tag in ("s", "strike", "del"):
                self.stack.append(("strikethrough", self._current_utf16(), None))
                return

            if tag == "code":
                self.stack.append(("code", self._current_utf16(), None))
                return

            if tag == "pre":
                self.stack.append(("pre", self._current_utf16(), None))
                return

            if tag == "a":
                href = None
                for k, v in attrs:
                    if k.lower() == "href":
                        href = v
                        break
                self.stack.append(("text_link", self._current_utf16(), href))
                return

        def handle_endtag(self, tag: str):
            tag = tag.lower()
            tag_map = {
                "b": "bold", "strong": "bold",
                "i": "italic", "em": "italic",
                "u": "underline",
                "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
                "code": "code",
                "pre": "pre",
                "a": "text_link",
            }
            t = tag_map.get(tag)
            if not t:
                return

            for idx in range(len(self.stack) - 1, -1, -1):
                st_tag, st_off, extra = self.stack[idx]
                if st_tag != t:
                    continue

                end_off = self._current_utf16()
                length = end_off - st_off
                if length > 0:
                    if st_tag == "text_link":
                        if extra:
                            self.entities.append(
                                MessageEntity(type="text_link", offset=st_off, length=length, url=extra)
                            )
                    else:
                        self.entities.append(
                            MessageEntity(type=st_tag, offset=st_off, length=length)
                        )

                self.stack.pop(idx)
                break

        def handle_data(self, data: str):
            if data:
                self.out.append(data)

    def html_to_entities(self, html_text: str) -> Tuple[str, List[MessageEntity]]:
        p = self._HTMLToEntities()
        p.feed(html_text or "")
        p.close()

        plain = "".join(p.out)
        ents = p.entities
        ents.sort(key=lambda e: e.offset)
        return plain, ents

    # ---------------- send helpers ----------------
    async def answer_html(self, message: Message, html_text: str, **kwargs):
        plain, base_entities = self.html_to_entities(html_text)
        emoji_entities = self.build_custom_emoji_entities(plain)
        entities = sorted(base_entities + emoji_entities, key=lambda e: e.offset)

        kwargs.pop("parse_mode", None)
        return await message.answer(plain, entities=entities, parse_mode=None, **kwargs)

    async def edit_html(self, message: Message, html_text: str, **kwargs):
        plain, base_entities = self.html_to_entities(html_text)
        emoji_entities = self.build_custom_emoji_entities(plain)
        entities = sorted(base_entities + emoji_entities, key=lambda e: e.offset)

        kwargs.pop("parse_mode", None)
        return await message.edit_text(plain, entities=entities, parse_mode=None, **kwargs)