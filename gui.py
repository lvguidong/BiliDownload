#!/usr/bin/env python3
"""BiliDownload 界面快捷启动入口（浏览器界面）。

运行：
    python3 gui.py               # 启动并自动打开浏览器
    python3 gui.py --no-browser  # 只起服务，手动访问打印出的地址
    python3 gui.py --port 8899   # 指定端口

说明：早期版本用 tkinter，但 macOS 26 自带的 Tk 8.5.9 存在渲染缺陷
（Label/Entry/Text 与全部 ttk 控件不绘制），会呈现空白窗口。
因此界面改为内置 HTTP 服务 + 浏览器渲染，一定可见。
"""
import sys

from bili_downloader.webserver import main

if __name__ == "__main__":
    sys.exit(main())
