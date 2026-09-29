#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_exe.py —— 一键把本项目打包成 exe（onedir 模式）。

用法（在 base_env 里）：
  C:/ProgramData/anaconda3/envs/base_env/python.exe build_exe.py

产物：release/BiliSummary/
  ├ BiliSummary.exe          ← 主程序（图形界面）
  ├ summary_schema.json      ← 总结格式契约（放在 exe 旁边，**可直接编辑**）
  └ _internal/               ← 运行时（含 ffmpeg.exe、playwright 驱动等，不用动）

为什么是 onedir 而不是单文件：playwright 的 node 驱动有几十 MB，
单文件模式每次启动都要解压一遍，启动会慢好几秒；onedir 只有第一次复制慢。

对目标机器的要求：
  · Windows 10/11（自带 Edge —— 自动化用的是真实 Edge 内核，channel="msedge"）
  · 无需安装 Python / playwright / ffmpeg（都已捆绑或使用系统 Edge）
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "BiliSummary4"
# conda 环境：tcl/tk 的 DLL 和 OpenSSL 都在 <env>\Library\bin，PyInstaller 的钩子
# 在 conda 布局下会漏收（实测缺 tcl86t.dll/tk86t.dll → _tkinter ImportError；
# 错收 cryptography 的 libcrypto → https 全崩），下面显式补/修。
ENV_BIN = os.path.join(os.path.dirname(sys.executable), "Library", "bin")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",                 # GUI 程序，不弹黑框
    "--name", NAME,
    # 产物放全新的 release\ 目录（COLLECT 若目标已存在会先删它 —— 会被安全策略拦，
    # 所以这里避开已存在的 dist\，且不用 --clean 以免触发删除）
    "--distpath", os.path.join(HERE, "release"),
    "--workpath", os.path.join(HERE, "build_exe_tmp4"),
    "--specpath", os.path.join(HERE, "build_exe_tmp4"),
    # playwright 的 node 驱动与包数据（hooks-contrib 也自带 hook，这里求稳全收）
    "--collect-all", "playwright",
    # conda 布局下 _tkinter 依赖的 Tcl/Tk DLL（PyInstaller 只收了数据目录没收 DLL）
    "--add-binary", os.path.join(ENV_BIN, "tcl86t.dll") + ";.",
    "--add-binary", os.path.join(ENV_BIN, "tk86t.dll") + ";.",
    "--add-binary", os.path.join(ENV_BIN, "zlib.dll") + ";.",
    # 精简版 ffmpeg 边车 → _internal 根目录（bili_dl.find_ffmpeg 按 __file__ 同目录找它）
    "--add-binary", os.path.join(HERE, "ffmpeg.exe") + ";.",
    # 总结格式契约 → _internal（exe 旁边还会另放一份可编辑的，APP_DIR 优先）
    "--add-data", os.path.join(HERE, "summary_schema.json") + ";.",
    os.path.join(HERE, "bili_entry.py"),
]

print("[1/3] 运行 PyInstaller（首次要收集 playwright，约 2-5 分钟）…")

# PyInstaller 的 COLLECT 会先删掉已存在的输出目录 —— 删除会被安全策略拦。
# 改成把旧目录改名让路（不删），构建完再把里面的数据文件（summaries/history/…）搬回来。
out = os.path.join(HERE, "release", NAME)
prev_dir = None
if os.path.isdir(out):
    prev_dir = os.path.join(HERE, "release", "_old_%s_%s" % (NAME, time.strftime("%H%M%S")))
    os.rename(out, prev_dir)
    print("  旧目录已让路 →", os.path.basename(prev_dir), "（确认新版没问题后可手动删除）")

p = subprocess.run(cmd, cwd=HERE)
if p.returncode != 0:
    if prev_dir:      # 构建失败就把旧目录还原回去
        os.rename(prev_dir, out)
        print("  构建失败，已还原旧目录")
    raise SystemExit(f"PyInstaller 失败（exit {p.returncode}）")

out = os.path.join(HERE, "release", NAME)

print("[2/3] 后处理：修正 OpenSSL DLL + 复制可编辑的 schema…")
inner = os.path.join(out, "_internal")

# PyInstaller 会把 cryptography 包自带的 libcrypto/libssl（版本与 _ssl.pyd 构建所用的
# OpenSSL 不一致）收进 _internal，导致 frozen 模式下所有 https 请求在证书解析时
# 崩掉（ASN1: NOT_ENOUGH_DATA）。用 base_env Library/bin 里与 _ssl.pyd 配套的版本覆盖。
conda_lib = ENV_BIN
for dll in ("libcrypto-3-x64.dll", "libssl-3-x64.dll"):
    src = os.path.join(conda_lib, dll)
    dst = os.path.join(inner, dll)
    if os.path.isfile(src) and os.path.isfile(dst):
        if os.path.getsize(src) != os.path.getsize(dst):
            shutil.copy2(src, dst)
            print(f"  已覆盖 {dll}（{os.path.getsize(dst)/1e6:.1f} MB ← 源 {os.path.getsize(src)/1e6:.1f} MB）")
        else:
            print(f"  {dll} 已是配套版本，跳过")
    elif not os.path.isfile(src):
        print(f"  [警告] 没找到 {src}，请手动核对 OpenSSL DLL")

# summary_schema.json 放 exe 旁边（可编辑；读取时 APP_DIR 优先于 _MEIPASS）
shutil.copy2(os.path.join(HERE, "summary_schema.json"), os.path.join(out, "summary_schema.json"))

# 把旧目录里的**用户数据**搬回来（summaries/history/settings/covers —— 程序文件用新构建的）
if prev_dir:
    import glob
    os.makedirs(os.path.join(out, "summaries"), exist_ok=True)
    os.makedirs(os.path.join(out, "covers"), exist_ok=True)
    for f in glob.glob(os.path.join(prev_dir, "summaries", "*")):
        shutil.copy2(f, os.path.join(out, "summaries", os.path.basename(f)))
    for name in ("history.json", "settings.json"):
        src = os.path.join(prev_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out, name))
    for f in glob.glob(os.path.join(prev_dir, "covers", "*")):
        shutil.copy2(f, os.path.join(out, "covers", os.path.basename(f)))
    print("  已从旧目录恢复用户数据（summaries / history.json / settings.json / covers）")

print("[3/3] 体积统计…")
def du(path):
    total = 0
    for dp, _d, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total
print(f"  {out}  共 {du(out)/1024/1024:.0f} MB")
for f in ("BiliSummary.exe", "summary_schema.json"):
    p2 = os.path.join(out, f)
    if os.path.isfile(p2):
        print(f"  {f:<24} {os.path.getsize(p2)/1024/1024:.1f} MB")
print("\n完成。发给别人时把整个 release/BiliSummary 文件夹打包压缩即可。")
