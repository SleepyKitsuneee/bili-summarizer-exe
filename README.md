# B站视频总结助手 · 桌面版（exe 源码，归档）

> ⚠️ **本仓库为归档 / 备用版本，不再作为主要更新对象。**
>
> **主要维护版本是浏览器插件版**：https://github.com/SleepyKitsuneee/bili-summarizer-ext
>
> 新功能、总结格式调整、提示词优化都请在插件版仓库查看。本仓库保留完整可用的桌面版源码，
> 仅做必要的兼容性维护；两边的**总结格式契约（`summary_schema.json`）保持一致**。

桌面版是一个 Python + tkinter 的 GUI 程序：填入 B 站链接 → 下载 → 合流 → 用无头 Edge 上传到千问 → 输出结构化总结。

---

## 与插件版的区别

| | 桌面版（本仓库） | 插件版（主版本） |
|---|---|---|
| 形态 | 独立 exe 窗口 | Edge 侧边栏 |
| 千问登录 | 独立 Edge profile（Playwright 无头） | 复用你浏览器里已登录的千问标签页 |
| 合流 | 本机 `ffmpeg.exe` | 浏览器内 `ffmpeg.wasm` |
| 浏览器操控 | 无头后台，不干扰你 | 零跳转静默 |
| 维护状态 | **归档/备用** | **主更新** |

---

## 从源码运行

### 依赖

- Python 3.11（本机用 conda 环境 `C:\ProgramData\anaconda3\envs\base_env\python.exe`）
  - 需要 `requests`、`playwright`
- **ffmpeg.exe**：放在脚本同目录（合流用，不转码，只做 `-c copy`）
- Edge 浏览器（Playwright 驱动，需 `playwright install msedge` 或系统已装 Edge）

### 启动

```bash
C:/ProgramData/anaconda3/envs/base_env/python.exe bili_ui.py
```

界面功能：链接框、账号登录态检测、模型下拉、历史记录列表（带封面缩略图）、总结区（结构化视图 / 原始 JSON 两页签）、清理临时视频。

---

## 打包成 exe

```bash
C:/ProgramData/anaconda3/envs/base_env/python.exe build_exe.py
```

产物在 `release/BiliSummary4/`（onedir 形式，`BiliSummary4.exe` + `_internal/`）。
打包时请把 `ffmpeg.exe` 与 `summary_schema.json` 一起带上（脚本已配置）。

打包后 exe 的**数据区落在 exe 旁边**：`tmp/`（临时视频，成功后自动清理）、`summaries/`（总结存档）、`history.json`、`settings.json`、`logs/`。

---

## 目录说明

| 文件 | 作用 |
|---|---|
| `bili_ui.py` | 图形界面（tkinter） |
| `bili_qwen_summary.py` | 千问自动化：上传、发送、轮询答案 |
| `bili_dl.py` | B 站下载（360P + 64K，单文件无第三方依赖） |
| `bili_entry.py` | 打包入口（支持 `--bili-dl-worker` 重入） |
| `build_exe.py` | 一键打包脚本（PyInstaller） |
| `test_ui_wiring.py` | GUI 接线集成测试（38+ 项断言，不联网） |
| `summary_schema.json` | **总结格式契约**（与插件版仓库保持一致） |

---

## 总结格式契约

`summary_schema.json` 定义输出结构（结论 / 核心要点 / 争议或不足 / 分段摘要 / 关键词）。
**改格式时请同步更新插件版仓库里的同名文件**，否则两边输出会不一致。

提示词 = 前置说明（界面里可自定义，或代码内 `DEFAULT_PREAMBLE`）+ 这份 JSON 结构。

---

## 已知限制

- 依赖千问网页端 DOM，千问改版会导致选择器失效
- 无头 Edge profile 需单独登录一次千问（登录态保存在 `%LOCALAPPDATA%\qwen-automation\edge-copy`）
- 桌面版不再新增功能

---

## 许可证

本项目代码采用 **MIT License**（见 [LICENSE](LICENSE)）。

依赖的 `ffmpeg.exe` 属 FFmpeg 项目，遵循其自身许可证，不随本仓库分发（需自行准备）。

仅供个人学习使用，请遵守 B 站与千问的服务条款。
