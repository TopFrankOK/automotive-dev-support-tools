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
    parser_hex         Intel HEX 镜像 → AddressSpace          [parser]
    parser_s19         Motorola S-Record 镜像 → AddressSpace  [parser]
    parser_asc         Vector ASC 日志 → 测量事件流           [parser]
    parser_blf         Vector BLF 二进制日志 → 事件流         [parser]
    parser_mf4         MDF4 块链索引 → 连续组（懒加载）       [parser]
    diff_calibration   两个 CalibrationModel → ChangeSet       [analyzer]
    export_excel_diff  ChangeSet → Excel 报告                  [exporter]
    gateway_sim        仿真网关：脚本化字节通道（无硬件）      [gateway]
    flash_recipe_sim   示例刷写配方：语义步骤 → 请求字节       [recipe]

## 计划中

    parser_elf / parser_dcm         内存镜像与值集
    align_symbol                    跨版本符号对齐
    view_diff_table / view_map_curve 比对结果的界面
    signal_compute / view_trend      测量数据分析与视图
    gateway_can / gateway_doip       真实总线网关（接口已就绪，加插件即可）
    flash_recipe_<ecu>               各 ECU 的真实配方
    bridge_matlab                                 MATLAB 桥
    admin_license                                 授权管理（用自己写的工具管自己）
"""

from __future__ import annotations

__all__: list[str] = []
