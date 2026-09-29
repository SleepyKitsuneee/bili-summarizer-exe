#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B站视频总结：给一个链接 → 下载 360P+64K → 传到 Qwen 网页端 → 取回 2000 字总结。

用法：
  python bili_qwen_summary.py "https://www.bilibili.com/video/BVxxxx"
  python bili_qwen_summary.py "BVxxxx" -o ./tmp
  python bili_qwen_summary.py "BVxxxx" --prompt "用1000字总结，重点讲外观和续航"
  python bili_qwen_summary.py "BVxxxx" --no-json --prompt "用2000字总结该视频"   # 不走 JSON 格式
  python bili_qwen_summary.py "BVxxxx" --skip-download --video 已有视频.mp4
  python bili_qwen_summary.py "BVxxxx" --keep-parts      # 保留视频，不自动清理

输出与清理：
  - 视频与中间流直接落在 ./tmp（**扁平，不分画质子目录**）
  - **总结（`.总结.json` / `.总结.md`）落在 ./summaries** —— 成果与临时视频分开存放
  - **上传成功并取回总结后，视频本体和 .m4s 中间流会自动删除**，只留 summaries 里的成果
  - 失败时**不清理**，方便直接重跑（会复用已下载好的文件）
  - 扁平目录的画质歧义：成功下载后写 `<名>.下载信息.json` 记录画质；再遇到"已存在"时
    比对画质，**不一致就自动 --force 重下**，不会把错画质的文件拿去上传

依赖：
  - 解释器（用户指定）：C:/ProgramData/anaconda3/envs/base_env/python.exe
  - 下载：同目录的 bili_dl.py（纯标准库）
  - 上传：playwright（base_env 已装）+ 本机真实 Edge 内核
  - 登录态：持久化 profile，登一次长期复用

实测要点（踩过的坑，别改）：
  1. 上传必须走页面自己的入口：点「选择模式」→ 点「上传附件」→ 捕获文件选择器。
     直接对隐藏的 #filesUpload 调 set_input_files 会被前端忽略（零上传请求）。
  2. 上传到 OSS 成功 ≠ 可用，要等服务端解析（附件卡片加载态消失）。
  3. 这个站会随机弹 A/B「你更喜欢哪个回复」面板，需要点「跳过」。
  4. 回答正文在接口的 content_list 里，用 phase 区分：'think'=思考过程（可能是英文、很长），
     'answer'=正文。content 字段常为空。取正文只能取 phase=='answer'。
  5. 长提示词（如 JSON 契约）必须用 keyboard.insert_text 输入，不能用 type()：
     内含换行会被当成回车，把消息提前发出去。
  6. -o 指定的目录会自动加一层画质子目录（downloads → downloads/360P），
     避免同一部视频的多个画质同名文件互相顶替（曾误把 1080P 当上传对象）。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

# ── 路径解析：兼容「源码运行」与「PyInstaller 打包运行」两种模式 ──
# frozen（exe）时：可写数据（tmp/summaries/history.json…）放 **exe 旁边**；
#                  只读资源（ffmpeg.exe、summary_schema.json）在 _MEIPASS（_internal）里。
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = RES_DIR = os.path.dirname(os.path.abspath(__file__))
HERE = APP_DIR                      # 兼容旧引用
BILI_DL = os.path.join(RES_DIR, "bili_dl.py")   # 仅源码模式用；exe 模式走 --bili-dl-worker
# 成果目录：总结（.总结.json / .总结.md）放这里，**与临时视频目录分开**，
# 免得哪天清空 tmp 把成果一起清掉。
SUMMARIES_DIR = os.path.join(APP_DIR, "summaries")
# 视频临时存放点（**扁平**，不再按画质分目录 —— 视频用完即删，画质信息记在 .下载信息.json 里）
TMP_DIR = os.path.join(APP_DIR, "tmp")
SCHEMA_FILE = os.path.join(APP_DIR, "summary_schema.json")
# 兜底路径不写死用户名：优先环境变量，其次用户主目录（避免泄露 Windows 账号名）
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                       "qwen-automation", "edge-copy")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")
HOME = "https://chat.qwen.ai/"
FALLBACK_MODEL = "Qwen3.8-Omni-Flash"
DEFAULT_PROMPT = "用2000字总结该视频"

# JSON 提示词的**前置说明**（这部分对用户开放自定义；JSON 结构部分固定来自 summary_schema.json）
DEFAULT_PREAMBLE = (
    "请完整观看或收听这段视频，然后严格按照下面的 JSON 结构输出总结。\n"
    "总结总长度约 2000 字。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象本体：不要 markdown 代码块、不要任何解释文字或前后缀。\n"
    "2. 字段名、层级、顺序必须与给定结构完全一致，不得增删字段。\n"
    "3. 「内容类型」从 数码测评/知识科普/访谈对话/教程教学/杂谈闲聊/游戏/生活Vlog/其他 里选一个。\n"
    "4. 「结论」80 字以内，开篇即给观众的核心判断与推荐。\n"
    "5. 「核心要点」5-8 条，每条 30 字以内。\n"
    "6. 「争议或不足」只写视频明确吐槽或指出的问题，没有就留空数组。\n"
    "7. 「分段摘要」按时间轴从前往后，8-15 段，尽量覆盖全片；时间点用 mm:ss 或 mm:ss-mm:ss；"
    "每段「主题」10 字以内、「要点」60 字以内。\n"
    "8. 「关键词」5-10 个。\n"
    "9. 除「分段摘要」是对象数组外，其余数组的每一项都是字符串。\n"
    "10. 只写视频里真实出现的内容，不要编造；视频没提到的信息用空字符串或空数组。"
)

_FALLBACK_SCHEMA = {
    "视频标题": "string", "UP主": "string", "时长": "string",
    "结论": "string", "核心要点": ["string"],
    "争议或不足": ["string"],
    "分段摘要": [{"时间点": "mm:ss", "主题": "string", "要点": "string"}],
    "关键词": ["string"],
}


def load_schema() -> dict:
    """读总结格式契约（summary_schema.json）。文件坏了就退回内置兜底结构。

    查找顺序：exe/脚本旁边的（用户可改）→ 打包资源区 _MEIPASS → 内置兜底。
    """
    for cand in (SCHEMA_FILE, os.path.join(RES_DIR, "summary_schema.json")):
        try:
            with open(cand, encoding="utf-8") as f:
                d = json.load(f)
            clean = {k: v for k, v in d.items() if not k.startswith("_")}
            if clean:
                return clean
        except Exception:
            continue
    return dict(_FALLBACK_SCHEMA)


def build_prompt(schema: dict, preamble: str | None = None) -> str:
    """拼出完整提示词 = 用户可自定义的前置说明 + 固定的 JSON 结构。

    JSON 结构部分来自 summary_schema.json，**不对用户开放**（它是解析的依据）；
    开放的是前置说明（口径、字数、侧重等）。
    """
    pre = (preamble or DEFAULT_PREAMBLE).strip() or DEFAULT_PREAMBLE
    body = json.dumps(schema, ensure_ascii=False, indent=2)
    return pre + "\n\nJSON 结构：\n" + body


# 同一个持久 profile **不能同时跑两个浏览器**：第二个 Edge 一启动就发现
# profile 被占、立即退出（playwright 报 TargetClosedError）。曾经踩过的坑：
# GUI 启动时的登录检测与「开始总结」几乎同时发起，视频又是缓存秒下 →
# 两个 launch 撞车。所有 playwright 入口都必须持锁。
_BROWSER_LOCK = threading.RLock()


def log(*a):
    """打印进度；同时转发给外部注册的钩子（GUI 用它做实时进度显示）；
    并落盘到 <数据区>/logs/ —— **exe 是 --windowed 的，stdout 会被丢弃**，
    没有这个文件日志，打包版出问题就完全无法诊断。"""
    msg = " ".join(str(x) for x in a)
    for h in list(_LOG_HOOKS):
        try:
            h(msg)
        except Exception:
            pass
    try:
        print(msg, flush=True)
    except Exception:
        pass                       # stdout 可能是关闭的管道/空写入器
    try:
        os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
        with open(os.path.join(APP_DIR, "logs", "engine-%s.log" % time.strftime("%Y%m%d")),
                  "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


_LOG_HOOKS: list = []


def add_log_hook(fn):
    _LOG_HOOKS.append(fn)


def remove_log_hook(fn):
    try:
        _LOG_HOOKS.remove(fn)
    except ValueError:
        pass


# ---------------- 1) 下载 ----------------
def _download_tag_path(mp4: str) -> str:
    """画质标记文件：记录这个 mp4 是用哪档画质下出来的（扁平目录下防"复用错画质"）。"""
    return os.path.splitext(mp4)[0] + ".下载信息.json"


def download(url: str, outdir: str, quality: str, audio: str, force: bool = False) -> str:
    """调 bili_dl.py 下载，返回成品 mp4 路径。视频**直接落在 outdir（扁平，不按画质分目录）**。

    扁平化之后的画质防护：tmp 里同名的 mp4 可能是上一次**别的画质**跑出来的，
    所以成功下载后写 `<名>.下载信息.json` 记录本次画质；再遇到"已存在"时读它比对——
    画质不一致就自动加 --force 重下，**绝不把错画质的文件拿去上传**。
    """
    os.makedirs(outdir, exist_ok=True)
    # exe 模式：没有独立 Python 也没有 bili_dl.py 源文件 —— 让 exe 自身
    # 以 --bili-dl-worker 身份重入（入口脚本会转调 bili_dl.main()）。
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--bili-dl-worker", url, "-o", outdir]
    else:
        cmd = [sys.executable, BILI_DL, url, "-o", outdir]
    if quality:
        cmd += ["-q", quality]
    if audio:
        cmd += ["--audio-quality", audio]
    if force:
        cmd += ["--force"]
    log("[1/3] 下载：", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    blob = (p.stdout or "") + (p.stderr or "")
    log("\n".join(blob.strip().splitlines()[-6:]))
    if p.returncode != 0:
        if "已存在" in blob:
            m0 = re.search(r"^输出：(.*)$", blob, re.M)
            cand = m0.group(1).strip() if m0 else ""
            tag = _download_tag_path(cand) if cand else ""
            prev = {}
            if tag and os.path.isfile(tag):
                try:
                    with open(tag, encoding="utf-8") as f:
                        prev = json.load(f)
                except Exception:
                    prev = {}
            if prev.get("quality") != quality or prev.get("audio") != audio:
                log(f"    已有文件是 {prev.get('quality') or '?'}/{prev.get('audio') or '?'} 画质，"
                    f"与本次 {quality}/{audio} 不一致 → 自动加 --force 重新下载")
                return download(url, outdir, quality, audio, force=True)
            log("    目标文件已存在且画质一致，直接复用（要重下加 --force）")
        else:
            raise SystemExit("下载失败：\n" + blob[-800:])
    # 优先用下载脚本自己报出来的路径
    m = re.search(r"^输出：(.*)$", blob, re.M)
    cand = m.group(1).strip() if m else ""
    if cand and os.path.isfile(cand):
        try:  # 记录画质（cleanup 时随视频一起删掉）
            with open(_download_tag_path(cand), "w", encoding="utf-8") as f:
                json.dump({"quality": quality, "audio": audio, "url": url},
                          f, ensure_ascii=False, indent=2)
        except OSError as e:
            log(f"[提示] 画质标记写入失败：{e}")
        return cand
    mp4s = [os.path.join(outdir, f) for f in os.listdir(outdir)
            if f.lower().endswith(".mp4") and "merged" not in f]
    if not mp4s:
        raise SystemExit("下载完成但没找到 mp4")
    mp4s.sort(key=os.path.getmtime)
    return mp4s[-1]


# ---------------- 2) 上传 + 提问 + 取回 ----------------
CARD_JS = r"""
() => {
  const c = document.querySelector('.file-card-list');
  if (!c) return {present: false};
  const h = c.outerHTML;
  return {present: true, loading: /loading|spin|uploading|progress/i.test(h),
          hasImg: /<img|thumbnail|cover/i.test(h)};
}
"""


def extract_answer(msg: dict) -> str:
    """从 assistant 消息里取出「正文」。

    关键：接口把思考过程和正文分开放在 content_list 里，靠 phase 区分：
      phase='think'  → 思考过程（可能是英文，很长）
      phase='answer' → 真正的回答
    content 字段常常是空的，所以不能直接用它；更不能把 content_list 全部拼起来，
    否则会把思考过程当成总结。
    """
    c = msg.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip()
    cl = [x for x in (msg.get("content_list") or []) if isinstance(x, dict)]
    if not cl:
        return ""
    ans = [str(x.get("content") or "") for x in cl if str(x.get("phase")) == "answer"]
    if not ans:
        ans = [str(x.get("content") or "") for x in cl if str(x.get("phase")) != "think"]
    if not ans:
        ans = [str(x.get("content") or "") for x in cl]
    return "\n\n".join(t for t in ans if t.strip()).strip()


def ask_qwen(mp4: str, prompt: str, model: str | None, wait_answer: int = 1800) -> str:
    from playwright.sync_api import sync_playwright

    log("[2/3] 启动浏览器自动化（headless Edge）…")
    with _BROWSER_LOCK, sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                PROFILE, channel="msedge", headless=True,
                viewport={"width": 1440, "height": 1000}, locale="zh-CN",
                timezone_id="Asia/Shanghai", user_agent=UA,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            log("[错误] 浏览器启动失败：", type(e).__name__, str(e)[:300])
            raise
        log("    浏览器已启动，打开页面…")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        oss: dict = {}

        def on_resp(r):
            try:
                if "aliyuncs.com" in r.url and r.request.method == "PUT":
                    oss["put"] = r.status
                if "getstsToken" in r.url:
                    oss["sts"] = r.status
            except Exception:
                pass

        page.on("response", on_resp)
        page.goto("https://chat.qwen.ai/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(9000)

        body = page.inner_text("body")[:150]
        if "登录" in body and "注册" in body:
            ctx.close()
            raise SystemExit("登录态已失效，请用同一个 profile 重新登录一次后重试：\n  " + PROFILE)

        # WAF 滑块检测（阿里风控会随机弹「访问验证」覆盖层，把整页挡死）
        try:
            b = page.inner_text("body")
            for marker in ("访问验证", "waf_nc", "请稍后重试"):
                if marker in b:
                    log("[警告] 页面出现风控标记「%s」——上传/发送可能被滑块挡住" % marker)
                    break
        except Exception:
            pass

        # 模型：必须切到指定的多模态模型（默认 Qwen3.8-Omni-Flash），切不中就报错退出，
        # 否则会像之前那样默默用默认的纯文本模型，视频根本读不进去。
        if model:
            cur = ""
            try:
                cur = page.locator("div.wms-trigger__text").first.inner_text().strip()
                if cur != model:
                    page.locator("div.wms-trigger").first.click(timeout=10000)
                    page.wait_for_timeout(2500)
                    opt = page.locator(f"text={model}").first
                    if opt.count() > 0:
                        opt.click(timeout=8000)
                        page.wait_for_timeout(2500)
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(800)
                cur = page.locator("div.wms-trigger__text").first.inner_text().strip()
            except Exception as e:
                log("选模型异常：", str(e)[:150])
            if cur != model:
                log("[错误] 模型切换失败：期望「%s」，实际「%s」" % (model, cur))
                ctx.close()
                raise SystemExit(f"模型切换失败：期望「{model}」，实际「{cur}」")
            log("[2/3] 模型：", cur, "✅")

        # 上传（必须走「选择模式 → 上传附件」）
        log("[2/3] 上传视频：", os.path.basename(mp4),
            round(os.path.getsize(mp4) / 1024 / 1024, 1), "MB")
        t0 = time.time()
        try:
            page.locator("div.mode-select-open").first.click(timeout=10000)
            page.wait_for_timeout(1800)
            with page.expect_file_chooser(timeout=12000) as fci:
                page.locator("[role=menuitem]", has_text="上传附件").first.click(timeout=10000)
            fci.value.set_files(mp4)
        except Exception as e:
            log("[错误] 上传入口失败：", type(e).__name__, str(e)[:300])
            ctx.close()
            raise SystemExit(f"上传入口失败：{type(e).__name__} {e}")

        while time.time() - t0 < 1200 and oss.get("put") is None:
            page.wait_for_timeout(5000)
            el = int(time.time() - t0)
            if el % 30 < 5:
                log(f"    上传中… {el}s（STS={oss.get('sts')}）")
        log(f"    OSS PUT = {oss.get('put')}（耗时 {int(time.time()-t0)}s）")

        # 等服务端解析
        log("    等待服务端解析…")
        t1, streak = time.time(), 0
        while time.time() - t1 < 900:
            page.wait_for_timeout(5000)
            try:
                st = page.evaluate(CARD_JS)
            except Exception:
                continue
            if st.get("present") and not st.get("loading") and (st.get("hasImg") or time.time() - t1 > 25):
                streak += 1
                if streak >= 3:
                    log(f"    解析完成（{int(time.time()-t1)}s）")
                    break
            else:
                streak = 0

        # 发送
        log("[3/3] 发送提问：", (prompt[:60] + "…") if len(prompt) > 60 else prompt,
            f"（共 {len(prompt)} 字）")
        for attempt in (1, 2):
            try:
                ta = page.locator("textarea").first
                ta.click(timeout=10000)
                page.wait_for_timeout(400)
                # 用 insert_text 而不是逐字 type：
                #   1) 快（1200 字的 JSON 提示词逐字打要几十秒）
                #   2) 关键——type() 遇到提示词里的换行会按 Enter，把消息提前发出去
                #      insert_text 只派发 input 事件，换行是普通字符，不会触发发送
                try:
                    page.keyboard.insert_text(prompt)
                except Exception:
                    ta.type(" ".join(prompt.split()), delay=5)
                page.wait_for_timeout(1200)
                page.keyboard.press("Enter")
            except Exception as e:
                log("    输入异常：", str(e)[:120])
            page.wait_for_timeout(9000)
            if re.search(r"/c/[0-9a-f\-]{20,}", page.url):
                break
            log(f"    第 {attempt} 次未进入会话页，重试…")

        # A/B 面板
        try:
            if "更喜欢哪个回复" in page.inner_text("body"):
                log("    检测到 A/B 面板，点「跳过」…")
                for _ in range(2):
                    b = page.locator("text=跳过").first
                    if b.count() > 0:
                        b.click(timeout=5000)
                        page.wait_for_timeout(2500)
        except Exception:
            pass

        m = re.search(r"/c/([0-9a-f\-]{20,})", page.url)
        chat_id = m.group(1) if m else ""
        log("    会话：", page.url)
        if not chat_id:
            ctx.close()
            return ""

        import requests
        cook = ctx.cookies("https://chat.qwen.ai")
        s = requests.Session()
        s.cookies.update({c["name"]: c["value"] for c in cook})
        H = {"user-agent": UA, "accept": "application/json", "referer": page.url,
             "source": "web", "version": "0.2.91"}
        log("    等待生成…")
        answer, t2, used_model, done_hits = "", time.time(), "", 0
        while time.time() - t2 < wait_answer:
            page.wait_for_timeout(12000)
            try:
                r = s.get(f"https://chat.qwen.ai/api/v2/chats/{chat_id}", headers=H, timeout=60)
                if r.status_code == 200:
                    ch = ((r.json().get("data") or {}).get("chat")) or {}
                    msgs = [m for m in (ch.get("messages") or []) if m.get("role") == "assistant"]
                    if not msgs:
                        continue
                    last = msgs[-1]
                    if last.get("model"):
                        used_model = str(last["model"])
                    cur = extract_answer(last)
                    if cur and cur != answer:
                        answer = cur
                        log(f"    [{int(time.time()-t2)}s] 已取到 {len(answer)} 字（模型 {used_model}）")
                    if answer and last.get("done"):
                        break
                    # 生成已结束却拿不到 answer 段（例如模型只输出思考/拒答）：
                    # 退化取全部文本，避免白等满 wait_answer 分钟
                    done_hits = done_hits + 1 if last.get("done") else 0
                    if done_hits >= 2:
                        parts = [str(x.get("content") or "") for x in (last.get("content_list") or [])
                                 if isinstance(x, dict)]
                        answer = "\n\n".join(p for p in parts if p.strip()).strip()
                        log(f"    生成结束但没有 answer 段，退化取全部文本（{len(answer)} 字）")
                        break
            except Exception as e:
                log("    读接口异常：", str(e)[:120])
        if used_model:
            log("    实际使用的模型：", used_model)
        ctx.close()
        return answer


def cleanup_video(mp4: str, keep: bool = False) -> list:
    """上传成功后清掉视频本体与中间流，只保留 `.总结.json` / `.总结.md`。

    只删「本次这条视频」的文件：`<名>.mp4` 和 `<名>.*.m4s*`；
    凡是带 `.总结.` 的一律不碰。
    ⚠️ 只在「下载 + 上传 + 取回总结」全部成功后才调用 —— 失败时留着文件才好重试。
    """
    if keep or not mp4:
        return []
    d = os.path.dirname(mp4) or "."
    base = os.path.splitext(os.path.basename(mp4))[0]
    victims = [mp4]
    for p in glob.glob(os.path.join(d, base + ".*")):
        n = os.path.basename(p)
        if ".总结." in n:
            continue                       # 成果，永远不删
        if ".m4s" in n or n.endswith(".part.json") or n.endswith(".下载信息.json"):
            victims.append(p)
    deleted = []
    for v in dict.fromkeys(victims):
        try:
            if os.path.isfile(v):
                os.remove(v)
                deleted.append(v)
        except OSError as e:
            log(f"[警告] 删除失败 {os.path.basename(v)}：{e}")
    if deleted:
        log(f"已清理 {len(deleted)} 个视频/中间文件（.总结.json / .总结.md 保留）")
    return deleted


def _launch(playwright, headless=True):
    """统一的浏览器启动参数（真实 Edge 内核 + 持久 profile）。"""
    return playwright.chromium.launch_persistent_context(
        PROFILE, channel="msedge", headless=headless,
        viewport={"width": 1440, "height": 1000}, locale="zh-CN",
        timezone_id="Asia/Shanghai", user_agent=UA,
        args=["--disable-blink-features=AutomationControlled"],
    )


def read_token(page) -> str:
    """登录令牌在 localStorage 的 `token` 字段里（**不是 cookie**，浏览器设置页也看不到）。"""
    try:
        return page.evaluate("() => localStorage.getItem('token')") or ""
    except Exception:
        return ""


def _verify_token(token: str):
    """拿 token 问一次账号接口，返回 (是否有效, 账号名)。"""
    if not token:
        return False, ""
    try:
        import requests
        r = requests.get("https://chat.qwen.ai/api/v2/auths", timeout=30,
                         headers={"Authorization": f"Bearer {token}", "user-agent": UA,
                                  "accept": "application/json", "referer": HOME,
                                  "source": "web", "version": "0.2.91"})
        if r.status_code != 200:
            return False, ""
        d = r.json() or {}
        d = d.get("data") if isinstance(d.get("data"), dict) else d
        who = d.get("name") or d.get("email") or d.get("username") or d.get("id") or "已登录"
        return True, str(who)
    except Exception:
        return False, ""


def check_login(headless: bool = True):
    """检查登录态是否还有效，返回 (是否登录, 账号名)。

    登录态过期、或换台机器首次使用时，就是靠这个检测出来，再引导用户去登录。
    """
    from playwright.sync_api import sync_playwright
    with _BROWSER_LOCK, sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            return _verify_token(read_token(page))
        except Exception as e:  # noqa: BLE001
            log("[提示] 登录检测异常：", str(e)[:150])
            return False, ""
        finally:
            ctx.close()


def open_login_window(wait_seconds: int = 900, on_tick=None):
    """打开一个**可见的** Edge 窗口让用户自己登录，登录成功即自动返回。

    - 用的是自动化专用的独立 profile，不碰用户平时的 Edge 数据；
    - 全程不需要用户提供密码给程序，就是在一个正常浏览器窗口里登录；
    - 用户中途关掉窗口也能正常返回（不会卡住）。
    返回 (是否成功, 账号名)。
    """
    from playwright.sync_api import sync_playwright
    ok, who = False, ""
    with _BROWSER_LOCK, sync_playwright() as p:
        ctx = _launch(p, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
            t0 = time.time()
            while time.time() - t0 < wait_seconds:
                page.wait_for_timeout(4000)
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                tok = read_token(page)
                if tok:
                    good, name = _verify_token(tok)
                    if good:
                        ok, who = True, name
                        break
                if on_tick:
                    on_tick(f"等待登录…（已等 {int(time.time()-t0)} 秒）")
            if on_tick:
                on_tick(f"登录{'成功：' + who if ok else '未完成'}")
        except Exception as e:  # noqa: BLE001
            log("[提示] 登录窗口异常：", str(e)[:150])
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return ok, who


def list_models(headless: bool = True):
    """读出页面上「当前可选」的模型名。

    注意：**动态从页面读**，而不是在代码里写死。这样千问哪天换了模型（下线/新增），
    界面上的下拉会自动跟着变，不需要改代码。
    """
    from playwright.sync_api import sync_playwright
    names = []
    with _BROWSER_LOCK, sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)
            page.locator("div.wms-trigger").first.click(timeout=15000)
            page.wait_for_timeout(2500)
            items = page.evaluate(r"""
            () => {
              const out = [];
              document.querySelectorAll('[role=option], .wms-list__item').forEach(e => {
                const nameEl = e.querySelector('.wms-list__name, .wms-list__name-text');
                const t = (nameEl ? nameEl.innerText : (e.innerText || ''))
                            .trim().split('\n')[0].trim();
                if (t && t.length < 40) out.push(t);
              });
              return out;
            }
            """)
            for t in items:
                if t not in names:
                    names.append(t)
        except Exception as e:  # noqa: BLE001
            log("[提示] 读取模型列表失败：", str(e)[:150])
        finally:
            ctx.close()
    return names


def probe_account_and_models(headless: bool = True, on_tick=None):
    """开**一次**浏览器，同时检查登录态并读取可用模型（比分开查省一半时间）。

    返回 (是否登录, 账号名, 模型列表)。
    """
    from playwright.sync_api import sync_playwright
    ok, who, names = False, "", []
    with _BROWSER_LOCK, sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            ok, who = _verify_token(read_token(page))
            if on_tick:
                on_tick(f"登录态：{'已登录（' + who + '）' if ok else '未登录或已过期'}")
            # 未登录时下拉也能看到，但登录后列表更全，所以放在登录检测之后
            page.locator("div.wms-trigger").first.click(timeout=15000)
            page.wait_for_timeout(2500)
            items = page.evaluate(r"""
            () => {
              const out = [];
              document.querySelectorAll('[role=option], .wms-list__item').forEach(e => {
                const nameEl = e.querySelector('.wms-list__name, .wms-list__name-text');
                const t = (nameEl ? nameEl.innerText : (e.innerText || ''))
                            .trim().split('\n')[0].trim();
                if (t && t.length < 40) out.push(t);
              });
              return out;
            }
            """)
            for t in items:
                t = t.strip().split("\n")[0].strip()
                if t and t not in names:
                    names.append(t)
            if on_tick:
                on_tick("可用模型：" + ("、".join(names) or "读取失败"))
        except Exception as e:  # noqa: BLE001
            log("[提示] 探测异常：", str(e)[:150])
        finally:
            ctx.close()
    return ok, who, names


def run_selfcheck(do_online: bool = True, on_tick=None) -> list:
    """环境自检：ffmpeg / Edge / 目录 / （可选）千问登录态与模型列表。

    给"打包给别人用"的场景兜底：别人跑不起来时，点一下就知道缺什么。
    返回 [(项目, 是否通过, 说明)]。只有 ffmpeg / Edge / 目录三项是离线的，
    联网部分（登录态、模型）可关掉以加速。
    """
    from bili_dl import find_ffmpeg
    res = []

    ff = find_ffmpeg(None)
    res.append(("ffmpeg 合流工具", bool(ff),
                ff or "未找到 → 视频下载后无法自动合流（会留下分离的音视频）"))

    edge = None
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(c):
            edge = c
            break
    res.append(("Edge 浏览器（自动化内核）", bool(edge),
                edge or "未找到 → 无法自动化千问网页（Win10/11 一般自带）"))

    for d, name in ((SUMMARIES_DIR, "成果目录 summaries/"), (TMP_DIR, "临时目录 tmp/")):
        try:
            os.makedirs(d, exist_ok=True)
            res.append((name, True, d))
        except OSError as e:
            res.append((name, False, f"无法创建：{e}"))

    if do_online:
        if on_tick:
            on_tick("正在检测千问登录态与可用模型…（要开一次浏览器，约 30 秒）")
        ok, who, ms = probe_account_and_models(headless=True, on_tick=on_tick)
        res.append(("千问登录态", ok,
                    f"已登录：{who}" if ok else "未登录或已过期 → 点「登录 / 重新登录」"))
        res.append(("可用模型（页面实时读）", bool(ms),
                    "、".join(ms) or "读取失败 → 可在模型框里手输模型 id"))
    return res


def main():
    ap = argparse.ArgumentParser(description="B站视频 → 下载 → Qwen 网页端总结")
    ap.add_argument("target", help="B站链接或 BV 号")
    ap.add_argument("-o", "--out", default=TMP_DIR,
                    help="视频临时目录（默认 ./tmp，扁平存放）")
    ap.add_argument("--prompt", default=None,
                    help="自定义 JSON 提示词的**前置说明**（JSON 结构固定来自 summary_schema.json）")
    ap.add_argument("--no-json", action="store_true",
                    help="不走 JSON 格式：把 --prompt 原样作为整个提示词（默认“用2000字总结该视频”）")
    ap.add_argument("--model", default="Qwen3.8-Omni-Flash",
                    help="模型名（默认 Qwen3.8-Omni-Flash，网页端唯一支持视听输入的模型）")
    ap.add_argument("--quality", default="360P", help="画质上限（默认 360P）")
    ap.add_argument("--audio-quality", default="64K", help="音频档位（默认 64K）")
    ap.add_argument("--skip-download", action="store_true", help="跳过下载，直接用 --video")
    ap.add_argument("--force", action="store_true", help="目标文件已存在时重新下载覆盖")
    ap.add_argument("--video", default=None, help="已有视频路径（配合 --skip-download）")
    ap.add_argument("--keep-parts", action="store_true",
                    help="保留视频与中间文件（默认在上传成功后自动清理，只留总结）")
    a = ap.parse_args()

    if a.skip_download:
        mp4 = a.video
        if not mp4 or not os.path.isfile(mp4):
            raise SystemExit("--skip-download 需要 --video 指定存在的文件")
    else:
        mp4 = download(a.target, a.out, a.quality, a.audio_quality, a.force)

    if a.no_json:
        prompt = a.prompt or DEFAULT_PROMPT
    else:
        prompt = build_prompt(load_schema(), a.prompt)
    answer = ask_qwen(mp4, prompt, a.model)
    if not answer:
        log("[提示] 没拿到总结，视频与中间文件已保留，可直接重跑（会复用已下载的文件）")
        raise SystemExit("未取到总结（可能生成超时或触发风控）")

    out_md = os.path.join(SUMMARIES_DIR, os.path.splitext(os.path.basename(mp4))[0] + ".总结.md")
    out_json = os.path.join(SUMMARIES_DIR, os.path.splitext(os.path.basename(mp4))[0] + ".总结.json")
    os.makedirs(SUMMARIES_DIR, exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(answer)
    try:                       # 顺便存一份结构化 JSON（对齐 UI 的产物）
        obj = None
        t = answer.strip()
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(t[i:j + 1])
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(obj or {"原始回答": answer}, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log(f"[提示] JSON 落盘失败（不影响 Markdown）：{e}")
    log(f"\n完成：{out_md}（{len(answer)} 字）\n")
    log(answer[:300])

    # 收尾：上传并取回成功 → 清掉视频与中间流，只留总结
    cleanup_video(mp4, keep=a.keep_parts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
