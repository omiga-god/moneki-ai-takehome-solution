"""分词。"""

from __future__ import annotations

import re
import unicodedata

#: 分词规则变了，索引缓存必须失效。
TOKENIZER_VERSION = "tokenizer-3"

#: 中文里几乎不携带信息的字。只用在“查询覆盖率”上，索引照常保留全部词。
STOP_CHARS = frozenset("的了吗呢是在有和与及或就都也还把被给对从向于个些这那哪什么怎样如何多少几请帮我你他它可以能要想会一下少吧啊呀们么样过得着为所")
STOP_WORDS = frozenset("the a an of to in is are and or for on at it this that how what".split())


def normalise(text: str) -> str:
    """全角转半角、统一大小写，比较与分词都走这一层。"""
    return unicodedata.normalize("NFKC", text or "").lower()


_LATIN = re.compile(r"[a-z0-9]+(?:[._+/-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """中文按二字切，英文和数字按词切。中文正文没有空格，按空白切会整句变成一个词。"""
    text = normalise(text)
    tokens: list[str] = []
    cjk: list[str] = []

    def flush_cjk() -> None:
        if not cjk:
            return
        if len(cjk) == 1:
            tokens.append(cjk[0])
        else:
            tokens.extend(cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1))
        cjk.clear()

    index = 0
    while index < len(text):
        char = text[index]
        if "\u4e00" <= char <= "\u9fff":
            cjk.append(char)
            index += 1
            continue
        flush_cjk()
        match = _LATIN.match(text, index)
        if match:
            tokens.append(match.group(0))
            index = match.end()
            continue
        index += 1
    flush_cjk()
    return tokens


def content_tokens(text: str) -> list[str]:
    """去掉虚词之后的查询词，用来算“这个问题被文档覆盖了多少”。"""
    kept = []
    for token in tokenize(text):
        if token in STOP_WORDS:
            continue
        if all(char in STOP_CHARS for char in token):
            continue
        kept.append(token)
    return kept
