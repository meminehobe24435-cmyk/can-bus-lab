# -*- coding: utf-8 -*-
"""can-bus-lab：纯标准库实现的 CAN 总线仿真与类 CAPL 脚本化测试框架。

子模块
------
* :mod:`canlab.frame`  —— 位级帧编解码、位填充、CRC15/17/21、DLC 映射
* :mod:`canlab.bus`    —— 多节点总线与**逐位非破坏性仲裁**、统计
* :mod:`canlab.errors` —— 五类错误检测、TEC/REC 计数与错误状态机
* :mod:`canlab.isotp`  —— ISO 15765-2 传输层（SF/FF/CF/FC、BS/STmin、六个超时）
* :mod:`canlab.uds`    —— ISO 14229-1 诊断服务子集（服务端 + 客户端）
* :mod:`canlab.capl`   —— 自研类 CAPL 脚本引擎（词法 / 语法 / 解释器 / 虚拟时间）
* :mod:`canlab.report` —— JUnit XML 与文本测试报告
* :mod:`canlab.cli`    —— 命令行入口

**定位**：教学 + 自研仿真 + 脚本化测试框架。本项目不是 CANoe 或 TSMaster
(同星) 的替代品，也没有使用过这两款工具；没有真实 CAN 硬件，总线行为全部
是软件逐位仿真。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
