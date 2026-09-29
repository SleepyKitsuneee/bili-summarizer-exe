#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_entry.py —— 打包入口（PyInstaller 以此文件为入口构建 exe）。

职责只有一个：区分 exe 的两种身份，再转发到对应实现。

  1. `bili.exe --bili-dl-worker <bili_dl 的参数…>`
     → bili_qwen_summary.download() 在打包模式下没有独立 Python 和 bili_dl.py
       源文件可用，就让 exe **重入自身**，把参数原样转给 bili_dl.main()。
       （必须在 import bili_ui 之前判断 —— bili_ui 一进来就拉起 playwright，
        worker 不需要那些，白白多花几秒启动。）

  2. 其余情况 → 正常启动图形界面（bili_ui.main()）。
"""
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--bili-dl-worker":
        # 保证 bili_dl 能作为普通模块被找到（onedir 下与入口同目录）
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        # PyInstaller --windowed 会把 sys.stdout/stderr 换成空写入器，
        # 而父进程靠解析本进程的 stdout 拿「输出：路径 / 已存在」等关键信息。
        # fd 1/2 在被父进程以管道拉起时是有效的，这里重建真实的 TextIOWrapper。
        import os as _os
        for _fd in (1, 2):
            try:
                _f = _os.fdopen(_fd, "w", encoding="utf-8", errors="replace",
                                buffering=1, closefd=False)
                if _fd == 1:
                    sys.stdout = _f
                else:
                    sys.stderr = _f
            except OSError:
                pass
        sys.argv = [sys.argv[0]] + argv[1:]
        import bili_dl
        return bili_dl.main()
    import bili_ui
    bili_ui.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
