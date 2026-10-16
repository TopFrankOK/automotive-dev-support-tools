"""值 → 字节（写回）的测试。

`bind_image()` 是「从镜像读值」，`apply_values()` 是它的反向。
两者必须**严格对称**，否则往返就不是无损的：
读的时候按大端 UINT16 读出来 850，写回去却成了别的字节，
这种错误不会报错，只会让某个标定量的值悄悄变掉。

另一条同样要紧：**只改被改动的字节，其它一个字节都不许动。**
"""

from __future__ import annotations

import struct

import pytest

from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    Characteristic,
    CharacteristicType,
    DataType,
    bind_image,
)
from automotive_dev_support.core.models.calibration.values import apply_values
from automotive_dev_support.core.models.common import SymbolPath
from automotive_dev_support.core.models.memory import AddressSpace, ImageAssembler


def _image(address: int, blob: bytes) -> AddressSpace:
    asm = ImageAssembler()
    asm.add(address, blob)
    return asm.build()


def _model(
    name: str = "V",
    *,
    data_type: DataType = DataType.UINT16,
    address: int | None = 0x1000,
    count: int = 1,
    values: list[float] | None = None,
) -> CalibrationModel:
    """构造单量模型。

    `element_count` 是**派生属性**（VALUE→1、VAL_BLK→array_size、其余→dims 乘积），
    不能当构造参数传。多元素的量要走 VAL_BLK + array_size。
    """
    shape: dict[str, object] = {}
    if count > 1:
        shape = {"type": CharacteristicType.VAL_BLK, "array_size": count}
    return CalibrationModel(
        characteristics={
            name: Characteristic(
                path=SymbolPath.parse(name),
                data_type=data_type,
                address=address,
                raw_values=values,
                **shape,  # type: ignore[arg-type]
            )
        }
    )


class TestRoundTrip:
    def test_uint16_big_endian(self):
        image = _image(0x1000, (850).to_bytes(2, "big"))
        bound, binding = bind_image(_model(), image)
        assert binding.matched == ["V"]
        assert bound.get("V").raw_values == [850.0]

        bound.get("V").raw_values = [900.0]
        result = apply_values(bound, image)
        assert result.written == ["V"]
        assert image.read(0x1000, 2) == (900).to_bytes(2, "big")
        # 再读一次，值必须回来
        reread, _ = bind_image(_model(), image)
        assert reread.get("V").raw_values == [900.0]

    def test_other_bytes_are_untouched(self):
        """只改目标区间，其余逐字节搬运 —— 无损往返的全部含义。"""
        blob = bytes(range(32))
        image = _image(0x1000, blob)
        bound, _ = bind_image(_model(), image)
        before = image.read(0x1000, 32)

        bound.get("V").raw_values = [0x1234]
        apply_values(bound, image)

        after = image.read(0x1000, 32)
        assert after[:2] == b"\x12\x34"
        assert after[2:] == before[2:], "目标区间以外的字节被改动了"

    def test_little_endian(self):
        image = _image(0x1000, (850).to_bytes(2, "little"))
        bound, _ = bind_image(_model(), image, byte_order="little")
        assert bound.get("V").raw_values == [850.0]

        bound.get("V").raw_values = [900.0]
        apply_values(bound, image, byte_order="little")
        assert image.read(0x1000, 2) == (900).to_bytes(2, "little")

    def test_float32(self):
        image = _image(0x2000, struct.pack(">f", 3.5))
        bound, _ = bind_image(_model(data_type=DataType.FLOAT32, address=0x2000), image)
        assert bound.get("V").raw_values == [pytest.approx(3.5)]

        bound.get("V").raw_values = [4.5]
        apply_values(bound, image)
        reread, _ = bind_image(_model(data_type=DataType.FLOAT32, address=0x2000), image)
        assert reread.get("V").raw_values == [pytest.approx(4.5)]

    def test_ascii(self):
        image = _image(0x1000, b"ABC")
        model = _model(data_type=DataType.ASCII, count=3)
        bound, _ = bind_image(model, image)
        assert bound.get("V").raw_values == [65.0, 66.0, 67.0]

        bound.get("V").raw_values = [88.0, 89.0, 90.0]
        apply_values(bound, image)
        assert image.read(0x1000, 3) == b"XYZ"

    def test_array(self):
        image = _image(0x1000, struct.pack(">3H", 1, 2, 3))
        model = _model(count=3)
        bound, _ = bind_image(model, image)
        assert bound.get("V").raw_values == [1.0, 2.0, 3.0]

        bound.get("V").raw_values = [10.0, 20.0, 30.0]
        apply_values(bound, image)
        assert image.read(0x1000, 6) == struct.pack(">3H", 10, 20, 30)


class TestSkipped:
    def test_without_address(self):
        image = _image(0x1000, b"\x01\x02")
        model = _model(address=None)
        result = apply_values(model, image)
        assert "V" in result.skipped
        assert "地址" in result.skipped["V"]
        assert image.read(0x1000, 2) == b"\x01\x02", "跳过的量不许改动镜像"

    def test_uncovered_address(self):
        """镜像没覆盖就写 = 发明字节，必须报出来。"""
        image = _image(0x1000, b"\x01\x02")
        model = _model(address=0x9000, values=[7.0])
        result = apply_values(model, image)
        assert "V" in result.skipped
        assert "不在镜像内" in result.skipped["V"]

    def test_value_count_mismatch(self):
        image = _image(0x1000, b"\x01\x02")
        model = _model()
        bound, _ = bind_image(model, image)
        bound.get("V").raw_values = [1.0, 2.0]  # 需要 1 个，给了 2 个
        result = apply_values(bound, image)
        assert "V" in result.skipped
        assert "1" in result.skipped["V"] and "2" in result.skipped["V"]

    def test_no_values_means_skip(self):
        image = _image(0x1000, b"\x01\x02")
        result = apply_values(_model(), image)
        assert "V" in result.skipped

    def test_non_integer_value_is_refused(self):
        """整数类型收到 1.5 —— 截断成 1 会让值悄悄变掉，必须报。"""
        image = _image(0x1000, b"\x01\x02")
        bound, _ = bind_image(_model(), image)
        bound.get("V").raw_values = [1.5]
        result = apply_values(bound, image)
        assert "V" in result.skipped
        assert image.read(0x1000, 2) == b"\x01\x02"


class TestResult:
    def test_written_and_skipped_are_both_reported(self):
        image = _image(0x1000, b"\x01\x02" * 2)
        model = CalibrationModel(
            characteristics={
                "Ok": Characteristic(
                    path=SymbolPath.parse("Ok"),
                    data_type=DataType.UINT16,
                    address=0x1000,
                    element_count=1,
                    raw_values=[7.0],
                ),
                "Bad": Characteristic(
                    path=SymbolPath.parse("Bad"),
                    data_type=DataType.UINT16,
                    address=0x9000,
                    element_count=1,
                    raw_values=[7.0],
                ),
            }
        )
        result = apply_values(model, image)
        assert result.written == ["Ok"]
        assert "Bad" in result.skipped

    def test_skip_reason_names_the_symbol(self):
        """逐个报出 + 带原因。只报一个总数等于没报。"""
        image = _image(0x1000, b"\x01\x02")
        result = apply_values(_model(address=None), image)
        assert result.skipped == {"V": result.skipped["V"]}
        assert result.skipped["V"]
