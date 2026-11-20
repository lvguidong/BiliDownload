"""内置 Web 服务 + 浏览器界面（零第三方依赖）。

为什么不用 tkinter：macOS 26 (Tahoe) 自带的 Tk 8.5.9 存在渲染缺陷，
Label/Entry/Text/Listbox/Canvas 文本与全部 ttk 控件都不绘制，导致窗口整片空白。
浏览器界面绕开 Tk，一定能正常显示。

用法::

    python3 -m bili_downloader --web        # 启动并自动打开浏览器
    python3 -m bili_downloader.webui        # 同上
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .api import BiliApi, BiliApiError
from .config import Config
from .constants import APP_NAME, APP_VERSION, QUALITY_MAP
from .cookie_store import CookieStore
from .manager import DownloadManager
from .models import (STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED,
                     STATUS_MERGING, STATUS_PAUSED, STATUS_PENDING,
                     STATUS_DOWNLOADING, TERMINAL_STATUSES, DownloadTask,
                     ParseResult)
from .parser import BiliParser, ParseError
from .storage import Storage
from .utils import ensure_dir, format_duration, format_size, format_speed, shorten

APP_DIR = os.environ.get("BILI_HOME") or os.path.join(os.path.expanduser("~"), ".bilidownload")

STATUS_TEXT = {
    STATUS_PENDING: "待下载",
    STATUS_DOWNLOADING: "下载中",
    STATUS_PAUSED: "已暂停",
    STATUS_MERGING: "封装中",
    STATUS_DONE: "完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
}


# ---------------------------------------------------------------- 应用状态
class WebState:
    """所有共享状态与业务动作。HTTP 处理线程通过它访问底层能力。"""

    def __init__(self) -> None:
        ensure_dir(APP_DIR)
        self.config = Config(os.path.join(APP_DIR, "config.json"))
        self.cookie_store = CookieStore(os.path.join(APP_DIR, "cookies.enc"))
        self.storage = Storage(os.path.join(APP_DIR, "tasks.db"))
        self.api = BiliApi(self.config, self.cookie_store)
        self.parser = BiliParser(self.api)
        self.manager = DownloadManager(self.config, self.api, self.storage)

        self.lock = threading.RLock()
        self.logs: List[str] = []
        self.last_result: Optional[ParseResult] = None
        self.downloading = False
        self.download_thread: Optional[threading.Thread] = None
        self.qr_url = ""
        self.qr_key = ""
        self.qr_status = "idle"   # idle / waiting / scanned / ok / expired / error
        self.qr_error = ""
        self.login_thread: Optional[threading.Thread] = None

    # ---------- 日志 ----------
    def log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + str(msg).rstrip()
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 2000:
                del self.logs[: len(self.logs) - 2000]

    def logs_since(self, since: int) -> Dict[str, Any]:
        with self.lock:
            if since < 0 or since > len(self.logs):
                since = 0
            return {"lines": self.logs[since:], "total": len(self.logs)}

    # ---------- 状态 ----------
    def account(self) -> Dict[str, Any]:
        try:
            info = self.api.account_info()
        except Exception as exc:      # 网络异常时不要让整个页面挂掉
            return {"isLogin": self.api.is_logged_in, "uname": "", "vip": False,
                    "avatar": "", "error": str(exc)}
        return info

    def state(self) -> Dict[str, Any]:
        ffmpeg = ""
        try:
            ffmpeg = self.config.ffmpeg
        except Exception:
            pass
        return {
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "account": self.account(),
            "ffmpeg": ffmpeg,
            "downloading": self.downloading,
            "config": self.config.data if hasattr(self.config, "data") else {},
            "stats": dict(self.manager.stats),
            "data_dir": APP_DIR,
            "download_dir": self.config.download_dir,
        }

    # ---------- 解析 ----------
    def do_parse(self, url: str) -> Dict[str, Any]:
        url = (url or "").strip()
        if not url:
            raise ValueError("请输入 B 站链接、BV 号或 av 号")
        self.log(f"解析：{url}")
        result = self.parser.parse(url)
        with self.lock:
            self.last_result = result
        self.log(f"解析成功：{result.series_title}（{len(result.episodes)} 集）")
        return self.result_payload(result)

    @staticmethod
    def result_payload(result: ParseResult) -> Dict[str, Any]:
        return {
            "media_id": result.media_id,
            "series_title": result.series_title,
            "type": result.type,
            "cover": result.cover,
            "description": result.description,
            "quality_options": [
                {"qn": qn, "name": QUALITY_MAP.get(qn, str(qn))}
                for qn in sorted(set(result.quality_options), reverse=True)
            ],
            "episodes": [
                {
                    "index": ep.index,
                    "title": ep.display_title,
                    "duration": ep.duration,
                    "duration_text": format_duration(ep.duration),
                    "cid": ep.cid,
                    "aid": ep.aid,
                    "bvid": ep.bvid,
                    "is_pgc": ep.is_pgc,
                }
                for ep in result.episodes
            ],
        }

    # ---------- 任务 ----------
    def tasks_payload(self) -> Dict[str, Any]:
        tasks = self.storage.list_tasks()
        rows = []
        for t in tasks:
            rows.append({
                "task_id": t.task_id,
                "title": t.episode.display_title,
                "series": t.episode.series,
                "index": t.episode.index,
                "status": t.status,
                "status_text": STATUS_TEXT.get(t.status, t.status),
                "progress": round(t.progress or 0.0, 1),
                "speed_text": format_speed(t.speed) if t.status == STATUS_DOWNLOADING else "",
                "size_text": (format_size(t.total_bytes) if t.total_bytes else
                              (format_size(t.downloaded_bytes) if t.downloaded_bytes else "")),
                "output": t.output_path,
                "error": t.error,
                "terminal": t.status in TERMINAL_STATUSES,
            })
        return {
            "tasks": rows,
            "downloading": self.downloading,
            "stats": dict(self.manager.stats),
            "counts": {
                "total": len(rows),
                "active": sum(1 for r in rows if not r["terminal"]),
                "done": sum(1 for r in rows if r["status"] == STATUS_DONE),
                "failed": sum(1 for r in rows if r["status"] == STATUS_FAILED),
            },
        }

    def start_download(self, indices: Optional[List[int]], opts: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self.downloading:
                raise ValueError("已有下载任务在进行中，请等待完成或先取消")
            result = self.last_result
        if result is None:
            raise ValueError("请先解析链接")

        # 把前端选项写入配置，build_tasks 会读取。
        # 注意：画质是「这一次下载」的选择，不能写进配置，
        # 否则用户下次打开时会莫名其妙地沿用上次的低画质。
        updates: Dict[str, Any] = {}
        for key in ("save_subtitle", "save_danmaku", "danmaku_ass", "save_cover",
                    "save_nfo", "dedup"):
            if key in opts:
                updates[key] = bool(opts[key])
        if updates:
            self.config.update(**updates)
            self.config.save()

        if opts.get("quality") is not None:
            quality = int(opts["quality"])
        else:
            quality = int(self.config.get("quality") or 0)

        audio_only = bool(opts.get("audio_only", False))
        if audio_only:
            self.log("仅下载音频模式：不下载视频画面，封装为 .m4a")

        tasks = self.manager.build_tasks(result, indices=indices, quality=quality, audio_only=audio_only)
        if not tasks:
            return {"ok": True, "started": 0,
                    "message": "没有需要下载的任务（可能已下载过，可关闭去重后重试）"}

        with self.lock:
            self.downloading = True
        self.log(f"开始下载 {len(tasks)} 个任务")

        def worker() -> None:
            old_stdout = sys.stdout
            try:
                sys.stdout = _LogBridge(self)      # 把 manager 的 print 收进日志
                self.manager.run(tasks)
            except Exception as exc:
                self.log(f"下载异常：{exc}")
                self.log(traceback.format_exc())
            finally:
                try:
                    sys.stdout = old_stdout
                except Exception:
                    pass
                with self.lock:
                    self.downloading = False
                self.log("队列结束")

        thread = threading.Thread(target=worker, daemon=True)
        with self.lock:
            self.download_thread = thread
        thread.start()
        return {"ok": True, "started": len(tasks), "message": f"已加入 {len(tasks)} 个任务"}

    def task_action(self, task_id: str, action: str) -> bool:
        if action == "pause":
            return self.manager.pause(task_id)
        if action == "resume":
            task = self.storage.get_task(task_id)
            if task and task.status == STATUS_PAUSED:
                # 暂停中的任务需要重新入队才有线程跑它
                self._run_single(task)
                return True
            return self.manager.resume(task_id)
        if action == "cancel":
            return self.manager.cancel(task_id)
        if action == "delete":
            self.storage.delete_task(task_id)
            return True
        raise ValueError(f"未知操作：{action}")

    def _run_single(self, task: DownloadTask) -> None:
        if self.downloading:
            raise ValueError("已有下载任务在进行中")

        def worker() -> None:
            old_stdout = sys.stdout
            with self.lock:
                self.downloading = True
            try:
                sys.stdout = _LogBridge(self)
                self.manager.run([task])
            except Exception as exc:
                self.log(f"任务异常：{exc}")
            finally:
                sys.stdout = old_stdout
                with self.lock:
                    self.downloading = False

        threading.Thread(target=worker, daemon=True).start()

    def bulk_action(self, action: str) -> Dict[str, Any]:
        if action == "retry_failed":
            with self.lock:
                if self.downloading:
                    raise ValueError("已有下载任务在进行中")
            tasks = self.manager.retry_failed()
            if not tasks:
                return {"ok": True, "message": "没有失败的任务"}
            self.log(f"重试 {len(tasks)} 个失败任务")
            self._run_single_batch(tasks)
            return {"ok": True, "message": f"已重新加入 {len(tasks)} 个任务"}
        if action == "resume_all":
            with self.lock:
                if self.downloading:
                    raise ValueError("已有下载任务在进行中")
            n = self.storage.mark_interrupted()
            tasks = self.storage.list_tasks([STATUS_PENDING, STATUS_PAUSED, STATUS_DOWNLOADING])
            if not tasks:
                return {"ok": True, "message": "没有未完成的任务"}
            self.log(f"恢复 {len(tasks)} 个任务（重置 {n} 个中断状态）")
            self._run_single_batch(tasks)
            return {"ok": True, "message": f"已恢复 {len(tasks)} 个任务"}
        if action == "clear_done":
            for t in self.storage.list_tasks([STATUS_DONE]):
                self.storage.delete_task(t.task_id)
            return {"ok": True, "message": "已清除完成记录"}
        if action == "clear_all":
            self.storage.clear_tasks()
            return {"ok": True, "message": "已清空任务列表"}
        raise ValueError(f"未知操作：{action}")

    def _run_single_batch(self, tasks: List[DownloadTask]) -> None:
        def worker() -> None:
            old_stdout = sys.stdout
            with self.lock:
                self.downloading = True
            try:
                sys.stdout = _LogBridge(self)
                self.manager.run(tasks)
            except Exception as exc:
                self.log(f"队列异常：{exc}")
            finally:
                sys.stdout = old_stdout
                with self.lock:
                    self.downloading = False
                self.log("队列结束")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 登录 ----------
    def login_cookie(self, cookie: str) -> Dict[str, Any]:
        cookie = (cookie or "").strip()
        if not cookie:
            raise ValueError("Cookie 不能为空")
        self.api.set_cookie_string(cookie)
        self.api.save_cookies()
        self.api._wbi_keys = None
        info = self.account()
        if info.get("isLogin"):
            self.log(f"登录成功：{info.get('uname', '用户')}")
            return {"ok": True, "message": f"登录成功：{info.get('uname', '用户')}", "account": info}
        raise ValueError("Cookie 无效或已过期，请重新复制")

    def login_logout(self) -> Dict[str, Any]:
        self.api.clear_cookies()
        self.api.save_cookies()
        self.api._wbi_keys = None
        self.log("已退出登录")
        return {"ok": True, "message": "已退出登录"}

    def login_qr_start(self) -> Dict[str, Any]:
        with self.lock:
            if self.login_thread and self.login_thread.is_alive():
                return {"ok": True, "url": self.qr_url, "status": self.qr_status}
            self.qr_status = "waiting"
            self.qr_error = ""
            self.qr_url = ""

        def worker() -> None:
            def on_url(url: str, key: str) -> None:
                with self.lock:
                    self.qr_url = url
                    self.qr_key = key

            try:
                ok = self.api.qr_login(timeout=180, on_qr_url=on_url)
                with self.lock:
                    self.qr_status = "ok" if ok else "error"
                if ok:
                    info = self.account()
                    self.log(f"登录成功：{info.get('uname', '用户')}")
            except BiliApiError as exc:
                with self.lock:
                    self.qr_status = "expired" if "过期" in str(exc) else "error"
                    self.qr_error = str(exc)
                self.log(f"扫码登录失败：{exc}")
            except Exception as exc:
                with self.lock:
                    self.qr_status = "error"
                    self.qr_error = str(exc)
                self.log(f"扫码登录异常：{exc}")

        self.login_thread = threading.Thread(target=worker, daemon=True)
        self.login_thread.start()
        # 等二维码链接就绪（最多 8 秒）
        for _ in range(80):
            with self.lock:
                if self.qr_url:
                    break
            time.sleep(0.1)
        with self.lock:
            url = self.qr_url
            qr_image = ""
            if url:
                try:
                    from .qrcode_min import to_data_url
                    # 二维码按模块放大 6 倍，手机屏幕/打印都清晰
                    qr_image = to_data_url(url, scale=6, border=3)
                except Exception as exc:
                    self.log(f"二维码生成失败：{exc}")
            return {"ok": True, "url": url, "qr_image": qr_image,
                    "status": self.qr_status, "error": self.qr_error}

    def login_qr_status(self) -> Dict[str, Any]:
        with self.lock:
            status = self.qr_status
            if status == "ok":
                self.qr_status = "idle"
            return {"status": status, "url": self.qr_url, "error": self.qr_error,
                    "account": self.account() if status == "ok" else None}

    # ---------- 设置 ----------
    def save_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "download_dir": str, "threads": int, "concurrency": int,
            "quality": int, "codec_pref": str, "speed_limit_kbps": int,
            "proxy": str, "timeout": int, "retries": int, "chunk_size_mb": int,
            "cdn_switch": bool, "dedup": bool, "name_template": str,
            "save_subtitle": bool, "save_danmaku": bool, "danmaku_ass": bool,
            "save_cover": bool, "save_nfo": bool, "season": int,
            "ffmpeg_path": str, "request_interval": float, "log_level": str,
        }
        updates: Dict[str, Any] = {}
        for key, caster in allowed.items():
            if key not in data:
                continue
            raw = data[key]
            try:
                if caster is bool:
                    updates[key] = bool(raw)
                elif caster is int:
                    updates[key] = int(raw)
                elif caster is float:
                    updates[key] = float(raw)
                else:
                    updates[key] = str(raw)
            except (TypeError, ValueError):
                continue
        if updates:
            self.config.update(**updates)
            self.config.save()
            self.log(f"设置已保存：{', '.join(updates)}")
        return {"ok": True, "config": self.config.data if hasattr(self.config, "data") else {}}


class _LogBridge:
    """把 print 输出同时写入 Web 日志。"""

    def __init__(self, state: WebState):
        self.state = state

    def write(self, s: str) -> int:
        for line in str(s).splitlines():
            if line.strip():
                self.state.log(line)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


# ---------------------------------------------------------------- HTML 界面
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BiliDownload · B 站批量下载</title>
<style>
  :root{
    --bg:#f5f6f8; --panel:#fff; --line:#e3e6ec; --text:#1f2430; --muted:#7b8494;
    --brand:#fb7299; --brand-dark:#e35c84; --ok:#12b76a; --warn:#f79009; --err:#f04438;
    --blue:#3b82f6;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text)}
  header{display:flex;align-items:center;gap:12px;padding:10px 18px;background:var(--panel);
         border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
  .logo{font-weight:700;font-size:17px;color:var(--brand)}
  .tag{color:var(--muted);font-size:12px}
  .spacer{flex:1}
  .btn{border:1px solid var(--line);background:#fff;color:var(--text);padding:7px 14px;
       border-radius:8px;cursor:pointer;font-size:13px;transition:.15s}
  .btn:hover{border-color:var(--brand);color:var(--brand)}
  .btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
  .btn.primary:hover{background:var(--brand-dark);color:#fff}
  .btn.danger:hover{border-color:var(--err);color:var(--err)}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .btn.sm{padding:4px 10px;font-size:12px;border-radius:6px}
  main{display:grid;grid-template-columns:340px 1fr;gap:14px;padding:14px 18px;align-items:start}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .panel h2{margin:0 0 10px;font-size:14px;font-weight:600;color:var(--muted);
            text-transform:uppercase;letter-spacing:.04em}
  input[type=text],input[type=number],select,textarea{
    width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
    font:inherit;background:#fff;color:var(--text);outline:none}
  input:focus,select:focus,textarea:focus{border-color:var(--brand)}
  label.field{display:block;margin-bottom:10px}
  label.field span{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
  .row{display:flex;gap:8px}
  .row>*{flex:1}
  .checks{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;margin:10px 0}
  .checks label{display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer}
  .muted{color:var(--muted);font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--muted);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--panel)}
  tbody tr:hover{background:#fafbfc}
  .scroll{max-height:calc(100vh - 340px);overflow:auto;border-radius:8px;border:1px solid var(--line)}
  .badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;white-space:nowrap}
  .b-pending{background:#eef2f7;color:#5b6b82}
  .b-downloading{background:#e8f1ff;color:var(--blue)}
  .b-merging{background:#fff4e5;color:var(--warn)}
  .b-done{background:#e7f8ef;color:var(--ok)}
  .b-failed{background:#fdecec;color:var(--err)}
  .b-paused{background:#f4f0ff;color:#7c5cff}
  .b-cancelled{background:#f3f4f6;color:var(--muted)}
  .bar{height:6px;background:#eef0f4;border-radius:4px;overflow:hidden;min-width:90px}
  .bar>i{display:block;height:100%;background:var(--brand);border-radius:4px;transition:width .3s}
  .empty{padding:40px;text-align:center;color:var(--muted)}
  #logs{background:#1e2430;color:#cfd6e4;font:12px/1.65 Menlo,Consolas,monospace;
        border-radius:10px;padding:10px 12px;height:160px;overflow:auto;white-space:pre-wrap;
        word-break:break-all}
  .tabs{display:flex;gap:6px;margin-bottom:12px;border-bottom:1px solid var(--line)}
  .tabs button{border:none;background:none;padding:8px 14px;cursor:pointer;font:inherit;
               color:var(--muted);border-bottom:2px solid transparent}
  .tabs button.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
  .status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
  .modal-bg{position:fixed;inset:0;background:rgba(20,25,35,.42);display:none;
            align-items:center;justify-content:center;z-index:50}
  .modal{background:#fff;border-radius:14px;padding:20px;width:min(560px,92vw);
         max-height:86vh;overflow:auto;box-shadow:0 18px 50px rgba(0,0,0,.22)}
  .modal h3{margin:0 0 14px;font-size:16px}
  .toast{position:fixed;right:20px;bottom:20px;background:#1f2430;color:#fff;padding:11px 16px;
         border-radius:10px;font-size:13px;opacity:0;transform:translateY(8px);
         transition:.25s;z-index:99;max-width:420px}
  .toast.show{opacity:1;transform:none}
  .toast.err{background:var(--err)}
  .toast.ok{background:var(--ok)}
  .sechead{display:flex;align-items:center;gap:8px;margin-bottom:8px}
  .sechead h2{margin:0}
  code{background:#f2f4f8;padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<header>
  <span class="logo">BiliDownload</span>
  <span class="tag">开箱即用 · 多线程分片 · 无损混流</span>
  <span class="spacer"></span>
  <span id="loginState" class="muted">未登录</span>
  <button class="btn sm" id="btnLogin">扫码登录</button>
  <button class="btn sm" id="btnCookie">Cookie 登录</button>
  <button class="btn sm" id="btnSettings">设置</button>
</header>

<main>
  <!-- 左侧：解析 -->
  <section class="panel">
    <h2>解析</h2>
    <label class="field">
      <span>B 站链接 / BV / av / ep / ss 号</span>
      <input type="text" id="url" placeholder="https://www.bilibili.com/video/BV... 或 BV1xx411c7mD">
    </label>
    <div class="row">
      <button class="btn primary" id="btnParse">解析</button>
      <button class="btn" id="btnDownload" disabled>下载选中</button>
    </div>

    <div class="checks">
      <label class="audio-toggle"><input type="checkbox" id="optAudioOnly"> <b>仅下载音频</b></label>
      <label><input type="checkbox" id="optSubtitle"> 字幕</label>
      <label><input type="checkbox" id="optDanmaku"> 弹幕</label>
      <label><input type="checkbox" id="optCover"> 封面</label>
      <label><input type="checkbox" id="optNfo"> NFO</label>
      <label><input type="checkbox" id="optDedup" checked> 跳过已下载</label>
      <label><input type="checkbox" id="optAss"> 弹幕转 ASS</label>
    </div>
    <div id="audioHint" class="muted" style="margin-top:6px;display:none;font-size:12px">
      仅下载音频：不下载视频画面，直接封装为 .m4a（无损，不转码）。如有封面会嵌入到音频文件中。
    </div>

    <label class="field">
      <span>画质</span>
      <select id="quality">
        <option value="0">自动（最高可用）</option>
        <option value="127">8K</option>
        <option value="126">杜比视界</option>
        <option value="125">HDR</option>
        <option value="120">4K</option>
        <option value="116">1080P60</option>
        <option value="112">1080P+</option>
        <option value="80">1080P</option>
        <option value="74">720P60</option>
        <option value="64">720P</option>
        <option value="32">480P</option>
        <option value="16">360P</option>
      </select>
    </label>

    <div id="seriesInfo" class="muted" style="margin-top:10px">尚未解析链接</div>
    <div id="rangeRow" style="margin-top:10px;display:none">
      <label class="field" style="margin:0">
        <span>选集范围（留空 = 全部，如 1-12,15）</span>
        <input type="text" id="range" placeholder="all">
      </label>
    </div>
  </section>

  <!-- 右侧：分集 + 任务 -->
  <section class="panel">
    <div class="tabs">
      <button class="active" data-tab="ep">分集列表</button>
      <button data-tab="task">任务队列</button>
      <button data-tab="log">日志</button>
    </div>

    <div id="tab-ep">
      <div class="sechead">
        <h2 style="font-size:13px">分集</h2>
        <span class="spacer"></span>
        <button class="btn sm" id="btnAll">全选</button>
        <button class="btn sm" id="btnNone">全不选</button>
        <span id="epCount" class="muted"></span>
      </div>
      <div class="scroll" id="epScroll">
        <div class="empty">解析链接后显示分集</div>
      </div>
    </div>

    <div id="tab-task" style="display:none">
      <div class="sechead">
        <h2 style="font-size:13px">任务</h2>
        <span id="taskStats" class="muted"></span>
        <span class="spacer"></span>
        <button class="btn sm" data-bulk="resume_all">恢复未完成</button>
        <button class="btn sm" data-bulk="retry_failed">重试失败</button>
        <button class="btn sm" data-bulk="clear_done">清除已完成</button>
      </div>
      <div class="scroll" id="taskScroll">
        <div class="empty">还没有任务</div>
      </div>
    </div>

    <div id="tab-log" style="display:none">
      <div id="logs"></div>
    </div>
  </section>
</main>

<!-- 设置弹窗 -->
<div class="modal-bg" id="settingsModal">
  <div class="modal">
    <h3>设置</h3>
    <label class="field"><span>下载目录</span><input type="text" id="s_download_dir"></label>
    <div class="row">
      <label class="field"><span>单文件线程数</span><input type="number" id="s_threads" min="1" max="32"></label>
      <label class="field"><span>同时任务数</span><input type="number" id="s_concurrency" min="1" max="8"></label>
    </div>
    <div class="row">
      <label class="field"><span>限速 (KB/s，0 不限)</span><input type="number" id="s_speed_limit_kbps" min="0"></label>
      <label class="field"><span>分片大小 (MB)</span><input type="number" id="s_chunk_size_mb" min="1"></label>
    </div>
    <label class="field"><span>命名模板</span><input type="text" id="s_name_template"></label>
    <div class="muted" style="margin:-4px 0 10px">
      占位符：<code>{title}</code> <code>{index}</code> <code>{series}</code> <code>{ext}</code>
      <code>{quality}</code> <code>{codec}</code> <code>{season}</code> <code>{year}</code>
    </div>
    <label class="field"><span>编码偏好</span><input type="text" id="s_codec_pref"></label>
    <label class="field"><span>代理（http:// 或 socks5://）</span><input type="text" id="s_proxy"></label>
    <div class="muted" id="ffmpegInfo" style="margin-bottom:12px"></div>
    <div class="row" style="justify-content:flex-end">
      <button class="btn" id="btnSettingsCancel">取消</button>
      <button class="btn primary" id="btnSettingsSave">保存</button>
    </div>
  </div>
</div>

<!-- 扫码登录弹窗 -->
<div class="modal-bg" id="qrModal">
  <div class="modal" style="width:min(460px,92vw)">
    <h3>扫码登录</h3>
    <div id="qrBody"></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <button class="btn" id="btnQrClose">关闭</button>
    </div>
  </div>
</div>

<!-- Cookie 登录弹窗 -->
<div class="modal-bg" id="cookieModal">
  <div class="modal">
    <h3>Cookie 登录</h3>
    <p class="muted" style="margin-top:0">
      在浏览器登录 bilibili.com，按 F12 → Application/应用 → Cookies，
      复制 <code>SESSDATA</code>、<code>bili_jct</code>、<code>DedeUserID</code> 等值，
      或直接粘贴完整 Cookie 串（<code>a=1; b=2</code> 形式）。
    </p>
    <textarea id="cookieText" rows="6" placeholder="SESSDATA=xxx; bili_jct=yyy; DedeUserID=123"></textarea>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <button class="btn" id="btnCookieCancel">取消</button>
      <button class="btn primary" id="btnCookieSave">登录</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const state = {episodes: [], parsed: null, logTotal: 0, tab: 'ep'};

function toast(msg, kind){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast'; }, 3200);
}

async function api(path, body){
  const opt = {method: body === undefined ? 'GET' : 'POST',
               headers: {'Content-Type': 'application/json'}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  let data;
  try { data = await res.json(); } catch(e){ data = {ok:false, error:'服务器响应异常'}; }
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

/* ---------- 状态 ---------- */
async function refreshState(){
  try{
    const s = await api('/api/state');
    const acc = s.account || {};
    const dot = acc.isLogin ? '<span class="status-dot" style="background:#12b76a"></span>' : '<span class="status-dot" style="background:#c3cad6"></span>';
    $('loginState').innerHTML = dot + (acc.isLogin ? (acc.uname || '已登录') + (acc.vip ? ' · 大会员' : '') : '未登录');
    $('ffmpegInfo').textContent = s.ffmpeg ? ('ffmpeg: ' + s.ffmpeg) : 'ffmpeg: 未找到（仅影响混流）';
    if (s.config){
      const c = s.config;
      ['download_dir','threads','concurrency','speed_limit_kbps','chunk_size_mb',
       'name_template','codec_pref','proxy'].forEach(k => {
        const el = $('s_' + k);
        if (el && c[k] !== undefined && document.activeElement !== el) el.value = c[k];
      });
    }
  }catch(e){ /* 静默 */ }
}

/* ---------- 解析 ---------- */
async function doParse(){
  const url = $('url').value.trim();
  if (!url){ toast('请先输入链接', 'err'); return; }
  const btn = $('btnParse');
  btn.disabled = true; btn.textContent = '解析中…';
  try{
    const r = await api('/api/parse', {url});
    state.parsed = r;
    renderSeries(r);
    renderEpisodes(r.episodes);
    $('btnDownload').disabled = !r.episodes.length;
    toast('解析成功：' + r.series_title, 'ok');
  }catch(e){
    toast('解析失败：' + e.message, 'err');
    $('epScroll').innerHTML = '<div class="empty">解析失败：' + escapeHtml(e.message) + '</div>';
  }finally{
    btn.disabled = false; btn.textContent = '解析';
  }
}

function renderSeries(r){
  const q = (r.quality_options || []);
  $('seriesInfo').innerHTML =
    '<div style="color:#1f2430;font-weight:600;margin-bottom:2px">' + escapeHtml(r.series_title) + '</div>' +
    '<div>' + r.episodes.length + ' 集 · 类型 ' + escapeHtml(r.type) + '</div>' +
    (q.length ? '<div>可用画质：' + q.map(x => x.name).join(' / ') + '</div>' : '');
  $('rangeRow').style.display = 'block';
}

function renderEpisodes(list){
  state.episodes = list.map(e => ({...e, sel: true}));
  const box = $('epScroll');
  if (!list.length){ box.innerHTML = '<div class="empty">没有分集</div>'; return; }
  let html = '<table><thead><tr><th style="width:36px"></th><th style="width:52px">#</th>' +
             '<th>标题</th><th style="width:92px">时长</th><th style="width:120px">CID</th></tr></thead><tbody>';
  for (const e of list){
    html += '<tr><td><input type="checkbox" class="epchk" data-index="' + e.index + '" checked></td>' +
            '<td>' + e.index + '</td>' +
            '<td>' + escapeHtml(e.title) + '</td>' +
            '<td>' + escapeHtml(e.duration_text || '') + '</td>' +
            '<td class="muted">' + e.cid + '</td></tr>';
  }
  html += '</tbody></table>';
  box.innerHTML = html;
  box.querySelectorAll('.epchk').forEach(cb => {
    cb.addEventListener('change', () => {
      const ep = state.episodes.find(x => x.index === Number(cb.dataset.index));
      if (ep) ep.sel = cb.checked;
      updateEpCount();
    });
  });
  updateEpCount();
}

function updateEpCount(){
  const n = state.episodes.filter(e => e.sel).length;
  $('epCount').textContent = '已选 ' + n + ' / ' + state.episodes.length;
}

/* ---------- 下载 ---------- */
async function doDownload(){
  const range = $('range').value.trim();
  let indices = null;
  if (range && range.toLowerCase() !== 'all' && range !== '*'){
    try{
      indices = expandRange(range);
    }catch(e){ toast('选集范围格式不对', 'err'); return; }
  }
  if (indices === null){
    indices = state.episodes.filter(e => e.sel).map(e => e.index);
    if (!indices.length){ toast('请至少选中一集', 'err'); return; }
  }
  const body = {
    indices,
    quality: Number($('quality').value),
    audio_only: $('optAudioOnly').checked,
    save_subtitle: $('optSubtitle').checked,
    save_danmaku: $('optDanmaku').checked,
    save_cover: $('optCover').checked,
    save_nfo: $('optNfo').checked,
    dedup: $('optDedup').checked,
    danmaku_ass: $('optAss').checked,
  };
  try{
    const r = await api('/api/download', body);
    toast(r.message || '已开始下载', 'ok');
    switchTab('task');
    refreshTasks();
  }catch(e){ toast('下载失败：' + e.message, 'err'); }
}

function expandRange(spec){
  const out = new Set();
  for (const part of spec.split(',')){
    const p = part.trim();
    if (!p) continue;
    if (p.includes('-')){
      const [a,b] = p.split('-').map(x => parseInt(x.trim(), 10));
      if (isNaN(a) || isNaN(b)) throw new Error('bad');
      for (let i = Math.min(a,b); i <= Math.max(a,b); i++) out.add(i);
    } else {
      const n = parseInt(p, 10);
      if (isNaN(n)) throw new Error('bad');
      out.add(n);
    }
  }
  return [...out].sort((x,y) => x-y);
}

/* ---------- 任务 ---------- */
async function refreshTasks(){
  try{
    const d = await api('/api/tasks');
    renderTasks(d);
    const downloading = d.downloading;
    $('btnDownload').disabled = downloading || !(state.parsed && state.parsed.episodes.length);
  }catch(e){ /* 静默 */ }
}

function renderTasks(d){
  $('taskStats').textContent = '共 ' + d.counts.total + ' · 完成 ' + d.counts.done +
                               ' · 失败 ' + d.counts.failed +
                               (d.downloading ? ' · 队列运行中' : '');
  const box = $('taskScroll');
  if (!d.tasks.length){ box.innerHTML = '<div class="empty">还没有任务</div>'; return; }
  let html = '<table><thead><tr><th>标题</th><th style="width:82px">状态</th>' +
             '<th style="width:150px">进度</th><th style="width:86px">速度</th>' +
             '<th style="width:88px">大小</th><th style="width:150px">操作</th></tr></thead><tbody>';
  for (const t of d.tasks){
    const pct = Math.max(0, Math.min(100, t.progress || 0));
    const canPause = ['pending','downloading'].includes(t.status);
    const canResume = t.status === 'paused';
    const canCancel = !t.terminal;
    html += '<tr>' +
      '<td title="' + escapeAttr(t.output || '') + '">' + escapeHtml(t.title) +
        (t.error ? '<div class="muted" style="color:#f04438">' + escapeHtml(t.error) + '</div>' : '') + '</td>' +
      '<td><span class="badge b-' + t.status + '">' + escapeHtml(t.status_text) + '</span></td>' +
      '<td><div class="bar"><i style="width:' + pct + '%"></i></div>' +
        '<span class="muted">' + pct.toFixed(1) + '%</span></td>' +
      '<td class="muted">' + escapeHtml(t.speed_text) + '</td>' +
      '<td class="muted">' + escapeHtml(t.size_text) + '</td>' +
      '<td>' +
        (canPause ? '<button class="btn sm" data-task="' + t.task_id + '" data-act="pause">暂停</button> ' : '') +
        (canResume ? '<button class="btn sm" data-task="' + t.task_id + '" data-act="resume">继续</button> ' : '') +
        (canCancel ? '<button class="btn sm danger" data-task="' + t.task_id + '" data-act="cancel">取消</button>' :
                     '<button class="btn sm" data-task="' + t.task_id + '" data-act="delete">移除</button>') +
      '</td></tr>';
  }
  html += '</tbody></table>';
  box.innerHTML = html;
  box.querySelectorAll('button[data-task]').forEach(b => {
    b.addEventListener('click', async () => {
      try{
        await api('/api/task', {task_id: b.dataset.task, action: b.dataset.act});
        refreshTasks();
      }catch(e){ toast(e.message, 'err'); }
    });
  });
}

/* ---------- 日志 ---------- */
async function refreshLogs(){
  try{
    const d = await api('/api/logs?since=' + state.logTotal);
    if (d.lines && d.lines.length){
      const el = $('logs');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 12;
      el.textContent += (el.textContent ? '\n' : '') + d.lines.join('\n');
      const lines = el.textContent.split('\n');
      if (lines.length > 600) el.textContent = lines.slice(-600).join('\n');
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    state.logTotal = d.total || state.logTotal;
  }catch(e){ /* 静默 */ }
}

/* ---------- 设置 ---------- */
async function openSettings(){
  await refreshState();
  $('settingsModal').style.display = 'flex';
}
async function saveSettings(){
  const body = {};
  ['download_dir','threads','concurrency','speed_limit_kbps','chunk_size_mb',
   'name_template','codec_pref','proxy'].forEach(k => {
    const el = $('s_' + k);
    if (el) body[k] = (el.type === 'number') ? Number(el.value) : el.value;
  });
  try{
    await api('/api/settings', body);
    $('settingsModal').style.display = 'none';
    toast('设置已保存', 'ok');
    refreshState();
  }catch(e){ toast('保存失败：' + e.message, 'err'); }
}

/* ---------- 登录 ---------- */
async function openQr(){
  $('qrModal').style.display = 'flex';
  $('qrBody').innerHTML = '<div class="muted">正在获取二维码…</div>';
  try{
    const d = await api('/api/login/qr', {});
    const img = d.qr_image
      ? '<div style="text-align:center;margin:6px 0 14px">' +
          '<img src="' + d.qr_image + '" alt="登录二维码" ' +
          'style="width:236px;height:236px;image-rendering:pixelated;border:1px solid #e3e6ec;border-radius:10px">' +
        '</div>'
      : '<div class="muted" style="margin-bottom:10px">二维码图片生成失败，请点下面链接在手机打开。</div>';
    $('qrBody').innerHTML =
      '<p class="muted" style="margin-top:0">用手机 B 站 App 扫一扫下方二维码，确认后自动登录。</p>' +
      img +
      '<p class="muted" style="word-break:break-all">或点此链接：<a href="' + escapeAttr(d.url) + '" target="_blank" rel="noreferrer">' +
        escapeHtml(d.url) + '</a></p>' +
      '<p class="muted" id="qrStatusText">等待扫码…</p>';
    pollQr();
  }catch(e){
    $('qrBody').innerHTML = '<div style="color:#f04438">' + escapeHtml(e.message) + '</div>';
  }
}
async function pollQr(){
  try{
    const d = await api('/api/login/qr/status');
    const el = $('qrStatusText');
    if (!el) return;
    if (d.status === 'ok'){
      el.textContent = '登录成功！';
      el.style.color = '#12b76a';
      toast('登录成功', 'ok');
      refreshState();
      setTimeout(() => { $('qrModal').style.display = 'none'; }, 1200);
      return;
    }
    if (d.status === 'expired' || d.status === 'error'){
      el.textContent = d.error || '二维码已失效，请重新打开';
      el.style.color = '#f04438';
      return;
    }
    setTimeout(pollQr, 1500);
  }catch(e){ /* 静默 */ }
}

/* ---------- 工具 ---------- */
function escapeHtml(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function escapeAttr(s){ return escapeHtml(s); }

function switchTab(name){
  state.tab = name;
  ['ep','task','log'].forEach(t => { $('tab-' + t).style.display = (t === name) ? '' : 'none'; });
  document.querySelectorAll('.tabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
}

/* ---------- 仅下载音频：联动禁用无关选项 ---------- */
function applyAudioMode(){
  const on = $('optAudioOnly').checked;
  $('audioHint').style.display = on ? 'block' : 'none';
  // 纯音频不需要视频画质/字幕/弹幕，禁用以免误导
  $('quality').disabled = on;
  $('optSubtitle').disabled = on;
  $('optDanmaku').disabled = on;
  $('optAss').disabled = on;
  if (on){
    $('optSubtitle').checked = false;
    $('optDanmaku').checked = false;
    $('optAss').checked = false;
    // 音频默认带上封面，音乐播放器可显示专辑图
    $('optCover').checked = true;
  }
}

/* ---------- 事件绑定 ---------- */
$('optAudioOnly').addEventListener('change', applyAudioMode);
$('btnParse').addEventListener('click', doParse);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') doParse(); });
$('btnDownload').addEventListener('click', doDownload);
$('btnAll').addEventListener('click', () => {
  state.episodes.forEach(e => e.sel = true);
  document.querySelectorAll('.epchk').forEach(cb => cb.checked = true);
  updateEpCount();
});
$('btnNone').addEventListener('click', () => {
  state.episodes.forEach(e => e.sel = false);
  document.querySelectorAll('.epchk').forEach(cb => cb.checked = false);
  updateEpCount();
});
document.querySelectorAll('.tabs button').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.tab));
});
document.querySelectorAll('button[data-bulk]').forEach(b => {
  b.addEventListener('click', async () => {
    try{
      const r = await api('/api/tasks/bulk', {action: b.dataset.bulk});
      toast(r.message || '完成', 'ok');
      refreshTasks();
    }catch(e){ toast(e.message, 'err'); }
  });
});
$('btnSettings').addEventListener('click', openSettings);
$('btnSettingsCancel').addEventListener('click', () => { $('settingsModal').style.display = 'none'; });
$('btnSettingsSave').addEventListener('click', saveSettings);
$('btnLogin').addEventListener('click', openQr);
$('btnQrClose').addEventListener('click', () => { $('qrModal').style.display = 'none'; });
$('btnCookie').addEventListener('click', () => { $('cookieModal').style.display = 'flex'; });
$('btnCookieCancel').addEventListener('click', () => { $('cookieModal').style.display = 'none'; });
$('btnCookieSave').addEventListener('click', async () => {
  try{
    const r = await api('/api/login/cookie', {cookie: $('cookieText').value});
    toast(r.message || '登录成功', 'ok');
    $('cookieModal').style.display = 'none';
    refreshState();
  }catch(e){ toast(e.message, 'err'); }
});

/* 初始化 */
refreshState();
refreshTasks();
refreshLogs();
setInterval(refreshTasks, 1000);
setInterval(refreshLogs, 1200);
setInterval(refreshState, 8000);

/* 支持 ?url=... 深链：打开即自动解析；?login=qr 直接弹扫码；?tab=task 直达任务页 */
(function(){
  const q = new URLSearchParams(location.search);
  const u = q.get('url');
  if (u){
    $('url').value = u;
    doParse();
  } else if (q.get('tab') === 'task'){
    switchTab('task');
  }
  if (q.get('login') === 'qr') openQr();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    state: WebState = None        # 由 serve() 注入
    server_version = f"{APP_NAME}/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:   # 静音默认访问日志
        return

    # ---------- 响应工具 ----------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": message}, code)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif path == "/api/state":
                self._json(self.state.state())
            elif path == "/api/tasks":
                self._json(self.state.tasks_payload())
            elif path == "/api/logs":
                since = int((query.get("since") or ["0"])[0])
                self._json(self.state.logs_since(since))
            elif path == "/api/login/qr/status":
                self._json(self.state.login_qr_status())
            else:
                self._error("未知路径", 404)
        except Exception as exc:
            self._error(f"{exc}", 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        data = self._read_json()
        try:
            if path == "/api/parse":
                self._json({"ok": True, **self.state.do_parse(data.get("url", ""))})
            elif path == "/api/download":
                indices = data.get("indices")
                if indices is not None:
                    try:
                        indices = [int(x) for x in indices]
                    except (TypeError, ValueError):
                        indices = None
                self._json(self.state.start_download(indices, data))
            elif path == "/api/task":
                ok = self.state.task_action(str(data.get("task_id", "")),
                                            str(data.get("action", "")))
                self._json({"ok": True, "changed": bool(ok)})
            elif path == "/api/tasks/bulk":
                self._json(self.state.bulk_action(str(data.get("action", ""))))
            elif path == "/api/settings":
                self._json(self.state.save_settings(data))
            elif path == "/api/login/cookie":
                self._json(self.state.login_cookie(data.get("cookie", "")))
            elif path == "/api/login/logout":
                self._json(self.state.login_logout())
            elif path == "/api/login/qr":
                self._json(self.state.login_qr_start())
            else:
                self._error("未知路径", 404)
        except (ParseError, ValueError) as exc:
            self._error(str(exc), 400)
        except BiliApiError as exc:
            self._error(f"B 站接口错误：{exc}", 502)
        except Exception as exc:
            self._error(f"{exc}", 500)


# ---------------------------------------------------------------- 启动
def _pick_port(host: str, preferred: int) -> int:
    """优先用 preferred，被占用则让系统分配。"""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def serve(host: str = "127.0.0.1", port: int = 0,
          open_browser: bool = True) -> None:
    """启动内置 Web 服务（阻塞）。"""
    state = WebState()
    # 上次异常退出的任务重置为待下载
    try:
        n = state.storage.mark_interrupted()
        if n:
            state.log(f"已重置 {n} 个中断任务为待下载")
    except Exception:
        pass

    Handler.state = state
    port = _pick_port(host, port)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"{APP_NAME} {APP_VERSION} 已启动：{url}")
    print(f"数据目录：{APP_DIR}")
    print("按 Ctrl+C 停止服务。")
    sys.stdout.flush()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="BiliDownload 浏览器界面")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=0, help="端口（默认自动选择）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    serve(args.host, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
