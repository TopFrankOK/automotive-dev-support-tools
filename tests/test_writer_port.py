"""Writer 扩展点的测试。

**为什么它是一个独立扩展点，而不是塞进 Exporter：**

Exporter 产出的是**给人看的报告**，Writer 产出的是**要刷进 ECU 的文件**。
失败后果完全不同 —— 报告错了重导一遍就行，文件错了是数据事故。
这两种语义一旦混在一个协议里，迟早有人用错，而且错的时候看不出来。

所以这里要钉死的正是「分得开」：实现了 Exporter 的对象**不算** Writer。
"""

from __future__ import annotations

from automotive_dev_support.core.ports.plugin import Exporter, Parser, Writer
from automotive_dev_support.core.skeleton import ArtifactKind


class _FullWriter:
    writer_id = "demo"
    kinds = frozenset({ArtifactKind.HEX})

    def serialize(self, image: object, ctx: dict[str, object]) -> bytes:
        return b""


class _WithoutSerialize:
    writer_id = "demo"
    kinds = frozenset({ArtifactKind.HEX})


class _ExporterOnly:
    media_type = "text/plain"
    extension = ".txt"

    def export(self, payload: object, target: object, params: dict, ctx: object):
        return target


class _ParserOnly:
    kinds = frozenset({ArtifactKind.HEX})

    def parse(self, source: object, ctx: object):
        return None


class TestWriterProtocol:
    def test_full_implementation_is_a_writer(self):
        assert isinstance(_FullWriter(), Writer)

    def test_missing_serialize_is_not_a_writer(self):
        assert not isinstance(_WithoutSerialize(), Writer)

    def test_exporter_is_not_a_writer(self):
        """这一条是本协议存在的理由：两种语义不许混。"""
        assert isinstance(_ExporterOnly(), Exporter)
        assert not isinstance(_ExporterOnly(), Writer)

    def test_parser_is_not_a_writer(self):
        assert isinstance(_ParserOnly(), Parser)
        assert not isinstance(_ParserOnly(), Writer)

    def test_writer_is_not_an_exporter(self):
        assert isinstance(_FullWriter(), Writer)
        assert not isinstance(_FullWriter(), Exporter)
