"""可插拔功能。

## 铁律

1. **插件只能 import `automotive_dev_support.core`。**
   不得 import platform / services / apps。由 .importlinter 契约 2 强制执行。

2. **插件之间禁止互相 import。**
   由契约 3 强制执行。需要共享类型时，把类型上提到 `core`
   （参见 `core/models/calibration/diff.py` 里 `ChangeSet` 的位置说明）。

3. **新增一个功能 = 新建一个文件夹。**
   里面至少要有：
       manifest.toml   声明能力与入口（注册表只读它）
       <impl>.py       实现 core.ports 里的某个协议
       ui/*.tsx        （可选）前端面板，前端自动挂载

   不需要改动内核的任何一行代码，也不需要在这个文件里登记。

## 已建

    parser_a2l         A2L 描述文件 → CalibrationModel        [parser]
    parser_cdfx        CDFX 值集文件 → ValueSet               [parser]
    diff_calibration   两个 CalibrationModel → ChangeSet       [analyzer]

## 计划中

    parser_hex / parser_elf / parser_dcm         内存镜像与值集
    align_symbol                                  跨版本符号对齐
    view_diff_table / view_map_curve              比对结果的界面
    export_excel_diff                             比对报告导出
    parser_mf4 / signal_compute / view_trend      数据分析
    gateway_can / gateway_doip / flash_recipe_*   刷写
    bridge_matlab                                 MATLAB 桥
    admin_license                                 授权管理（用自己写的工具管自己）
"""

from __future__ import annotations

__all__: list[str] = []
