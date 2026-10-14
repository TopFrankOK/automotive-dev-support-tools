"""A2L 语法层：分词与分块。

A2L 的结构其实非常简单，本质是一个嵌套列表：

    /begin CHARACTERISTIC
        Kp
        "比例增益"
        VALUE
        0x80001234
        ...
    /end CHARACTERISTIC

所以不需要词法分析器生成器，一个手写扫描器加一个栈就够了。
把它从 `parser.py` 里分出来，是为了让「语法」与「语义映射」
分开 —— 换一种描述格式（比如 CDFX）时，前者完全不复用，
后者（映射到 CalibrationModel 的逻辑）才是真正值钱的部分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Block", "tokenize", "parse_blocks", "unquote"]


@dataclass
class Block:
    """一个 `/begin X ... /end X` 块。"""

    type: str
    items: list[str | Block] = field(default_factory=list)

    @property
    def scalars(self) -> list[str]:
        """本层的标量项（不含子块），按出现顺序。"""
        return [it for it in self.items if isinstance(it, str)]

    def sub(self, block_type: str) -> Block | None:
        for it in self.items:
            if isinstance(it, Block) and it.type == block_type:
                return it
        return None

    def subs(self, block_type: str) -> list[Block]:
        return [it for it in self.items if isinstance(it, Block) and it.type == block_type]

    def find_all(self, block_type: str) -> list[Block]:
        """递归查找所有指定类型的块。"""
        found: list[Block] = []
        for it in self.items:
            if isinstance(it, Block):
                if it.type == block_type:
                    found.append(it)
                found.extend(it.find_all(block_type))
        return found

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Block {self.type} items={len(self.items)}>"


def unquote(token: str) -> str:
    """去掉字符串的引号并还原转义。非字符串原样返回。"""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        body = token[1:-1]
        return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return token


def is_quoted(token: str) -> bool:
    return len(token) >= 2 and token[0] == '"' and token[-1] == '"'


def tokenize(text: str) -> list[str]:
    """扫描出 token 列表。

    处理三件事：块注释 `/* */`、行注释 `//`、双引号字符串。
    字符串保留引号一起返回，由 `unquote` 负责去掉 ——
    这样调用方可以区分「字符串 'VALUE'」与「标识符 VALUE」，
    在 A2L 里这两者语义完全不同。
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
            continue

        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j])
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            tokens.append('"' + "".join(buf) + '"')
            i = j + 1
            continue

        j = i
        while j < n and not text[j].isspace():
            if text[j] == "/" and j + 1 < n and text[j + 1] in "*/":
                break
            j += 1
        if j == i:  # 防御：遇到孤立的 / 也要前进
            j = i + 1
        tokens.append(text[i:j])
        i = j

    return tokens


def parse_blocks(tokens: list[str]) -> Block:
    """把 token 流解析成块树。返回一个虚拟根块。

    用显式栈而不是递归，原因是 A2L 里 MODULE 可以嵌套多层
    （PROJECT → MODULE → FUNCTION → GROUP → ...），
    递归深度在超大文件上有风险。
    """
    root = Block(type="<root>")
    stack: list[Block] = [root]
    i = 0
    n = len(tokens)

    while i < n:
        token = tokens[i]
        if token == "/begin" and i + 1 < n:
            block = Block(type=tokens[i + 1])
            stack[-1].items.append(block)
            stack.append(block)
            i += 2
        elif token == "/end":
            if len(stack) > 1:
                stack.pop()
            i += 2  # 跳过 "/end" 与其后的类型名
        else:
            stack[-1].items.append(token)
            i += 1

    return root
