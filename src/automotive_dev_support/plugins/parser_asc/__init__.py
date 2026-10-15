"""插件入口。只做导出，不放逻辑 —— 逻辑在 parser.py。"""

from automotive_dev_support.plugins.parser_asc.parser import AscParser

__all__ = ["AscParser"]
