#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_dl.py —— 轻量 B站视频下载器（单文件，零第三方依赖，纯 Python 标准库）

算法与参数借鉴自开源项目 lanyeeee/bilibili-video-downloader 的 Rust 实现：
  · 接口与请求头        src-tauri/src/bili_client.rs (USER_AGENT / REFERRER / get_cookie)
  · 取视频信息          bili_client.rs  get_normal_info   -> /x/web-interface/view
  · 取播放地址          bili_client.rs  get_normal_url    -> /x/player/wbi/playurl
                        （注：该项目仅对 UP主投稿列表使用 WBI 签名，下载接口不需要）
  · 分片下载            download_chunk_task.rs + get_media_chunk
                        Range: bytes=start-end，只带 UA + Referer，不带 Cookie
  · 合流                tasks/video_process_task.rs  merge()
                        ffmpeg -i v -i a -c copy -map 0:v:0 -map 1:a:0 out.mp4 -y

用法：
  python bili_dl.py "<B站链接或BV号>"                # 下载并合流成 mp4（默认 360P + 64K）
  python bili_dl.py "<链接>" --list                  # 只列出可用画质，不下载
  python bili_dl.py "<链接>" -o ./tmp -q 1080P -a 192K   # 临时要高清（覆盖默认）
  python bili_dl.py --merge-only --video a.mp4 --audio b.m4a   # 只把已有两条流合流

常用参数：
  -o/--out        输出目录（默认 ./tmp）
  -q/--quality    画质上限：默认 360P；可填 240P/480P/720P/1080P/1080P60/4K/8K/HDR/杜比视界，或 best 取最高
  -a/--audio-quality  音频档位上限：默认 64K；可填 132K/192K/杜比全景声/Hi-Res，或 best 取最高
  -p/--page       分P 序号（从 1 开始，默认 1）
  -j/--workers    分片并发数（默认 8）
  -c/--chunk-mb   分片大小 MB（默认 2，与该项目一致）
  --sessdata      登录 Cookie 的 SESSDATA 值（高清/大会员需要）
  --from-app-config  从该项目已有的 config.json 读取 SESSDATA（默认开启）
  --ffmpeg        指定 ffmpeg 可执行文件；不指定则自动查找
  --keep-parts    合流后保留中间文件（默认保留；加 --clean-parts 才删除）
  --force         目标文件已存在时覆盖
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─────────────────────────── 常量（取自 Rust 源码） ───────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
REFERRER = "https://www.bilibili.com/"

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"

# 画质 id（B站定义，与项目 VideoQuality 枚举一致）
QN_NAME = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P+", 100: "智能修复", 80: "1080P", 74: "720P60", 64: "720P",
    32: "480P", 16: "360P", 6: "240P",
}
NAME_QN = {v: k for k, v in QN_NAME.items()}

# 音频品质 id
AUDIO_QN_NAME = {
    30280: "192K", 30232: "132K", 30216: "64K",
    30250: "杜比全景声", 30251: "Hi-Res",
}
NAME_AUDIO = {v: k for k, v in AUDIO_QN_NAME.items()}

# 优先顺序：高清优先，与项目 config.rs 默认一致
QUALITY_ORDER = [127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16, 6]
# 编码优先：AVC > HEVC > AV1（与项目 config.rs 一致）
CODEC_ORDER = ["avc", "hev", "av01"]
AUDIO_ORDER = [30251, 30250, 30280, 30232, 30216]

# 用户指定：默认一律用 360P + 64K 下载（省流量、够看够听）；要高清用 -q best / -a best 临时覆盖
DEFAULT_QUALITY = "360P"
DEFAULT_AUDIO = "64K"
# 视频临时存放点（按画质分子目录）；上传成功后由 bili_qwen_summary 清理
DEFAULT_OUT = "./tmp"

DEFAULT_CHUNK = 2 * 1024 * 1024  # 2MB，与项目分片一致
APP_CONFIG_CANDIDATES = [
    Path(os.environ.get("APPDATA", "")) / "com.lanyeeee.bilibili-video-downloader" / "config.json",
]


# ─────────────────────────────── HTTP 基础 ───────────────────────────────

class BiliError(Exception):
    pass


class Client:
    """极简 HTTP 客户端：统一带上 UA / Referer，可选 Cookie。带简单重试。"""

    def __init__(self, sessdata: str = "", retries: int = 3, timeout: int = 15):
        self.sessdata = (sessdata or "").strip().rstrip(";")
        self.retries = retries
        self.timeout = timeout
        self._opener = urllib.request.build_opener()

    def headers(self, with_cookie: bool = True, extra: dict | None = None) -> dict:
        h = {"User-Agent": USER_AGENT, "Referer": REFERRER}
        if with_cookie and self.sessdata:
            h["Cookie"] = f"SESSDATA={self.sessdata}"
        if extra:
            h.update(extra)
        return h

    def open(self, url: str, with_cookie: bool = True, extra_headers: dict | None = None,
             method: str = "GET", timeout: int | None = None):
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                url, method=method, headers=self.headers(with_cookie, extra_headers)
            )
            try:
                return self._opener.open(req, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as e:
                # 4xx 一般不重试（429 除外）
                if e.code == 429 or e.code >= 500:
                    last = e
                else:
                    raise BiliError(f"HTTP {e.code} {e.reason} — {url[:120]}") from e
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(0.5 * (attempt + 1) + random.random() * 0.3)
        raise BiliError(f"请求失败（已重试{self.retries}次）：{url[:120]} → {last}")

    def get_json(self, url: str, params: dict | None = None, with_cookie: bool = True) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        with self.open(url, with_cookie=with_cookie) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise BiliError(f"响应不是合法 JSON：{body[:200]}") from e
        code = data.get("code", -1)
        if code != 0:
            msg = data.get("message") or data.get("msg") or ""
            hint = ""
            if code == -404:
                hint = "（视频不存在）"
            elif code == -403:
                hint = "（访问被拒绝，可能需要登录态或该内容受限）"
            raise BiliError(f"接口返回 code={code} {msg}{hint}")
        return data.get("data") or {}


# ─────────────────────────────── 业务逻辑 ───────────────────────────────

def parse_bvid(text: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", text):
        raise BiliError("看起来是 av 号，请换成 BV 号或完整链接")
    raise BiliError(f"从输入里找不到 BV 号：{text}")


def fetch_info(client: Client, bvid: str) -> dict:
    """取视频信息：标题、cid、分P 列表、UP主、时长。"""
    return client.get_json(API_VIEW, {"bvid": bvid}, with_cookie=False)


def fetch_playurl(client: Client, bvid: str, cid: int) -> dict:
    """取播放地址。qn=127 + fnval=4048 = 尽可能把所有可用格式都要回来。"""
    return client.get_json(
        API_PLAYURL, {"bvid": bvid, "cid": cid, "qn": 127, "fnval": 4048}, with_cookie=True
    )


def codec_rank(codec: str) -> int:
    low = (codec or "").lower()
    for i, tag in enumerate(CODEC_ORDER):
        if low.startswith(tag):
            return i
    return len(CODEC_ORDER)


def pick_streams(dash: dict, quality_cap: int | None,
                 audio_cap: int | None = None) -> tuple[dict, dict]:
    """按画质优先 + 编码优先挑出视频流与音频流。

    quality_cap / audio_cap 都是"上限"，None 表示不限制（取最高可用）。
    注意：音频档位**不是**按 id 数值单调的（Hi-Res/杜比的 id 反而更小），
    所以必须走显式排序表 AUDIO_ORDER，不能比大小。
    """
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos:
        raise BiliError("该视频没有 DASH 视频流（可能是互动视频/付费内容，或需要登录态）")

    allow = [q for q in QUALITY_ORDER if quality_cap is None or q <= quality_cap]
    rank = {q: i for i, q in enumerate(allow)}

    cand = [v for v in videos if v.get("id") in rank]
    if not cand:
        cand = videos  # 兜底：全都不在允许列表里就直接用返回的
        rank = {}
    pick_v = min(cand, key=lambda v: (rank.get(v.get("id"), 999), codec_rank(v.get("codecs"))))

    # 音频：截取"不高于上限"的那一段，再按品质从高到低取第一条存在的
    if audio_cap is None or audio_cap not in AUDIO_ORDER:
        audio_order = AUDIO_ORDER
    else:
        audio_order = AUDIO_ORDER[AUDIO_ORDER.index(audio_cap):]
    pick_a = None
    for q in audio_order:
        same = [a for a in audios if a.get("id") == q]
        if same:
            pick_a = same[0]
            break
    if pick_a is None and audios:
        pick_a = audios[0]
    return pick_v, (pick_a or {})


def parse_quality_arg(value: str) -> int | None:
    """把 -q 的取值转成画质上限 id；best/auto/max 表示不限制。"""
    s = (value or "").strip()
    if not s or s.lower() in ("best", "auto", "max"):
        return None
    for name, qid in NAME_QN.items():
        if s.upper() == name.upper():
            return qid
    raise BiliError(f"未知画质：{value}（可选：{'/'.join(NAME_QN)}/best）")


def parse_audio_arg(value: str) -> int | None:
    """把 -a 的取值转成音频上限 id；best/auto/max 表示不限制。"""
    s = (value or "").strip()
    if not s or s.lower() in ("best", "auto", "max"):
        return None
    for name, qid in NAME_AUDIO.items():
        if s.upper() == name.upper():
            return qid
    raise BiliError(f"未知音频档位：{value}（可选：{'/'.join(NAME_AUDIO)}/best）")


def stream_size(stream: dict) -> int:
    for key in ("size", "sizeTotal"):
        if isinstance(stream.get(key), int) and stream[key] > 0:
            return stream[key]
    return 0  # dash 流通常不带长度，稍后用 HEAD / Range 探测


def resolve_total(client: Client, url: str) -> int:
    """探测媒体流总长度。与项目 get_content_length 同样的策略：
    先 HEAD 取 Content-Length，失败则 Range: bytes=0-0 取 Content-Range 里的总量。
    """
    try:
        with client.open(url, with_cookie=False, method="HEAD", timeout=20) as r:
            n = int(r.headers.get("Content-Length") or 0)
            if n > 0:
                return n
    except Exception:  # noqa: BLE001
        pass
    with client.open(url, with_cookie=False,
                     extra_headers={"Range": "bytes=0-0"}, timeout=20) as r:
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                return int(cr.rsplit("/", 1)[1])
            except ValueError:
                pass
        n = int(r.headers.get("Content-Length") or 0)
        if n > 0:
            return n
    raise BiliError("无法获知媒体流长度（HEAD 与 Range 探测都失败）")


def stream_url(stream: dict) -> str:
    url = stream.get("baseUrl") or stream.get("base_url")
    if not url:
        for backup in stream.get("backupUrl") or stream.get("backup_url") or []:
            if backup:
                url = backup
                break
    if not url:
        raise BiliError("该媒体流没有可用地址")
    return url


def safe_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len] or "video").strip()


def fmt_size(n: int) -> str:
    if not n:
        return "未知"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return f"{x:.1f}{units[i]}"


# ─────────────────────────────── 分片下载 ───────────────────────────────

def download_stream(client: Client, url: str, out: Path, total: int,
                    chunk_size: int, workers: int, resume: bool = True) -> None:
    """并发 Range 分片下载，可断点续传。

    与项目一致：只发 Range 请求、只带 UA+Referer，期望 206。
    """
    meta_path = out.with_suffix(out.suffix + ".part.json")
    if total <= 0:
        total = resolve_total(client, url)

    chunks = [(i, i * chunk_size, min(total - 1, (i + 1) * chunk_size - 1))
              for i in range((total + chunk_size - 1) // chunk_size)]

    done: set[int] = set()
    if resume and meta_path.exists() and out.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("total") == total and meta.get("chunk_size") == chunk_size:
                done = set(meta.get("completed") or [])
                print(f"    断点续传：已完成 {len(done)}/{len(chunks)} 个分片")
        except Exception:  # noqa: BLE001
            done = set()

    mode = "r+b" if (resume and out.exists()) else "wb"
    fh = open(out, mode)
    if mode == "wb":
        fh.truncate(total)
    lock = threading.Lock()
    written = [0]
    written[0] = sum(min(chunk_size, max(0, e - s + 1)) for i, s, e in chunks if i in done)
    t0 = time.time()

    def one(item):
        idx, start, end = item
        if idx in done:
            return idx, 0
        data = client.open(url, with_cookie=False,
                           extra_headers={"Range": f"bytes={start}-{end}"}, timeout=60).read()
        with lock:
            fh.seek(start)
            fh.write(data)
        return idx, len(data)

    pending = [c for c in chunks if c[0] not in done]
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(one, c) for c in pending]
            for fut in as_completed(futures):
                try:
                    idx, n = fut.result()
                except Exception as e:  # noqa: BLE001
                    raise BiliError(f"分片下载失败：{e}") from e
                done.add(idx)
                with lock:
                    written[0] += n
                    pct = written[0] * 100 / total
                    speed = written[0] / max(0.001, time.time() - t0)
                    sys.stdout.write(
                        f"\r    进度 {pct:5.1f}%  {fmt_size(written[0])}/{fmt_size(total)}"
                        f"  {fmt_size(int(speed))}/s   "
                    )
                    sys.stdout.flush()
    finally:
        fh.close()
        print()
        meta_path.write_text(
            json.dumps({"total": total, "chunk_size": chunk_size,
                        "completed": sorted(done)}, ensure_ascii=False),
            encoding="utf-8",
        )
    if len(done) != len(chunks):
        raise BiliError(f"分片未全部完成（{len(done)}/{len(chunks)}），重跑可继续")


# ─────────────────────────────── ffmpeg ───────────────────────────────

def find_ffmpeg(explicit: str | None) -> str | None:
    """按优先级找 ffmpeg：显式参数 → 环境变量 → PATH → 常见目录 → 该项目的边车文件。"""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        print(f"[警告] 指定的 ffmpeg 不存在：{explicit}，继续自动查找")
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cands = [Path(__file__).parent / "ffmpeg.exe", Path.cwd() / "ffmpeg.exe"]
    cands += sorted(local.glob("ffmpeg*/bin/ffmpeg.exe"))
    cands += sorted(local.glob("ffmpeg*/ffmpeg.exe"))
    # 该项目自带的 ffmpeg 边车（被改名的精简 ffmpeg，只读不改）
    for root in (Path(__file__).parent, Path(__file__).parent.parent):
        cands += sorted(root.glob("**/com.lanyeeee.bilibili-video-downloader-ffmpeg*.exe"))
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def merge_with_ffmpeg(ffmpeg: str, video: Path, audio: Path, out: Path):
    """与项目 merge() 完全相同的参数：-c copy -map 0:v:0 -map 1:a:0"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(audio),
           "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
           str(out), "-y"]
    creationflags = 0x08000000 if os.name == "nt" else 0  # 隐藏控制台窗口
    proc = subprocess.run(cmd, capture_output=True, creationflags=creationflags)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:600]
        raise BiliError(f"ffmpeg 合流失败（exit {proc.returncode}）：{err}")


# ─────────────────────────────── 配置读取 ───────────────────────────────

def load_sessdata(args) -> str:
    if args.sessdata:
        return args.sessdata
    if not args.from_app_config:
        return ""
    for path in APP_CONFIG_CANDIDATES:
        try:
            if path and path.is_file():
                cfg = json.loads(path.read_text(encoding="utf-8"))
                val = (cfg.get("sessdata") or "").strip()
                if val:
                    print(f"[信息] 已从已有配置读取登录态：{path}（未登录时只能拿低画质）")
                    return val
        except Exception:  # noqa: BLE001
            continue
    return ""


# ─────────────────────────────── 主流程 ───────────────────────────────

def cmd_list(client: Client, bvid: str, page: int):
    info = fetch_info(client, bvid)
    pages = info.get("pages") or []
    if page < 1 or page > len(pages):
        raise BiliError(f"分P 序号超出范围（该视频共 {len(pages)} P）")
    cid = pages[page - 1]["cid"]
    print(f"标题：{info.get('title')}")
    print(f"UP主：{(info.get('owner') or {}).get('name')}   分P：{len(pages)}   第 {page} P：{pages[page-1].get('part')}")
    play = fetch_playurl(client, bvid, cid)
    dash = play.get("dash") or {}
    print(f"\n可用视频流（共 {len(dash.get('video') or [])} 条）：")
    for v in sorted(dash.get("video") or [], key=lambda x: -x.get("id", 0)):
        print(f"  {QN_NAME.get(v.get('id'), v.get('id')):<10} {v.get('codecs','?'):<16} "
              f"码率 {int((v.get('bandwidth') or 0)/1000):>6} kbps   大小 {fmt_size(stream_size(v))}")
    print(f"\n可用音频流（共 {len(dash.get('audio') or [])} 条）：")
    for a in dash.get("audio") or []:
        print(f"  {AUDIO_QN_NAME.get(a.get('id'), a.get('id')):<10} 码率 "
              f"{int((a.get('bandwidth') or 0)/1000):>6} kbps   大小 {fmt_size(stream_size(a))}")
    if not dash:
        print("  （没有 DASH 流，可能是受限内容）")


def cmd_download(client: Client, args):
    bvid = parse_bvid(args.target)
    info = fetch_info(client, bvid)
    pages = info.get("pages") or [{}]
    page = args.page
    if page < 1 or page > len(pages):
        raise BiliError(f"分P 序号超出范围（该视频共 {len(pages)} P）")
    cur = pages[page - 1]
    cid = cur["cid"]

    title = info.get("title") or bvid
    if len(pages) > 1:
        title = f"{title}-P{page} {cur.get('part') or ''}".strip()
    filename = safe_filename(title)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{filename}.mp4"

    print(f"标题：{title}")
    print(f"UP主：{(info.get('owner') or {}).get('name')}   时长：{info.get('duration')} 秒")
    print(f"输出：{final}")

    if final.exists() and not args.force:
        raise BiliError(f"目标文件已存在：{final}（加 --force 覆盖）")

    play = fetch_playurl(client, bvid, cid)
    dash = play.get("dash") or {}
    v, a = pick_streams(dash, args.quality_cap, args.audio_cap)
    print(f"已选画质：{QN_NAME.get(v.get('id'), v.get('id'))} / {v.get('codecs')}"
          f"   音频：{AUDIO_QN_NAME.get(a.get('id'), a.get('id')) or '无'}")

    chunk = int(float(args.chunk_mb) * 1024 * 1024)
    v_tmp = out_dir / f"{filename}.video.m4s"
    a_tmp = out_dir / f"{filename}.audio.m4s"

    print("[1/3] 下载视频流 …")
    download_stream(client, stream_url(v), v_tmp, stream_size(v), chunk, args.workers)
    audio_file = None
    if a:
        print("[2/3] 下载音频流 …")
        download_stream(client, stream_url(a), a_tmp, stream_size(a), chunk, args.workers)
        audio_file = a_tmp
    else:
        print("[2/3] 该视频没有独立音频流，跳过")

    if audio_file is None:
        shutil.move(str(v_tmp), str(final))
        print(f"完成：{final}")
        return

    print("[3/3] 合流 …")
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        print("[提示] 没找到 ffmpeg，视频与音频已分别下好，未合流：")
        print(f"       视频 {v_tmp}")
        print(f"       音频 {audio_file}")
        print("       请用 --ffmpeg 指定 ffmpeg 后重跑，或用 --merge-only 单独合流")
        return
    merged = out_dir / f"{filename}.merged.mp4"
    merge_with_ffmpeg(ffmpeg, v_tmp, audio_file, merged)
    if final.exists():
        final.unlink()
    shutil.move(str(merged), str(final))
    if args.clean_parts:
        for p in (v_tmp, a_tmp):
            p.unlink(missing_ok=True)
            Path(str(p) + ".part.json").unlink(missing_ok=True)
    print(f"完成：{final}  （{fmt_size(final.stat().st_size)}）")
    if not args.clean_parts:
        print("       中间文件已保留（要自动删除请加 --clean-parts）")


def cmd_merge_only(args):
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        raise BiliError("没找到 ffmpeg，请用 --ffmpeg 指定完整路径")
    video, audio = Path(args.video), Path(args.audio)
    for p in (video, audio):
        if not p.is_file():
            raise BiliError(f"文件不存在：{p}")
    # 安全：默认绝不覆盖输入的原始文件，输出成 <名字>.merged.mp4
    if args.out and args.out != DEFAULT_OUT:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{video.stem}.mp4"
    else:
        out = video.with_name(f"{video.stem}.merged.mp4")
    if out.exists() and not args.force:
        raise BiliError(f"输出文件已存在：{out}（加 --force 覆盖）")
    print(f"合流：{video.name}  +  {audio.name}")
    merge_with_ffmpeg(ffmpeg, video, audio, out)
    print(f"完成：{out}  （{fmt_size(out.stat().st_size)}）")


def build_parser():
    p = argparse.ArgumentParser(
        description="轻量 B站视频下载器（纯标准库，零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?", help="B站视频链接或 BV 号")
    p.add_argument("--list", action="store_true", help="只列出可用画质，不下载")
    p.add_argument("-o", "--out", default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}）")
    p.add_argument("-q", "--quality", default=DEFAULT_QUALITY,
                   help=f"画质上限（默认 {DEFAULT_QUALITY}）；可填 {'/'.join(NAME_QN)} 或 best 取最高")
    p.add_argument("-a", "--audio-quality", dest="audio_quality", default=DEFAULT_AUDIO,
                   help=f"音频档位上限（默认 {DEFAULT_AUDIO}）；可填 {'/'.join(NAME_AUDIO)} 或 best 取最高")
    p.add_argument("-p", "--page", type=int, default=1, help="分P 序号（默认 1）")
    p.add_argument("-j", "--workers", type=int, default=8, help="分片并发数（默认 8）")
    p.add_argument("-c", "--chunk-mb", default="2", help="分片大小 MB（默认 2）")
    p.add_argument("--sessdata", default="", help="登录 Cookie 的 SESSDATA 值")
    p.add_argument("--from-app-config", action="store_true", default=True,
                   help="从该项目的 config.json 读取 SESSDATA（默认开启）")
    p.add_argument("--no-app-config", dest="from_app_config", action="store_false",
                   help="不使用已有配置里的登录态")
    p.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    p.add_argument("--keep-parts", dest="clean_parts", action="store_false", default=False,
                   help="合流后保留中间文件（默认保留）")
    p.add_argument("--clean-parts", dest="clean_parts", action="store_true",
                   help="合流成功后删除中间文件")
    p.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p.add_argument("--merge-only", action="store_true", help="只合流，不下载")
    p.add_argument("--video", help="--merge-only 时的视频文件")
    p.add_argument("--audio", help="--merge-only 时的音频文件")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.merge_only:
            if not (args.video and args.audio):
                raise BiliError("--merge-only 需要同时给 --video 和 --audio")
            cmd_merge_only(args)
            return 0
        if not args.target:
            build_parser().print_help()
            return 2
        # 默认 360P + 64K；用 -q/-a 或 best 覆盖
        args.quality_cap = parse_quality_arg(args.quality)
        args.audio_cap = parse_audio_arg(args.audio_quality)
        sessdata = load_sessdata(args)
        client = Client(sessdata)
        if args.list:
            cmd_list(client, parse_bvid(args.target), args.page)
        else:
            cmd_download(client, args)
        return 0
    except BiliError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 已停止（已下载的分片保留，重跑可续传）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
