# -*- coding: utf-8 -*-
"""GUI 接线集成测试（历史列表版）：不联网、不注入鼠标。

验证：按钮 → start() → 线程 → 队列 → 左栏历史列表新增一行 + 右栏渲染该条总结；
      以及「点历史行 → 右栏切换成那一条」。
外部依赖（取信息/取封面/下载/问模型）全部替换成假实现；
history.json 与封面缓存目录改到临时路径，不污染工作区。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"D:\py_project\b站视频总结")
import tkinter as tk  # noqa: E402

import bili_ui as U  # noqa: E402

TMP = tempfile.mkdtemp(prefix="uitest_")
U.HISTORY_FILE = os.path.join(TMP, "history.json")
U.COVER_DIR = os.path.join(TMP, "covers")
U.SUMMARIES_DIR = os.path.join(TMP, "summaries")      # 成果目录也改到临时区，别污染真实 summaries/
U.SETTINGS_FILE = os.path.join(TMP, "settings.json")  # 设置文件同理
json.dump([], open(U.HISTORY_FILE, "w", encoding="utf-8"))

# 预置「自定义前置说明」，验证它真的被拼进提示词（且 JSON 结构部分原样保留）
_TEST_PREAMBLE = "【测试前置】请用 500 字总结，只讲缺点。"
json.dump({"prompt_preamble": _TEST_PREAMBLE},
          open(U.SETTINGS_FILE, "w", encoding="utf-8"), ensure_ascii=False)

FAKE_MP4 = os.path.join(TMP, "测试视频.mp4")
open(FAKE_MP4, "wb").write(b"\x00" * 1024)

FAKE_A = json.dumps({
    "视频标题": "", "UP主": "某只", "时长": "0秒", "内容类型": "数码测评",
    "结论": "【A条】这是测试结论。",
    "核心要点": ["A要点一", "A要点二"],
    "分段摘要": [{"时间点": "00:00-01:00", "主题": "A开场", "要点": "开箱"}],
    "争议或不足": ["长焦挑光线"],
    "适合谁看": "想换机的人", "关键词": ["测试"],
}, ensure_ascii=False)

CALLS = {}


def fake_info(url):
    CALLS["info"] = url
    return {"bvid": "BV1TEST", "title": "【A条】标题被接口校正",
            "up": "真UP主", "duration": "25分41秒", "cover": ""}


def fake_download(url, outdir, quality, audio, force=False):
    CALLS["download"] = (url, outdir, quality, audio)
    return FAKE_MP4


def fake_ask(mp4, prompt, model):
    CALLS["ask"] = (mp4, len(prompt), model)
    CALLS["prompt_has_schema"] = ("JSON 结构" in prompt and "分段摘要" in prompt)
    CALLS["prompt_startswith_preamble"] = prompt.startswith(_TEST_PREAMBLE)
    CALLS["prompt_full"] = prompt
    return "好的：\n```json\n" + FAKE_A + "\n```"


U.get_video_info = fake_info
U.fetch_cover_bytes = lambda url: None      # 不联网取封面
U.download = fake_download
U.ask_qwen = fake_ask
# 启动时的登录检测 / 模型列表也别真开浏览器
U.check_login = lambda headless=True: (True, "测试账号")
U.list_models = lambda headless=True: ["Qwen3.8-Omni-Flash", "Qwen3.8-Max", "Qwen3.7-Plus"]
U.open_login_window = lambda wait_seconds=900, on_tick=None: (True, "测试账号")

# 预置一条历史记录 + 它的封面缓存，验证「列表行真的贴上了缩略图」
# （曾因"行还没登记进列表就去回填"而静默失败）
from PIL import Image  # noqa: E402
os.makedirs(U.COVER_DIR, exist_ok=True)
os.makedirs(U.SUMMARIES_DIR, exist_ok=True)
_seed_sum = os.path.join(U.SUMMARIES_DIR, "封面测试视频.总结.json")
json.dump({"视频标题": "封面测试视频", "一句话总结": "种子记录"},
          open(_seed_sum, "w", encoding="utf-8"), ensure_ascii=False)
SEED = {"bvid": "BVSEED1", "title": "封面测试视频", "up": "种子UP", "duration": "1分",
        "cover": "", "ts": 1000, "json": _seed_sum, "md": "", "mp4": ""}
json.dump([SEED], open(U.HISTORY_FILE, "w", encoding="utf-8"), ensure_ascii=False)
Image.new("RGB", (320, 180), (30, 120, 200)).save(U.cover_cache_path(SEED), "JPEG")
print("已预置 1 条种子记录 + 其封面缓存")

root = tk.Tk()
root.withdraw()
app = U.App(root)
n0 = len(app.recs)                          # 启动时会扫到磁盘上已有的总结

app.entry.delete(0, "end")
app.entry.insert(0, "https://www.bilibili.com/video/BV1TEST")
app.btn.invoke()                            # ← 真实触发「开始总结」
root.update()                               # 处理一轮事件：占位行/进度页签应已就位
checks_pending = [
    ("点开始后立即出现进行中记录",
     app.pending_rec is not None and len(app.rows) == n0 + 1),
    ("自动切到「进度」页签", app.nb.nametowidget(app.nb.select()) is app.step_view),
    ("第一步（读取视频信息）处于运行中",
     app.step_view.state.get("读取视频信息", ("",))[0] == "running"),
]
print(f"已通过 Button.invoke() 触发；启动时列表有 {n0} 条，当前 {len(app.rows)} 条")

t0 = time.time()
while time.time() - t0 < 20:
    root.update()
    time.sleep(0.05)
    if app.status.cget("text").startswith(("完成", "未完成")):
        break

view = app.view_tx.get("1.0", "end")
status = app.status.cget("text")
hist = json.load(open(U.HISTORY_FILE, encoding="utf-8"))

print("\n=== 结果 ===")
print("列表条数            ：", n0, "→", len(app.recs), f"（界面行数 {len(app.rows)}）")
print("第 1 行标题         ：", app.recs[0]["title"] if app.recs else "—")
print("第 1 行 UP主/时长   ：", app.recs[0].get("up"), "/", app.recs[0].get("duration"))
print("当前选中            ：", app.cur)
print("download 参数       ：", CALLS.get("download"))
print("模型                ：", CALLS.get("ask", ("", 0, ""))[2])
print("状态栏              ：", status)

checks = [
    ("按钮触发 start()", "info" in CALLS and "download" in CALLS),
    ("download 直接落在 tmp（扁平，无画质子目录）",
     CALLS.get("download", ("", "", "", ""))[1] == U.OUTDIR),
    ("提示词以自定义前置说明开头",
     bool(CALLS.get("prompt_startswith_preamble"))),
    ("提示词里 JSON 结构部分原样保留", bool(CALLS.get("prompt_has_schema"))),
    ("自定义前置说明已持久化",
     json.load(open(U.SETTINGS_FILE, encoding="utf-8")).get("prompt_preamble") == _TEST_PREAMBLE),
    ("用的是 omni 模型", CALLS.get("ask", ("", 0, ""))[2] == "Qwen3.8-Omni-Flash"),
    ("左栏历史列表新增一行", len(app.recs) == n0 + 1),
    ("界面行数与记录数一致", len(app.rows) == len(app.recs)),
    ("新记录排在最前", (app.recs[0].get("json") or "").endswith(".总结.json")),
    ("新记录被自动选中", app.cur == 0),
    ("右栏渲染出核心要点", "核心要点" in view and "A要点一" in view),
    ("右栏渲染出分段摘要", "分段摘要" in view and "00:00-01:00" in view),
    ("顶部直接是「结论」", "结论：【A条】这是测试结论。" in view),
    ("新格式不再渲染关键数据/亮点", "关键数据" not in view and "亮点" not in view),
    ("完成后进度页签全部打勾",
     all(s[0] == "done" for s in app.step_view.state.values())),
    ("标题被接口值校正", (app._cur_data() or {}).get("视频标题") == "【A条】标题被接口校正"),
    ("UP主被接口值校正", (app._cur_data() or {}).get("UP主") == "真UP主"),
    ("状态栏显示完成", status.startswith("完成")),
    ("总结落盘到 summaries/（不是 tmp/）",
     os.path.isfile(os.path.join(U.SUMMARIES_DIR, "测试视频.总结.json"))),
    ("新记录 json 落在 summaries 目录", "summaries" in (app.recs[0].get("json") or "")),
    ("history.json 已更新", len(hist) == n0 + 1),
]
checks = checks_pending + checks          # 「点开始后立即出现」的断言放最前面

# —— 测「点历史行 → 右栏切换」：塞一条特征明显的假记录再选中它 ——
fake_json = os.path.join(TMP, "第二条.总结.json")
json.dump({"视频标题": "【第二条】", "一句话总结": "这是第二条记录的特征文字ZZZ",
           "核心要点": ["第二条要点"]}, open(fake_json, "w", encoding="utf-8"),
          ensure_ascii=False)
app.recs.insert(0, {"bvid": "BV2", "title": "【第二条】", "up": "UP2", "duration": "1分",
                    "cover": "", "ts": int(time.time()), "json": fake_json,
                    "md": "", "mp4": ""})
app.refresh_history()
app.select(0)
root.update()
view2 = app.view_tx.get("1.0", "end")
checks.append(("点第 1 行 → 右栏切到那一条", "第二条记录的特征文字ZZZ" in view2))
checks.append(("切换后原始 JSON 同步", "【第二条】" in app.raw_tx.get("1.0", "end")))

# —— 旧格式兼容：种子记录用的是旧字段「一句话总结」，也应渲染成顶部结论 ——
app.select(2)                               # [第二条, 新记录, 种子记录]
root.update()
view3 = app.view_tx.get("1.0", "end")
checks.append(("旧记录的「一句话总结」兼容渲染为结论", "结论：种子记录" in view3))

# —— 测右键菜单 & 粘贴 ——
checks.append(("链接框已绑定右键菜单", bool(app.entry.bind("<Button-3>"))))
checks.append(("右键菜单第一项是「粘贴」", app.entry_menu.entrycget(0, "label") == "粘贴"))
checks.append(("总结框也有右键菜单", bool(app.raw_tx.bind("<Button-3>"))))
try:
    root.clipboard_clear()
    root.clipboard_append("BV1PasteTest")
    root.update()
    app.entry.delete(0, "end")
    app.entry.focus_set()
    app.entry_menu.invoke(0)            # 触发「粘贴」
    root.update()
    pasted = app.entry.get()
except Exception as e:
    pasted = f"<异常 {e}>"
checks.append((f"右键菜单「粘贴」真的生效（得到 {pasted!r}）", pasted == "BV1PasteTest"))

app._select_all(app.entry, "entry")
root.update()
checks.append(("Ctrl+A 全选生效", bool(app.entry.selection_present())))

# —— 测「临时视频清理」的挑选逻辑（不真删，只验证挑得对不对）——
ct = os.path.join(TMP, "cleanme")
os.makedirs(os.path.join(ct, "旧GUI下载"), exist_ok=True)
for n in ("a.mp4", "a.video.m4s", "a.audio.m4s.part.json", "a.总结.json", "a.总结.md"):
    open(os.path.join(ct, n), "w").write("x")
open(os.path.join(ct, "旧GUI下载", "b.mp4"), "w").write("x")
victims = [os.path.basename(p) for p in U.collect_tmp_victims(ct)]
checks.append((f"清理只挑视频/中间流（挑到 {victims}）",
               set(victims) == {"a.mp4", "a.video.m4s", "a.audio.m4s.part.json"}))
checks.append(("清理不碰 旧GUI下载/", "b.mp4" not in victims))

# —— 封面缩略图是否真的贴到了列表行上 ——（只看有缓存的行；最后插入的假记录本来就没封面）
_imgs = [w.cget("image")
         for r in app.rows for w in r.winfo_children() if isinstance(w, tk.Label)]
checks.append((f"列表行贴上了封面缩略图（{sum(1 for i in _imgs if i)} 行有图）",
               any(bool(i) for i in _imgs)))

# —— 环境自检（离线部分：ffmpeg / Edge / 目录，不联网）——
sc = U.run_selfcheck(do_online=False)
sc_names = [n for n, ok, _d in sc]
checks.append(("自检包含 ffmpeg/Edge/两个目录项",
               {"ffmpeg 合流工具", "Edge 浏览器（自动化内核）",
                "成果目录 summaries/", "临时目录 tmp/"} <= set(sc_names)))
sc_map = {n: ok for n, ok, _d in sc}
checks.append(("自检：ffmpeg 可用（合流不会断）", sc_map.get("ffmpeg 合流工具", False)))
checks.append(("自检：Edge 可用（自动化内核在）", sc_map.get("Edge 浏览器（自动化内核）", False)))

# —— 滚轮联动修复：只有鼠标真在历史列表上才滚动 ——
checks.append(("滚轮判定：鼠标在列表内 → 滚", app._is_in_history(app.list_frame)))
checks.append(("滚轮判定：鼠标在列表行子控件 → 滚",
               bool(app.rows) and app._is_in_history(app.rows[0].winfo_children()[0])))
checks.append(("滚轮判定：鼠标在右侧总结区 → 不滚", not app._is_in_history(app.view_tx)))

# —— 账号 / 模型入口（等启动期后台检测把事件送回主线程）——
_t1 = time.time()
while time.time() - _t1 < 4:
    root.update()
    time.sleep(0.05)
checks.append((f"账号栏显示已登录（{app.acct_lb.cget('text')}）",
               "✅" in app.acct_lb.cget("text")))
_vals = list(app.model_cb.cget("values") or ())
checks.append((f"模型下拉被填充（{len(_vals)} 个：{_vals}）", len(_vals) >= 2))
if _vals:
    app.model_var.set(_vals[-1])
    app._on_model_change()
    root.update()
    _st = json.load(open(U.SETTINGS_FILE, encoding="utf-8"))
    checks.append((f"模型选择写入 settings.json（{_st.get('model')}）",
                   _st.get("model") == _vals[-1]))

print("\n=== 断言 ===")
ok = True
for name, val in checks:
    print(("  ✅ " if val else "  ❌ ") + name)
    ok = ok and bool(val)
print("\n" + ("全部通过 ✅" if ok else "有失败项 ❌"))

root.destroy()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
