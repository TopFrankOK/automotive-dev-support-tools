"""存储基础设施。

    filesource.py  本地文件数据源（几乎所有解析器都经由它读文件）
    index.py       工件索引与元数据库（SQLite）      [待建]
    cache.py       按内容哈希缓存的解析结果          [待建]

为什么 `Artifact` 只存引用不存内容，这里给出落地答案：
原文件永远留在原地，元数据库里只放路径、内容哈希、解析耗时、
以及解析结果的**索引**（不是结果本身）。
这样 5 GB 的测量文件在数据库里也只是几十行记录。
"""

from automotive_dev_support.platform.store.filesource import FileSource, kind_from_suffix

__all__ = ["FileSource", "kind_from_suffix"]
