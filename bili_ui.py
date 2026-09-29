#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B站视频总结 —— 图形界面（tkinter，纯标准库 + 已有 Pillow）

布局：
  ┌───────────────────────────────────────────────────────────────┐
  │ 视频链接 [ ................................ ] [ 开始总结 ] [日志]│
  │ 账号：xxx ✅ [登录/重新登录]   模型 [Qwen3.8-Omni-Flash ▾] [刷新]│
  ├──────────────────┬────────────────────────────────────────────┤
  │ 历史记录 (N)     │  ┌ 结构化视图 ┬ 原始 JSON ┐                 │
  │ ┌──┬───────────┐ │  │                                        │
  │ │图│ 视频标题  │ │  │        视频总结（大界面）               │
  │ ├──┼───────────┤ │  │                                        │
  │ │图│ 视频标题  │ │  │                                        │
  │ └──┴───────────┘ │  └────────────────────────────────────────┘
  │   （每行一条记录， │  [保存 JSON][保存 Markdown][打开目录][清理] │
  │     点哪行看哪条） │                                            │
  ├──────────────────┴────────────────────────────────────────────┤
  │ 状态栏：最后一条进度信息                                        │
  └───────────────────────────────────────────────────────────────┘

登录入口：启动时会自动检测登录态；过期/首次使用时账号栏会标红，
         点「登录 / 重新登录」会打开一个**可见的 Edge 窗口**让你自己登录，
         程序不接触密码，登录成功自动识别。
模型入口：下拉里的模型是**从千问页面动态读取**的（不是写死的），
         千问换模型时点「刷新」即可，选择结果记在 settings.json。

历史记录存在 history.json；界面每次启动会自动扫描磁盘上已有的 *.总结.json 补齐列表。

目录分工：
  - **`tmp/`** —— 视频临时存放点（按画质分子目录，如 `tmp/360p`）。上传成功并取回总结后，
    视频本体和 .m4s 中间流会自动删除；失败时不清理，可直接重跑（会复用已下载的文件）。
  - **`summaries/`** —— 成果目录，存 `<视频名>.总结.json` / `.总结.md`，**不会被自动清理**。

启动：
  C:/ProgramData/anaconda3/envs/base_env/python.exe bili_ui.py
  C:/ProgramData/anaconda3/envs/base_env/python.exe bili_ui.py --auto "<链接>"
"""
import argparse
import hashlib
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.request
from types import SimpleNamespace

import tkinter as tk
import tkinter.font as tkFont
from tkinter import ttk, filedialog, messagebox

# frozen（exe）时：可写数据放 exe 旁边；只读资源在 _MEIPASS（_internal）
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = RES_DIR = os.path.dirname(os.path.abspath(__file__))
HERE = APP_DIR                      # 兼容旧引用
SCHEMA_FILE = os.path.join(APP_DIR, "summary_schema.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
COVER_DIR = os.path.join(APP_DIR, "covers")
FONT = "Microsoft YaHei UI"

# 视频临时存放点：**扁平**（不再按画质分目录）。
# 画质歧义由下载侧的 `<名>.下载信息.json` 标记解决：复用时画质不符会自动 --force 重下。
TMPROOT = os.path.join(HERE, "tmp")
OUTDIR = TMPROOT
# 默认画质/音频档位（用户要求固定 360P+64K；改这两行即可全局调整）
QUALITY = "360P"
AUDIO_Q = "64K"

from bili_dl import BiliError, Client, fetch_info, parse_bvid, load_sessdata  # noqa: E402
from bili_qwen_summary import (add_log_hook, remove_log_hook, download,  # noqa: E402
                               ask_qwen, cleanup_video, SUMMARIES_DIR,
                               check_login, open_login_window, list_models,
                               run_selfcheck, TMP_DIR,
                               FALLBACK_MODEL, load_schema, build_prompt,
                               DEFAULT_PREAMBLE)  # 成果目录：summaries/


# ══════════════════════ 纯逻辑（可单独测试，不依赖窗口） ══════════════════════


def parse_json_answer(text: str):
    """从模型回答里抠出 JSON（容忍 ```json 包裹和尾随逗号）。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None, "模型回答里找不到 JSON 对象"
    raw = t[i:j + 1]
    try:
        return json.loads(raw), None
    except Exception as e:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", raw)), None
        except Exception as e2:
            return None, f"{e} / 补尾随逗号后仍失败: {e2}"


def fmt_duration(sec) -> str:
    try:
        sec = int(sec)
    except Exception:
        return str(sec or "")
    m, s = divmod(sec, 60)
    return f"{m}分{s}秒" if m else f"{s}秒"


def get_video_info(url: str) -> dict:
    bvid = parse_bvid(url)
    client = Client(load_sessdata(SimpleNamespace(sessdata=None, from_app_config=True)))
    info = fetch_info(client, bvid)
    return {
        "bvid": bvid,
        "title": info.get("title") or "",
        "cover": info.get("pic") or "",
        "up": ((info.get("owner") or {}).get("name") or ""),
        "duration": fmt_duration(info.get("duration")),
    }


def fetch_cover_bytes(url: str):
    """B站图床有防盗链，必须带 Referer。"""
    if not url:
        return None
    try:
        c = Client()
        with c.open(url, with_cookie=False,
                    extra_headers={"Referer": "https://www.bilibili.com/"}) as r:
            return r.read()
    except Exception:
        try:
            return urllib.request.urlopen(url, timeout=10).read()
        except Exception:
            return None


def apply_authoritative_meta(data: dict, info: dict) -> dict:
    """把标题/UP主/时长换成本地接口取到的权威值。

    模型听写容易错（实测把 UP主「沫咨」听成「某只」、视频标题留空），
    而这几项我们有准确来源（B站 view 接口），以接口为准更可靠。
    """
    if not isinstance(data, dict) or not info:
        return data
    if info.get("title"):
        data["视频标题"] = info["title"]
    if info.get("up"):
        data["UP主"] = info["up"]
    if info.get("duration"):
        data["时长"] = info["duration"]
    return data


def summary_rows(data: dict) -> list:
    """把 JSON 摊平成 (层级, 文本) 列表，供界面按层级加样式。

    新格式（summary_schema.json）：开头就是「结论」；「关键数据」「亮点」已移除，
    但仍兼容渲染旧记录里的这两个字段。
    """
    rows = []
    if not isinstance(data, dict):
        return [("body", str(data))]

    # 顶部结论：新格式用「结论」，旧记录回退「一句话总结」
    top = data.get("结论") or data.get("一句话总结")
    if top:
        rows.append(("quote", f"结论：{top}"))

    head = []
    for k in ("内容类型", "时长", "UP主", "视频标题"):
        if data.get(k):
            head.append(f"{k}：{data[k]}")
    if head:
        rows.append(("meta", "　｜　".join(head)))

    def section(title, items, numbered=False):
        items = [x for x in (items or []) if str(x).strip()]
        if not items:
            return
        rows.append(("h2", title))
        for n, x in enumerate(items, 1):
            rows.append(("body", f"{n}. {x}" if numbered else f"• {x}"))

    section("核心要点", data.get("核心要点"), numbered=False)

    section("争议或不足", data.get("争议或不足"))

    segs = [s for s in (data.get("分段摘要") or []) if isinstance(s, dict)]
    if segs:
        rows.append(("h2", "分段摘要"))
        for s in segs:
            rows.append(("seg", f"{s.get('时间点','')}　{s.get('主题','')}"))
            if s.get("要点"):
                rows.append(("body", f"　　{s['要点']}"))

    # 兼容旧记录：这两个字段已从新格式移除，但旧 JSON 里还有就照常显示
    section("关键数据", data.get("关键数据"))
    section("亮点", data.get("亮点"))

    kws = [x for x in (data.get("关键词") or []) if str(x).strip()]
    if kws:
        rows.append(("h2", "关键词"))
        rows.append(("body", "　".join(f"#{k}" for k in kws)))

    known = {"一句话总结", "视频标题", "UP主", "时长", "内容类型", "核心要点",
             "分段摘要", "关键数据", "亮点", "争议或不足", "结论", "适合谁看", "关键词"}
    for k, v in data.items():
        if k in known:
            continue
        rows.append(("h2", str(k)))
        rows.append(("body", str(v)))
    return rows


# ══════════════════════ 历史记录 ══════════════════════

def _rel(p: str) -> str:
    try:
        return os.path.relpath(p, HERE).replace("\\", "/")
    except Exception:
        return p


def _abs(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(HERE, p)


SETTINGS_FILE = os.path.join(HERE, "settings.json")


def load_settings() -> dict:
    """界面设置（目前只有模型选择）。文件不存在或有损坏都能安全兜底。"""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_settings(d: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_history() -> list:
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            recs = json.load(f)
        if isinstance(recs, list):
            return [r for r in recs if isinstance(r, dict) and r.get("json")]
    except Exception:
        pass
    return []


def save_history(recs: list):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def scan_existing_summaries(recs: list) -> list:
    """扫描磁盘上已有的 *.总结.json，把索引里没有的补进来（无封面，标题取文件内的字段）。

    扫两个地方：成果目录 summaries/（正常来源）与 tmp/（兼容早期版本留在临时目录里的）。
    """
    known = {r.get("json") for r in recs}
    found = []
    for root in (SUMMARIES_DIR, TMPROOT):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".总结.json"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = _rel(full)
                if rel in known or any(f["json"] == rel for f in found):
                    continue
                title = fn[:-len(".总结.json")]
                up, dur = "", ""
                try:
                    with open(full, encoding="utf-8") as f:
                        d = json.load(f)
                    title = d.get("视频标题") or title
                    up = d.get("UP主") or ""
                    dur = d.get("时长") or ""
                except Exception:
                    pass
                # 视频可能被自动清理了，找不到就把 mp4 留空
                base = fn[:-len(".总结.json")]
                mp4 = ""
                cand = os.path.join(TMPROOT, base + ".mp4")
                if os.path.isfile(cand):
                    mp4 = _rel(cand)
                found.append({
                    "bvid": "", "title": title, "up": up, "duration": dur,
                    "cover": "", "ts": int(os.path.getmtime(full)),
                    "json": rel,
                    "md": rel[:-len(".json")] + ".md",
                    "mp4": mp4,
                })
    recs.extend(found)
    return recs


def cover_cache_path(rec: dict) -> str:
    key = rec.get("bvid") or hashlib.md5(
        (rec.get("title") or "x").encode("utf-8")).hexdigest()[:16]
    return os.path.join(COVER_DIR, re.sub(r"[^\w.-]", "_", key) + ".jpg")


def collect_tmp_victims(root: str) -> list:
    """列出 tmp 下可清理的文件：视频与 .m4s 中间流。

    排除：`.总结.json` / `.总结.md`（成果）、以及 `旧GUI下载/`（用户手动挪进来的旧文件）。
    """
    victims = []
    if not os.path.isdir(root):
        return victims
    for dp, dirs, fs in os.walk(root):
        if os.path.basename(dp) == "旧GUI下载":
            dirs[:] = []
            continue
        for f in fs:
            if ".总结." in f:
                continue
            if f.lower().endswith(".mp4") or ".m4s" in f:
                victims.append(os.path.join(dp, f))
    return sorted(victims)


# ══════════════════════════════ 界面 ══════════════════════════════

# ── iOS 风格配色（tkinter 没有真正的毛玻璃/亚克力合成，用扁平卡片 + 柔和色近似）──
BG        = "#f2f3f7"     # 窗口底色
CARD      = "#ffffff"     # 卡片 / 行底
ROW_HOVER = "#eef1f7"     # 行悬停
ROW_SEL   = "#e3eeff"     # 行选中
ROW_BAD   = "#fdecec"     # 失败行
TEXT      = "#1c1c1e"     # 主文字
SUB       = "#8e8e93"     # 次要文字
ACCENT    = "#0a84ff"     # 主色（iOS 蓝）
ACCENT_DK = "#0069d9"
OK_C      = "#34c759"
DANGER    = "#ff3b30"
LINE      = "#e3e5ec"     # 分隔线


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in rgb)


def _lerp_hex(a, b, t):
    """两种颜色按 t∈[0,1] 插值 —— 悬停/脉冲动画的基础。"""
    ca, cb = _hex_rgb(a), _hex_rgb(b)
    return _rgb_hex(tuple(x + (y - x) * t for x, y in zip(ca, cb)))


def _fade_bg(widgets, target, steps=6, delay=22, token_box=None):
    """把一组 widget 的背景渐变到 target（小动画）。token_box 用来取消上一次未完成的动画。"""
    if token_box is not None and token_box.get("id"):
        try:
            widgets[0].after_cancel(token_box["id"])
        except Exception:
            pass
    start = widgets[0].cget("background")
    state = {"i": 0}

    def tick():
        state["i"] += 1
        t = state["i"] / steps
        c = _lerp_hex(start, target, min(1.0, t))
        for w in widgets:
            try:
                w.configure(background=c)
            except Exception:
                pass
        if state["i"] < steps:
            if token_box is not None:
                token_box["id"] = widgets[0].after(delay, tick)
    tick()
    if token_box is not None:
        token_box["id"] = None


class PillButton(tk.Canvas):
    """iOS 风格胶囊按钮（Canvas 自绘）：悬停变色动画、按下变深、禁用变灰。

    对外保持 ttk.Button 的常用接口：configure(state=...)、invoke()，方便测试与旧代码。
    """

    def __init__(self, master, text, command, accent=True,
                 font=None, padx=18, pady=8, radius=None, bg=None):
        self._parent = master
        self._cmd = command
        self._text = text
        self._state = "normal"
        self._accent = accent
        self._font = font or (FONT, 10, "bold")
        self._base = ACCENT if accent else CARD
        self._fg = "#ffffff" if accent else TEXT
        f = tkFont.Font(font=self._font)
        w = f.measure(text) + padx * 2
        h = f.metrics("linespace") + pady * 2
        self._r = radius if radius is not None else h // 2
        if bg is None:
            try:
                bg = master.cget("background")
            except Exception:
                bg = BG
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bd=0, bg=bg)
        self._body = self._round_rect(1, 1, w - 2, h - 2, self._r, fill=self._base, outline="")
        self._label = self.create_text(w // 2, h // 2, text=text,
                                       fill=self._fg, font=self._font)
        for seq in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>"):
            self.bind(seq, self._on_evt)
        self._anim = None

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _on_evt(self, e):
        if self._state != "normal":
            return None
        if e.type == "7":                      # Enter
            self._animate(ACCENT_DK if self._accent else ROW_HOVER)
        elif e.type == "8":                    # Leave
            self._animate(self._base)
        elif e.type == "4":                    # Press
            self._animate(_lerp_hex(ACCENT_DK if self._accent else ROW_HOVER,
                                    "#000000", 0.15))
        elif e.type == "5":                    # Release
            self.invoke()
        return None

    def _animate(self, target, steps=5, delay=18):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
        state = {"i": 0}

        def tick():
            state["i"] += 1
            c = _lerp_hex(self.itemcget(self._body, "fill"), target, state["i"] / steps)
            self.itemconfigure(self._body, fill=c)
            if state["i"] < steps:
                self._anim = self.after(delay, tick)
        tick()

    def configure(self, **kw):                 # noqa: A003
        if "state" in kw:
            self._state = kw.pop("state")
            if self._state == "disabled":
                dis = _lerp_hex(self._base, "#d8dae2" if not self._accent else "#b7d4fb", 0.85)
                self.itemconfigure(self._body, fill=dis)
                self.itemconfigure(self._label, fill="#ffffff" if self._accent else "#9a9aa3")
            else:
                self.itemconfigure(self._body, fill=self._base)
                self.itemconfigure(self._label, fill=self._fg)
        if "text" in kw:
            self._text = kw.pop("text")
            self.itemconfigure(self._label, text=self._text)
        for k, v in kw.items():
            try:
                super().configure(**{k: v})
            except Exception:
                pass

    def state(self, flags=None):               # noqa: A003 —— ttk.Button.state 兼容壳
        return ()

    def invoke(self):                          # noqa: A003
        if self._state == "normal" and self._cmd:
            return self._cmd()


class StepView(ttk.Frame):
    """右侧「进度」页签：步骤卡片 + 旋转加载动画 + 当前步骤实时更新。"""

    STEPS = ["读取视频信息", "下载视频", "上传并解析", "AI 生成", "保存清理"]

    def __init__(self, master):
        super().__init__(master, padding=(18, 14))
        self.title_var = tk.StringVar(value="—")
        ttk.Label(self, textvariable=self.title_var, font=(FONT, 14, "bold"),
                  foreground=TEXT).pack(anchor="w")
        ttk.Label(self, text="实时进度", font=(FONT, 9), foreground=SUB).pack(
            anchor="w", pady=(2, 10))
        self.bar = ttk.Progressbar(self, mode="indeterminate", maximum=100)
        self.bar.pack(fill="x", pady=(0, 14))
        self.canvas = tk.Canvas(self, height=len(self.STEPS) * 56 + 16,
                                highlightthickness=0, background=CARD)
        self.canvas.pack(fill="both", expand=True)
        self.state = {name: ("pending", "") for name in self.STEPS}
        self.error = ""
        self._angle = 0
        self._anim = None
        self.canvas.bind("<Configure>", lambda e: self.redraw())

    def start(self, title):
        self.title_var.set(title or "—")
        self.error = ""
        self.state = {name: ("pending", "") for name in self.STEPS}
        self.bar.configure(mode="indeterminate")
        self.bar.start(14)
        if not self._anim:
            self._tick()

    def set_state(self, name, st, detail=""):
        if name in self.state:
            self.state[name] = (st, detail)
        self.redraw()

    def set_detail(self, name, detail):
        if name in self.state:
            cur = self.state[name][0]
            self.state[name] = (cur, detail)
        self.redraw()

    def fail(self, detail):
        self.bar.stop()
        for name, (st, _d) in self.state.items():
            if st == "running":
                self.state[name] = ("failed", detail)
                break
        self.redraw()

    def finish(self):
        self.bar.stop()
        for name, (st, d) in self.state.items():
            if st in ("pending", "running"):
                self.state[name] = ("done", d)
        if self._anim:
            self.after_cancel(self._anim)
            self._anim = None
        self.redraw()

    def _tick(self):
        self._angle = (self._angle + 14) % 360
        self.redraw()
        self._anim = self.after(60, self._tick)

    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        w = max(cv.winfo_width(), 420)
        for i, name in enumerate(self.STEPS):
            st, detail = self.state.get(name, ("pending", ""))
            y = 22 + i * 56
            cx, r = 34, 13
            if st == "done":
                cv.create_oval(cx - r, y - r, cx + r, y + r, fill=OK_C, outline="")
                cv.create_text(cx, y, text="✓", fill="#ffffff", font=(FONT, 11, "bold"))
            elif st == "running":
                cv.create_oval(cx - r, y - r, cx + r, y + r, outline=LINE, width=2)
                cv.create_arc(cx - r, y - r, cx + r, y + r, start=self._angle, extent=270,
                              style="arc", outline=ACCENT, width=3)
            elif st == "failed":
                cv.create_oval(cx - r, y - r, cx + r, y + r, fill=DANGER, outline="")
                cv.create_text(cx, y, text="✕", fill="#ffffff", font=(FONT, 11, "bold"))
            else:
                cv.create_oval(cx - r, y - r, cx + r, y + r, outline=LINE, width=2)
            if i < len(self.STEPS) - 1:
                cv.create_line(cx, y + r, cx, y + 56 - r, fill=LINE, width=2)
            active = st == "running"
            cv.create_text(60, y - (8 if detail else 0), text=name, anchor="w",
                           font=(FONT, 11, "bold" if active else "normal"),
                           fill=ACCENT if active else (DANGER if st == "failed"
                                                       else TEXT if st == "done" else SUB))
            if detail:
                cv.create_text(60, y + 12, text=detail[:64], anchor="w",
                               font=(FONT, 9), fill=SUB)
        if self.error:
            cv.create_text(60, len(self.STEPS) * 56 + 22, text=self.error[:80],
                           anchor="w", font=(FONT, 9), fill=DANGER)





class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.authing = False
        self.checking = False           # 环境自检进行中（也要占浏览器）
        self.pending_rec = None         # 正在处理的记录（出现在历史列表顶部，完成后转正）
        self.logs = []
        self.recs = scan_existing_summaries(load_history())
        save_history(self.recs)
        self.cur = None                 # 当前选中的历史记录
        self.rows = []                  # [(frame, rec, widgets...)]
        self.thumbs = {}                # 缩略图 PhotoImage 引用（不持有会被 GC）
        self._setup_style(root)

        root.title("B站视频 → 结构化总结")
        root.geometry("1280x760")
        root.minsize(1040, 640)
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1280, sw - 40), min(760, sh - 80)
        root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//3)}")

        self._build_top()
        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        left = ttk.Frame(body, padding=(0, 4))
        right = ttk.Frame(body, padding=(10, 4, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=4)
        self._body_pane = body
        self._build_history(left)
        self._build_summary(right)
        self._build_status()

        os.makedirs(COVER_DIR, exist_ok=True)
        self.refresh_history()
        # 布局比例：分界栏靠左（左栏窄一些，右栏总结区更大）
        try:
            body.update_idletasks()
            body.sashpos(0, 296)
        except Exception:
            pass
        self._log("启动中：正在检测登录态并读取可用模型…")
        threading.Thread(target=self._cover_worker, daemon=True).start()
        threading.Thread(target=self._boot_auth, daemon=True).start()
        self.root.after(120, self._poll)

    @staticmethod
    def _setup_style(root):
        """iOS 风格 ttk 主题：扁平卡片、无边框、柔和底色。"""
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        root.configure(background=BG)
        style.configure(".", background=BG, font=(FONT, 10), borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", background=CARD, foreground=TEXT, borderwidth=0,
                        focusthickness=0, padding=(12, 6))
        style.map("TButton",
                  background=[("active", ROW_HOVER), ("disabled", "#e4e6ee")],
                  foreground=[("disabled", "#9a9aa3")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        padding=(16, 7))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DK), ("disabled", "#b7d4fb")],
                  foreground=[("disabled", "#ffffff")])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=SUB,
                        padding=(18, 7), borderwidth=0, font=(FONT, 10))
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", ACCENT), ("active", TEXT)])
        style.configure("TEntry", fieldbackground=CARD, borderwidth=0, padding=7,
                        foreground=TEXT)
        style.configure("TCombobox", fieldbackground=CARD, arrowcolor=TEXT, padding=5)
        style.configure("TPanedwindow", background=BG)
        style.configure("TPanedWindow", background=BG)
        style.configure("Sash", sashthickness=6, background=BG)
        style.configure("TProgressbar", background=ACCENT, troughcolor="#e6e8ef",
                        borderwidth=0)
        style.configure("Vertical.TScrollbar", background="#d5d8e0",
                        troughcolor=BG, borderwidth=0, arrowcolor="#9a9aa3")
        style.configure("Horizontal.TScrollbar", background="#d5d8e0",
                        troughcolor=BG, borderwidth=0, arrowcolor="#9a9aa3")

    # ── 顶部 ──
    def _build_top(self):
        top = ttk.Frame(self.root, padding=(12, 12, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="视频链接", font=(FONT, 10)).pack(side="left")
        self.entry = ttk.Entry(top, font=(FONT, 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=10)
        self.entry.insert(0, "")
        self.entry.bind("<Return>", lambda e: self.start())
        self.entry_menu = self._attach_ctx_menu(self.entry, kind="entry")
        self.btn = PillButton(top, "开始总结", self.start)
        self.btn.pack(side="left", padx=(4, 0))
        ttk.Button(top, text="环境自检", command=self.do_selfcheck, width=10).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="日志", command=self.show_log, width=6).pack(side="left", padx=(6, 0))

        # 第二行：账号 / 登录入口 / 模型选择入口
        row2 = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        row2.pack(fill="x")
        self.acct_lb = ttk.Label(row2, text="账号：检测中…", font=(FONT, 9), foreground="#888")
        self.acct_lb.pack(side="left")
        self.login_btn = ttk.Button(row2, text="登录 / 重新登录", command=self.do_login, width=18)
        self.login_btn.pack(side="left", padx=(8, 0))

        ttk.Label(row2, text="模型", font=(FONT, 9)).pack(side="left", padx=(20, 4))
        st = load_settings()
        self.model_var = tk.StringVar(value=st.get("model") or FALLBACK_MODEL)
        # 下拉候选：上次成功读到的列表（settings.json）兜底 + 内置默认，
        # 这样就算某次读页面失败，用户也还能选/手输模型 id
        cached = list(st.get("models") or [])
        if FALLBACK_MODEL not in cached:
            cached = [FALLBACK_MODEL] + cached
        self.model_cb = ttk.Combobox(row2, textvariable=self.model_var, width=24,
                                     values=cached or [self.model_var.get()])
        self.model_cb.pack(side="left")
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self._on_model_change())
        self.model_cb.bind("<Return>", lambda e: self._on_model_change())
        self.model_cb.bind("<FocusOut>", lambda e: self._on_model_change())
        self.model_btn = ttk.Button(row2, text="刷新", width=6,
                                    command=lambda: self.refresh_models())
        self.model_btn.pack(side="left", padx=(6, 0))
        self.model_hint = ttk.Label(row2, text="(下拉自动从千问页面读取；也可手输模型 id)",
                                    font=(FONT, 8), foreground="#aaa")
        self.model_hint.pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="自定义提示词", command=self.edit_prompt, width=12).pack(side="left", padx=(8, 0))

    def edit_prompt(self):
        """编辑 JSON 提示词的**前置说明**（JSON 结构部分固定来自 summary_schema.json，不开放）。

        保存到 settings.json 的 prompt_preamble 字段；留空则用内置默认。
        """
        win = tk.Toplevel(self.root)
        win.title("自定义提示词（JSON 前置说明）")
        win.geometry("760x480")
        ttk.Label(win, padding=(10, 8, 10, 0), text=(
            "下面这段文字会拼在 JSON 结构**前面**发给模型——写你想让总结怎么写："
            "口径、字数、侧重、语气都行。\n"
            "JSON 结构本身固定来自 summary_schema.json（保证每条总结格式一致），在这里改不了；"
            "留空则恢复内置默认。"), foreground="#555", wraplength=720,
            justify="left").pack(fill="x")
        tx = tk.Text(win, wrap="word", font=(FONT, 10), padx=12, pady=10, height=16)
        sb = ttk.Scrollbar(win, command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        tx.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 10))
        cur = load_settings().get("prompt_preamble")
        tx.insert("1.0", (cur or DEFAULT_PREAMBLE).strip())
        self._attach_ctx_menu(tx, kind="text")

        def _save():
            text = tx.get("1.0", "end").strip()
            st = load_settings()
            if text and text != DEFAULT_PREAMBLE.strip():
                st["prompt_preamble"] = text
            else:
                st.pop("prompt_preamble", None)   # 与默认相同/为空 → 视为未自定义
            save_settings(st)
            self._log("提示词已保存" + ("（自定义）" if "prompt_preamble" in st else "（恢复内置默认）"))
            messagebox.showinfo("已保存", "提示词已保存，下一次总结就会生效。")
            win.destroy()

        def _reset():
            tx.delete("1.0", "end")
            tx.insert("1.0", DEFAULT_PREAMBLE)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="保存", command=_save, width=10).pack(side="left")
        ttk.Button(bar, text="恢复默认", command=_reset, width=10).pack(side="left", padx=6)
        ttk.Label(bar, text="JSON 结构在 summary_schema.json 里改", font=(FONT, 8),
                  foreground="#aaa").pack(side="right")

    def _select_all(self, widget, kind):
        if kind == "entry":
            widget.select_range(0, "end")
            widget.icursor("end")
        else:
            widget.tag_add("sel", "1.0", "end-1c")
            widget.see("1.0")
        return "break"

    def _attach_ctx_menu(self, widget, kind="entry"):
        """给控件挂右键菜单。

        tkinter 的 Entry/Text 在 Windows 上**没有原生右键菜单**（这是 Tk 的默认行为，
        不是被禁用了），所以右键会毫无反应。必须自己绑一个 Menu 并 tk_popup 弹出来。
        """
        m = tk.Menu(widget, tearoff=0)
        if kind == "entry":
            m.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
            m.add_separator()
            m.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
            m.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
            m.add_command(label="全选", command=lambda: self._select_all(widget, kind))
            m.add_separator()
            m.add_command(label="清空", command=lambda: widget.delete(0, "end"))
        else:
            m.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
            m.add_separator()
            m.add_command(label="全选", command=lambda: self._select_all(widget, kind))

        def popup(e):
            widget.focus_set()          # 先聚焦，否则粘贴会跑到别的控件上
            try:
                m.tk_popup(e.x_root, e.y_root)
            finally:
                m.grab_release()
            return "break"

        widget.bind("<Button-3>", popup)
        widget.bind("<Control-a>", lambda e: self._select_all(widget, kind))
        return m

    # ── 左：历史记录列表 ──
    def _build_history(self, parent):
        head = ttk.Frame(parent)
        head.pack(fill="x", padx=(0, 8))
        self.hist_title = ttk.Label(head, text="历史记录", font=(FONT, 10, "bold"))
        self.hist_title.pack(side="left")
        ttk.Button(head, text="刷新", width=6, command=self.reload_history).pack(side="right")

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=(0, 8), pady=(6, 0))
        self.canvas = tk.Canvas(wrap, highlightthickness=0, background=CARD)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.list_frame = tk.Frame(self.canvas, background=CARD)
        self._win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        # 滚轮只绑到整个应用（bind_all），但处理时判断鼠标是否真的在列表上，
        # 否则在右边看总结滚轮时，左边历史列表会跟着一起滚。
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _is_in_history(self, w) -> bool:
        """判断某个控件是否属于左栏历史列表（含它的子控件）。"""
        p = w
        while p is not None:
            if p is self.canvas or p is self.list_frame:
                return True
            p = getattr(p, "master", None)
        return False

    def _on_wheel(self, event):
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        if self._is_in_history(w):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"
        return None

    # ── 右：总结 ──
    def _build_summary(self, parent):
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill="both", expand=True)

        # 「进度」页签：实时步骤 + 动画（点开始总结时自动切到这里）
        self.step_view = StepView(self.nb)
        self.nb.add(self.step_view, text=" 进度 ")

        self.view_tx = self._make_text(self.nb, mono=False)
        self.raw_tx = self._make_text(self.nb, mono=True)
        self.nb.add(self.view_tx.master, text=" 结构化视图 ")
        self.nb.add(self.raw_tx.master, text=" 原始 JSON ")
        for tx in (self.view_tx, self.raw_tx):
            self._style_text(tx)
            self._attach_ctx_menu(tx, kind="text")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="保存 JSON", command=lambda: self.save("json")).pack(side="left")
        ttk.Button(bar, text="保存 Markdown", command=lambda: self.save("md")).pack(side="left", padx=6)
        ttk.Button(bar, text="打开输出目录", command=self.open_dir).pack(side="left")
        ttk.Button(bar, text="清理临时视频", command=self.cleanup_tmp_dir).pack(side="left", padx=6)

    def _build_status(self):
        bar = ttk.Frame(self.root, padding=(12, 2, 12, 8))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="就绪", font=(FONT, 9), foreground="#0a7",
                                anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

    def _make_text(self, parent, mono=False):
        holder = ttk.Frame(parent)
        tx = tk.Text(holder, wrap="word", font=("Consolas" if mono else FONT, 10),
                     padx=14, pady=12, background=CARD, relief="flat",
                     foreground=TEXT, insertbackground=ACCENT,
                     selectbackground=ROW_SEL)
        sb = ttk.Scrollbar(holder, command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        tx.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tx

    def _style_text(self, tx: tk.Text):
        tx.tag_configure("h2", font=(FONT, 12, "bold"), foreground=ACCENT_DK,
                         spacing1=14, spacing3=4)
        tx.tag_configure("meta", font=(FONT, 9), foreground=SUB, spacing3=6)
        tx.tag_configure("quote", font=(FONT, 11), foreground="#0a5c36",
                         background="#eef8f1", lmargin1=10, lmargin2=10,
                         rmargin=10, spacing1=6, spacing3=6)
        tx.tag_configure("seg", font=(FONT, 10, "bold"), foreground="#333", spacing1=6)
        tx.tag_configure("body", font=(FONT, 10), spacing2=3)
        tx.tag_configure("err", font=(FONT, 10), foreground=DANGER)
        tx.tag_configure("hint", font=(FONT, 11), foreground="#999")

    # ══════════ 历史列表渲染 ══════════

    def refresh_history(self, select=None):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows = []
        self.thumbs = {}                     # 旧行的图引用一并丢弃
        self.recs.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        for idx, rec in enumerate(self.recs):
            frame = self._make_row(idx, rec)
            self.rows.append(frame)          # 必须先登记，_set_thumb 才能按 idx 找到这一行
            self._load_cached_thumb(idx, rec)
        self.hist_title.configure(text=f"历史记录（{len(self.recs)}）")
        if select is not None:
            self.select(select)
        elif self.cur is None and self.recs:
            self.select(0)

    def _load_cached_thumb(self, idx, rec):
        """有磁盘缓存就直接用，省一次联网。"""
        cp = cover_cache_path(rec)
        if os.path.isfile(cp):
            try:
                with open(cp, "rb") as f:
                    self._set_thumb(idx, f.read())
            except Exception:
                pass
        elif not rec.get("cover"):
            self._set_thumb(idx, None)

    def _make_row(self, idx, rec):
        bg = ROW_BAD if rec.get("failed") else CARD
        f = tk.Frame(self.list_frame, background=bg, cursor="hand2")
        f.pack(fill="x", pady=2, padx=2)

        thumb = tk.Label(f, background=bg, text="无图", foreground="#bbb",
                         font=(FONT, 7), width=15, height=4)
        thumb.pack(side="left", padx=(2, 8), pady=4)

        txt = tk.Frame(f, background=bg)
        txt.pack(side="left", fill="both", expand=True, pady=4)
        title = tk.Label(txt, text=rec.get("title") or "(未命名)", background=bg,
                         font=(FONT, 10), wraplength=190, justify="left", anchor="w")
        title.pack(fill="x")
        bits = [x for x in (rec.get("up"), rec.get("duration")) if x]
        if rec.get("pending"):
            bits.append(rec.get("state_text") or "处理中…")
        elif rec.get("failed"):
            bits.append("❌ 失败（点日志看原因）")
        sub = tk.Label(txt, text=" · ".join(bits) or "（无元信息）", background=bg,
                       font=(FONT, 8), foreground=ACCENT if rec.get("pending")
                       else (DANGER if rec.get("failed") else SUB), anchor="w")
        sub.pack(fill="x")

        widgets = (f, thumb, txt, title, sub)
        for w in widgets:
            w.bind("<Button-1>", lambda e, i=idx: self.select(i))
        if not rec.get("pending") and not rec.get("failed"):
            # 悬停动画：渐变到悬停色 / 离开时渐变回来（选中行不做悬停）
            for seq, target in (("<Enter>", ROW_HOVER), ("<Leave>", CARD)):
                f.bind(seq, lambda e, i=idx, t=target: self._hover_row(i, t))
        self._paint_row(f, idx, widgets, selected=False)
        self.thumbs.setdefault(idx, None)
        return f

    def _hover_row(self, idx, target):
        if idx >= len(self.rows) or idx == self.cur:
            return
        frame = self.rows[idx]
        if frame.winfo_exists():
            _fade_bg(tuple(frame.winfo_children()) + (frame,), target)

    # ── 进行中记录（占位行）的更新 ──
    def _pending_idx(self):
        if self.pending_rec is None:
            return None
        try:
            return self.recs.index(self.pending_rec)
        except ValueError:
            return None

    def _update_pending(self, info):
        """后台取到视频信息后：占位行立即变成真实标题/UP主/时长，并补封面。"""
        if self.pending_rec is None:
            return
        self.pending_rec.update({
            "bvid": info.get("bvid", ""),
            "title": info.get("title", "") or "（未命名）",
            "up": info.get("up", ""), "duration": info.get("duration", ""),
            "cover": info.get("cover", ""),
        })
        self.step_view.title_var.set(info.get("title") or "正在处理")
        self.step_view.set_state("读取视频信息", "done")
        self._rebuild_pending_row()
        i = self._pending_idx()
        if i is not None:
            self._load_cached_thumb(i, self.pending_rec)

    def _refresh_pending_row_text(self):
        i = self._pending_idx()
        if i is None or i >= len(self.rows):
            return
        frame = self.rows[i]
        if not frame.winfo_exists():
            return
        kids = frame.winfo_children()
        if len(kids) >= 2 and isinstance(kids[1], tk.Frame):
            labels = kids[1].winfo_children()
            if len(labels) >= 2:
                pr = self.pending_rec or {}
                bits = [x for x in (pr.get("up"), pr.get("duration")) if x]
                bits.append(pr.get("state_text") or "处理中…")
                labels[1].configure(text=" · ".join(bits))

    def _rebuild_pending_row(self):
        i = self._pending_idx()
        if i is None or i >= len(self.rows):
            return
        old = self.rows[i]
        if old.winfo_exists():
            old.destroy()
        self.rows[i] = self._make_row(i, self.recs[i])

    def _paint_row(self, frame, idx, widgets, selected):
        rec = self.recs[idx] if idx < len(self.recs) else {}
        if rec.get("failed"):
            bg = ROW_BAD
        else:
            bg = ROW_SEL if selected else CARD
        frame.configure(background=bg)
        for w in widgets:
            try:
                w.configure(background=bg)
            except Exception:
                pass

    def _set_thumb(self, idx, raw):
        """把缩略图贴到某一行；raw 为 None 表示这条记录没有封面。"""
        if idx >= len(self.rows):
            return
        labels = [w for w in self.rows[idx].winfo_children()
                  if isinstance(w, tk.Label) and w.cget("text") in ("无图", "无封面")]
        if not raw:
            for w in labels:
                w.configure(text="无封面")
            return
        try:
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im = im.resize((104, 59), Image.LANCZOS)
            ph = ImageTk.PhotoImage(im)
            self.thumbs[idx] = ph            # 必须留引用，否则被 GC 变空白
            for w in labels:
                w.configure(image=ph, text="", width=104, height=59)
                break
        except Exception:
            pass

    def _cover_worker(self):
        """后台补齐封面：先查磁盘缓存，再联网取；取到后按 json 路径找回对应行更新。

        （不能用下标传递：refresh_history 会按时间重排，下标和启动时不一致。）
        """
        for rec in list(self.recs):
            key = rec.get("json")
            cp = cover_cache_path(rec)
            if os.path.isfile(cp) or not rec.get("cover"):
                continue
            raw = fetch_cover_bytes(rec.get("cover"))
            if raw:
                try:
                    os.makedirs(COVER_DIR, exist_ok=True)
                    with open(cp, "wb") as f:
                        f.write(raw)
                except Exception:
                    pass
                self.q.put(("thumb", (key, raw)))

    def select(self, idx):
        if idx < 0 or idx >= len(self.recs):
            return
        self.cur = idx
        for i, frame in enumerate(self.rows):
            sel = (i == idx)
            self._paint_row(frame, i, tuple(frame.winfo_children()), selected=sel)
            for w in frame.winfo_children():
                if isinstance(w, tk.Frame):
                    self._paint_row(w, i, tuple(w.winfo_children()), selected=sel)
        self._render_record(self.recs[idx])

    def reload_history(self):
        self.recs = scan_existing_summaries(load_history())
        save_history(self.recs)
        self.refresh_history(select=0)
        self._log("已重新扫描磁盘上的总结文件")

    def _render_record(self, rec):
        p = _abs(rec.get("json") or "")
        self.raw_tx.delete("1.0", "end")
        self.view_tx.delete("1.0", "end")
        if rec.get("pending"):
            self.view_tx.insert("end", "这条记录正在处理中，\n切到「进度」页签可看实时步骤。\n", "hint")
            self.raw_tx.insert("end", "（处理完成后这里会显示原始回答）")
            return
        if not p or not os.path.isfile(p):
            self.view_tx.insert("end", f"记录对应的 JSON 不存在：\n{p}\n", "err")
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.view_tx.insert("end", f"读取 JSON 失败：{e}\n", "err")
            return
        self.raw_tx.insert("end", json.dumps(data, ensure_ascii=False, indent=2))
        for tag, text in summary_rows(data):
            self.view_tx.insert("end", text + "\n", tag)
        self._selected_data = data

    # ══════════ 账号 / 模型入口 ══════════

    def _boot_auth(self):
        """启动时后台检查登录态 + 拉取页面上的模型列表（都要开浏览器，约 10~25 秒）。"""
        try:
            ok, who = check_login()
            self.q.put(("login", (ok, who)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"[提示] 登录检测失败：{str(e)[:150]}"))
        self._fetch_models()

    def _fetch_models(self):
        try:
            ms = list_models()
            if ms:
                # 记住这次读到的列表：下次就算页面结构变了读不到，下拉里也还有旧候选
                st = load_settings()
                st["models"] = ms
                save_settings(st)
                self.q.put(("models", ms))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"[提示] 读取模型列表失败：{str(e)[:150]}"))

    def refresh_models(self):
        if self.busy or self.authing or self.checking:
            return
        self._log("正在从千问页面读取可用模型…")
        threading.Thread(target=self._fetch_models, daemon=True).start()

    def _on_model_change(self):
        st = load_settings()
        st["model"] = self.model_var.get()
        save_settings(st)
        self._log(f"已切换模型：{self.model_var.get()}")

    def do_selfcheck(self):
        """环境自检：ffmpeg / Edge / 目录 / 千问登录态 / 可用模型。

        给"打包给别人用"的场景兜底：别人跑不起来时，点一下就知道缺什么。
        """
        if self.busy or self.authing or self.checking:
            messagebox.showinfo("提示", "有任务/登录/自检正在进行，请等它结束。")
            return
        self.checking = True
        self.login_btn.configure(state="disabled")
        self.model_btn.configure(state="disabled")
        self.btn.configure(state="disabled")
        self._log("开始环境自检…（联网部分要开一次浏览器，约 30 秒）")

        def work():
            try:
                res = run_selfcheck(do_online=True,
                                    on_tick=lambda m: self.q.put(("log", m)))
            except Exception as e:  # noqa: BLE001
                res = [("自检异常", False, f"{type(e).__name__}: {str(e)[:200]}")]
            self.q.put(("selfcheck", res))

        threading.Thread(target=work, daemon=True).start()

    def _show_selfcheck(self, res):
        """自检结果弹窗：每项 ✅/❌ + 说明，最后给一句结论。"""
        win = tk.Toplevel(self.root)
        win.title("环境自检")
        win.geometry("720x420")
        tx = tk.Text(win, wrap="word", font=(FONT, 10), padx=12, pady=10,
                     background="#ffffff", relief="flat")
        sb = ttk.Scrollbar(win, command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        tx.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tx.tag_configure("ok", font=(FONT, 10, "bold"), foreground="#0a7d34")
        tx.tag_configure("bad", font=(FONT, 10, "bold"), foreground="#b00020")
        tx.tag_configure("detail", font=(FONT, 9), foreground="#666", lmargin1=24, lmargin2=24)
        bad = 0
        for name, ok, detail in res:
            tx.insert("end", ("✅ " if ok else "❌ ") + name + "\n", "ok" if ok else "bad")
            tx.insert("end", "      " + str(detail) + "\n\n", "detail")
            bad += (not ok)
        if bad:
            tx.insert("end", f"有 {bad} 项未通过：按上面提示处理后再试。\n", "bad")
        else:
            tx.insert("end", "全部通过，环境没问题。\n", "ok")
        tx.configure(state="disabled")

    def do_login(self):
        """打开一个可见的 Edge 窗口让用户自己登录（程序不接触密码）。"""
        if self.busy or self.authing or self.checking:
            messagebox.showinfo("提示", "有任务/自检正在进行，请等它结束。")
            return
        self.authing = True
        self.login_btn.configure(state="disabled")
        self.btn.configure(state="disabled")
        self.acct_lb.configure(text="账号：等待登录…", foreground="#c60")
        self._log("已打开登录窗口：请在弹出的 Edge 里登录千问，登录成功后会自动识别。")

        def work():
            try:
                ok, who = open_login_window(on_tick=lambda m: self.q.put(("log", m)))
                self.q.put(("login", (ok, who)))
                if ok:
                    self._fetch_models()
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"[提示] 登录流程异常：{str(e)[:150]}"))
                self.q.put(("login", (False, "")))
        threading.Thread(target=work, daemon=True).start()

    # ══════════ 运行 ══════════

    def start(self):
        url = self.entry.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴视频链接")
            return
        if self.busy or self.checking or self.authing:
            return
        self.busy = True
        self.btn.configure(state="disabled")
        self._log("开始处理：" + url)

        # 需求：点「开始总结」时**立刻**在左栏出现一条进行中的记录（完成后转正）
        self.pending_rec = {
            "bvid": "", "title": "（读取视频信息…）", "up": "", "duration": "",
            "cover": "", "ts": int(time.time()), "json": "", "md": "", "mp4": "",
            "pending": True, "state_text": "准备中…",
        }
        self.recs.insert(0, self.pending_rec)
        self.refresh_history(select=0)
        self.step_view.start("正在处理")
        self.step_view.set_state("读取视频信息", "running", url[:56])
        self.nb.select(self.step_view)

        # Tk 变量只能主线程读：这里先取出来再传给工作线程
        # （在工作线程里直接 self.model_var.get() 会抛 "main thread is not in main loop"）
        model = self.model_var.get()
        preamble = load_settings().get("prompt_preamble") or DEFAULT_PREAMBLE
        threading.Thread(target=self._worker, args=(url, model, preamble), daemon=True).start()

    def _worker(self, url, model, preamble):
        try:
            info = get_video_info(url)
            self.q.put(("info", info))
            self.q.put(("log", f"视频：{info['title']}（{info['up']} · {info['duration']}）"))
            cov = fetch_cover_bytes(info.get("cover"))
            if cov:
                try:
                    os.makedirs(COVER_DIR, exist_ok=True)
                    with open(cover_cache_path(info), "wb") as f:
                        f.write(cov)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"[错误] 读取视频信息失败：{e}"))
            self.q.put(("fail", "读取视频信息失败"))
            self.q.put(("done", False))
            return

        hook = lambda m: self.q.put(("log", m))  # noqa: E731

        def step_hook(m):
            """引擎日志里捎带出步骤事件（日志本身照常显示）。"""
            hook(m)
            if "[1/3] 下载" in m:
                self.q.put(("step", ("下载视频", "360P + 64K")))
            elif "[2/3] 启动浏览器" in m:
                self.q.put(("step", ("上传并解析", "连接千问…")))
            elif "[2/3] 上传视频" in m:
                self.q.put(("step", ("上传并解析", "上传到服务器…")))
            elif "解析完成" in m:
                self.q.put(("step", ("AI 生成", "已解析，发送提问…")))
            elif "等待生成" in m:
                self.q.put(("step", ("AI 生成", "模型正在观看视频…")))

        add_log_hook(step_hook)
        mp4, answer = "", ""
        try:
            mp4 = download(url, OUTDIR, QUALITY, AUDIO_Q, force=False)
            self.q.put(("step", ("上传并解析", "")))
            answer = ask_qwen(mp4, build_prompt(load_schema(), preamble), model)
            if answer:
                self.q.put(("step", ("保存清理", "")))
        except SystemExit as e:
            self.q.put(("log", f"[错误] {e}"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"[错误] {type(e).__name__}: {e}"))
            self.q.put(("log", traceback.format_exc(limit=2)))
        finally:
            remove_log_hook(hook)

        if not answer:
            self.q.put(("fail", "处理失败（点「日志」按钮看原因）"))
            self.q.put(("done", False))
            return

        data, err = parse_json_answer(answer)
        if data:
            data = apply_authoritative_meta(data, info)
        self.q.put(("result", (data, answer, err)))

        rec = {
            "bvid": info.get("bvid", ""), "title": info.get("title", ""),
            "up": info.get("up", ""), "duration": info.get("duration", ""),
            "cover": info.get("cover", ""), "ts": int(time.time()),
            "json": "", "md": "", "mp4": _rel(mp4) if mp4 else "",
        }
        saved = False
        try:
            base = os.path.splitext(os.path.basename(mp4))[0] if mp4 else "summary"
            os.makedirs(SUMMARIES_DIR, exist_ok=True)
            # 成果放 summaries/，视频留在 tmp/ —— 清理 tmp 不会伤到总结
            jp = os.path.join(SUMMARIES_DIR, base + ".总结.json")
            mp = os.path.join(SUMMARIES_DIR, base + ".总结.md")
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(data or {"原始回答": answer}, f, ensure_ascii=False, indent=2)
            with open(mp, "w", encoding="utf-8") as f:
                f.write(answer)
            rec["json"], rec["md"] = _rel(jp), _rel(mp)
            self.q.put(("log", f"已保存：summaries/{base}.总结.json / .总结.md"))
            saved = True
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"[警告] 落盘失败：{e}"))

        if saved:
            # 总结已落盘 → 清掉视频本体与中间流（tmp 是临时存放点，只留总结）
            n = len(cleanup_video(mp4, keep=False))
            if n:
                self.q.put(("log", f"已清理 {n} 个视频/中间文件（.总结.json / .总结.md 保留）"))

        self.q.put(("history", rec))
        self.q.put(("done", bool(data)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "thumb":
                    key, raw = payload
                    i = next((n for n, r in enumerate(self.recs)
                              if r.get("json") == key), None)
                    if i is not None:
                        self._set_thumb(i, raw)
                elif kind == "result":
                    self._show_result(*payload)
                elif kind == "login":
                    ok, who = payload
                    self.authing = False
                    self.login_btn.configure(state="normal")
                    self.btn.configure(state="normal")
                    if ok:
                        self.acct_lb.configure(text=f"账号：{who} ✅", foreground="#0a7")
                        self._log(f"登录态有效：{who}")
                    else:
                        self.acct_lb.configure(
                            text="账号：未登录 / 已过期 ⚠ 点右边「登录 / 重新登录」",
                            foreground="#b00")
                        self._log("登录态无效：请点「登录 / 重新登录」，会打开一个 Edge 窗口让你登录")
                elif kind == "models":
                    ms = payload
                    cur = self.model_var.get()
                    self.model_cb.configure(values=ms)
                    if cur not in ms:
                        self.model_var.set(ms[0])
                        self._on_model_change()
                    self.model_hint.configure(text=f"(共 {len(ms)} 个，来自千问页面)")
                    self._log("可用模型：" + "、".join(ms))
                elif kind == "selfcheck":
                    self.checking = False
                    self.login_btn.configure(state="normal")
                    self.model_btn.configure(state="normal")
                    self.btn.configure(state="normal")
                    self._show_selfcheck(payload)
                elif kind == "info":
                    self._update_pending(payload)
                elif kind == "step":
                    name, detail = payload
                    self.step_view.set_state(name, "running", detail)
                    if self.pending_rec is not None:
                        self.pending_rec["state_text"] = f"{name}…"
                        self._refresh_pending_row_text()
                elif kind == "fail":
                    self.step_view.fail(payload)
                    self.step_view.error = payload
                    self.step_view.redraw()
                    if self.pending_rec is not None:
                        self.pending_rec["failed"] = True
                        self.pending_rec["state_text"] = ""
                        i = self._pending_idx()
                        if i is not None and i < len(self.rows):
                            frame = self.rows[i]
                            ws = tuple(frame.winfo_children())
                            self._paint_row(frame, i, ws, selected=(i == self.cur))
                            for w in frame.winfo_children():
                                if isinstance(w, tk.Frame):
                                    self._paint_row(w, i, tuple(w.winfo_children()),
                                                    selected=(i == self.cur))
                        # 重建行以刷新副标题文字
                        self._rebuild_pending_row()
                elif kind == "history":
                    # 进行中记录转正：移除占位行，插入真正的成果记录
                    self.recs = [r for r in self.recs if not r.get("pending")]
                    self.pending_rec = None
                    self.recs.insert(0, payload)
                    save_history(self.recs)
                    self.refresh_history(select=0)
                    self.step_view.finish()
                    self.nb.select(self.view_tx.master)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _show_result(self, data, raw, err):
        self.raw_tx.delete("1.0", "end")
        self.raw_tx.insert("end", raw)
        self.view_tx.delete("1.0", "end")
        if data:
            for tag, text in summary_rows(data):
                self.view_tx.insert("end", text + "\n", tag)
            self._selected_data = data
        else:
            self.view_tx.insert("end", f"模型返回的不是合法 JSON（{err}）。\n"
                                       f"请看「原始 JSON」页签。\n", "err")

    def _finish(self, ok):
        self.busy = False
        self.btn.configure(state="normal")
        self.status.configure(text=("完成 ✅ " if ok else "未完成，点「日志」看原因 ")
                                    + (self.logs[-1] if self.logs else ""),
                              foreground="#0a7" if ok else "#b00")

    # ══════════ 日志 / 保存 ══════════

    def _log(self, msg):
        msg = str(msg)
        self.logs.append(msg)
        last = msg.strip().splitlines()[-1] if msg.strip() else ""
        if last:
            self.status.configure(text=last[:160], foreground="#555")

    def show_log(self):
        win = tk.Toplevel(self.root)
        win.title("运行日志")
        win.geometry("760x460")
        tx = tk.Text(win, wrap="word", font=("Consolas", 9), padx=10, pady=8)
        sb = ttk.Scrollbar(win, command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        tx.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tx.insert("end", "\n".join(self.logs) or "（暂无日志）")
        tx.see("end")
        self._attach_ctx_menu(tx, kind="text")

    def _cur_data(self):
        return getattr(self, "_selected_data", None)

    def save(self, kind):
        data = self._cur_data()
        if data is None:
            messagebox.showinfo("提示", "还没有可保存的总结")
            return
        rec = self.recs[self.cur] if (self.cur is not None and self.cur < len(self.recs)) else {}
        base = rec.get("title") or "summary"
        if kind == "json":
            p = filedialog.asksaveasfilename(defaultextension=".json",
                                             initialfile=base + ".json",
                                             filetypes=[("JSON", "*.json")])
            if not p:
                return
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            p = filedialog.asksaveasfilename(defaultextension=".md",
                                             initialfile=base + ".md",
                                             filetypes=[("Markdown", "*.md")])
            if not p:
                return
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.raw_tx.get("1.0", "end"))
        messagebox.showinfo("已保存", p)

    def open_dir(self):
        rec = self.recs[self.cur] if (self.cur is not None and self.cur < len(self.recs)) else {}
        mp4 = rec.get("mp4")
        d = os.path.dirname(_abs(mp4)) if mp4 else OUTDIR
        try:
            os.startfile(d)  # noqa: S606
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo("输出目录", f"{d}\n\n（自动打开失败：{e}）")

    def cleanup_tmp_dir(self):
        """清掉 tmp 下的视频与中间流，保留 .总结.json/.总结.md 和「旧GUI下载」。"""
        victims = collect_tmp_victims(TMPROOT)
        if not victims:
            messagebox.showinfo("清理临时视频", "tmp 里没有可清理的视频文件。")
            return
        size = sum(os.path.getsize(p) for p in victims if os.path.isfile(p))
        names = "\n".join("  " + os.path.basename(p) for p in victims[:8])
        more = f"\n  …另外 {len(victims)-8} 个" if len(victims) > 8 else ""
        if not messagebox.askyesno(
                "清理临时视频",
                f"将删除 {len(victims)} 个视频/中间文件，共 {size/1024/1024:.1f} MB：\n\n"
                f"{names}{more}\n\n"
                f"只删 tmp 下的视频与 .m4s 中间流；\n"
                f".总结.json / .总结.md 和「旧GUI下载」都会保留。\n\n确认删除？"):
            return
        n, freed = 0, 0
        for p in victims:
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                n += 1
                freed += sz
            except OSError as e:
                self._log(f"[警告] 删除失败 {os.path.basename(p)}：{e}")
        self._log(f"已清理 {n} 个视频/中间文件，释放 {freed/1024/1024:.1f} MB")
        messagebox.showinfo("清理完成", f"已删除 {n} 个文件，释放 {freed/1024/1024:.1f} MB。")


def main():
    ap = argparse.ArgumentParser(description="B站视频 → 结构化总结（图形界面）")
    ap.add_argument("url", nargs="?", default="", help="视频链接或 BV 号")
    ap.add_argument("--auto", action="store_true", help="启动后自动开始总结（配合链接使用）")
    a = ap.parse_args()

    root = tk.Tk()
    app = App(root)
    if a.url:
        app.entry.delete(0, "end")
        app.entry.insert(0, a.url)
        if a.auto:
            root.after(600, app.start)
    root.mainloop()


if __name__ == "__main__":
    main()
