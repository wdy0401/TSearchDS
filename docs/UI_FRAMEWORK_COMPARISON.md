# 界面选型与迁移

当前默认 Tkinter/ttk，入口 `python main.py`。它是 Python 标准界面库，
Tcl/Tk 使用宽松许可；发布二进制仍需保留 Python/Tcl/Tk 的版权和许可证。
无需购买 GUI 商业授权，也无需因该 GUI 库开放整个应用的源码。
项目仍保留现有 GPL-3.0-or-later 协议。

wxPython 有 wxWindows Library Licence 的二进制例外，也可用于此目的，
但增加安装和打包依赖。PySide6 有 LGPL 选项，仍需核对模块和履行 LGPL
义务；它作为可选开发后端保留，默认依赖和 EXE 均排除 Qt。

Tk 实现保留多标签、异步搜索、去重、资源数补充、六列（含来源）、
过滤排序、复制和格式转换、代理配置、中英文切换及设置保存。
18 项 Tk 行为测试通过；共享行为测试的 Tk 部分通过，Qt 部分因为本机
没有 PySide6 而跳过，因此不声称已经逐项实测两套后端完全等价。
Tk Treeview 原生交互与 Qt 的观感不同，不能承诺像素一致。

许可证减负只针对新版本的 GUI 依赖。mihomo 的 GPL、certifi 的 MPL、
其他库通知、搜索服务条款和内容权利仍分别适用。迁移不会消除历史发布
义务。详见 THIRD_PARTY.md 和 HISTORICAL_DISTRIBUTION.md。

来源：
- https://docs.python.org/3/library/tkinter.html
- https://github.com/tcltk/tk/blob/core-8-6-branch/license.terms
- https://wxpython.org/pages/license/
- https://www.qt.io/development/open-source-lgpl-obligations
