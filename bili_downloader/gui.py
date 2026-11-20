"""BiliDownload 图形界面（tkinter，零第三方依赖）。

布局：
  ┌───────────────────────────────────────────────┐
  │ 顶部工具栏：登录状态 / 登录按钮 / 设置按钮      │
  ├──────────────┬────────────────────────────────┤
  │ 左侧：解析面板 │ 右侧：分集列表 + 任务队列       │
  │ 输入链接      │                                │
  │ 解析/下载按钮 │                                │
  │ 选集范围      │                                │
  ├──────────────┴────────────────────────────────┤
  │ 底部：日志输出                                 │
  └───────────────────────────────────────────────┘

所有网络/下载操作在后台线程执行，通过线程安全队列把事件投递到 UI 线程。
"""

from __future__ import annotations

import io
import logging
import os
import queue
import re
import sys
import threading
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Set, Tuple

from .api import BiliApi, BiliApiError
from .config import Config
from .constants import QUALITY_MAP
from .cookie_store import CookieStore
from .manager import DownloadManager
from .models import (STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_MERGING,
                     STATUS_PAUSED, STATUS_PENDING, STATUS_DOWNLOADING,
                     DownloadTask, Episode, ParseResult, TERMINAL_STATUSES)
from .parser import BiliParser, ParseError
from .storage import Storage
from .utils import (ensure_dir, format_duration, format_size, format_speed,
                    parse_range_selection, shorten)

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

# 用于把 manager 的 print 重定向到 GUI 日志
class QueueWriter(io.StringIO):
    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def write(self, s: str) -> int:
        if s.strip():
            self.q.put(("log", s.rstrip()))
        return len(s)

    def flush(self):
        pass


class DownloadApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BiliDownload · B 站批量下载")
        self.geometry("1100x720")
        self.minsize(940, 600)

        self.config_path = os.path.join(APP_DIR, "config.json")
        self.config = Config(self.config_path)
        self.cookie_store = CookieStore(os.path.join(APP_DIR, "cookies.enc"))
        self.storage = Storage(os.path.join(APP_DIR, "tasks.db"))
        self.api = BiliApi(self.config, self.cookie_store)
        self.parser = BiliParser(self.api)
        self.manager = DownloadManager(self.config, self.api, self.storage)

        self.events: "queue.Queue" = queue.Queue()
        self.current_result: Optional[ParseResult] = None
        self.task_map: Dict[str, DownloadTask] = {}  # task_id -> task（UI 缓存）
        self.selected_indices: Set[int] = set()
        self.downloading = False
        self._stop_flag = threading.Event()
        self._qr_window: Optional[tk.Toplevel] = None
        self._qr_canvas: Optional[tk.Canvas] = None
        self._qr_photo = None  # 保持引用，防止被 GC
        self._poll_job: Optional[str] = None

        self._build_ui()
        self._redirect_output()
        self._load_existing_tasks()
        self._update_login_status()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        # 使用系统默认主题（macOS 为原生 aqua，渲染最可靠）。
        # 不再强制切到 clam：纯 Tk 绘制主题在部分 macOS + Tk 8.5 上会导致内容不渲染（空白窗口）。
        ttk.Style(self)

        # 顶部工具栏
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill=tk.X)
        ttk.Label(top, text="BiliDownload", font=("Helvetica", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, text="开箱即用 · 多线程分片 · 无损混流",
                  foreground="#666").pack(side=tk.LEFT, padx=(10, 0))
        self.login_label = ttk.Label(top, text="未登录", foreground="#c0392b")
        self.login_label.pack(side=tk.RIGHT, padx=6)
        self.login_btn = ttk.Button(top, text="扫码登录", command=self.on_login, width=10)
        self.login_btn.pack(side=tk.RIGHT, padx=4)
        self.settings_btn = ttk.Button(top, text="设置", command=self.on_settings, width=8)
        self.settings_btn.pack(side=tk.RIGHT, padx=4)

        # 主体：左右分栏
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # 左侧解析面板
        left = ttk.LabelFrame(body, text="解析", padding=10)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        left.configure(width=300)

        ttk.Label(left, text="B 站链接 / BV / av 号：").pack(anchor=tk.W)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(left, textvariable=self.url_var, width=36)
        url_entry.pack(fill=tk.X, pady=(4, 8))
        url_entry.bind("<Return>", lambda e: self.on_parse())

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X)
        self.parse_btn = ttk.Button(btn_row, text="解析", command=self.on_parse, width=8)
        self.parse_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.download_btn = ttk.Button(btn_row, text="下载选中", command=self.on_download, width=10)
        self.download_btn.pack(side=tk.LEFT)

        ttk.Label(left, text="选集范围（如 1-12,15）：").pack(anchor=tk.W, pady=(14, 0))
        self.range_var = tk.StringVar(value="all")
        ttk.Entry(left, textvariable=self.range_var, width=20).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(left, text="留空或 all = 全部", foreground="#888").pack(anchor=tk.W)

        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        self.info_var = tk.StringVar(value="尚未解析链接")
        info_lbl = ttk.Label(left, textvariable=self.info_var, wraplength=270,
                             justify=tk.LEFT, foreground="#444")
        info_lbl.pack(anchor=tk.W, fill=tk.X)

        # 画质与选项
        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        opt = ttk.Frame(left)
        opt.pack(fill=tk.X)
        ttk.Label(opt, text="画质：").grid(row=0, column=0, sticky=tk.W)
        self.quality_var = tk.IntVar(value=int(self.config.get("quality") or 0))
        quality_combo = ttk.Combobox(opt, textvariable=self.quality_var, width=18, state="readonly")
        quality_combo["values"] = [0, 16, 32, 64, 74, 80, 112, 116, 120, 125, 126]
        quality_combo.grid(row=0, column=1, sticky=tk.W, pady=4)
        # IntVar 与 Combobox 值类型兼容处理
        self.quality_combo = quality_combo

        self.subtitle_var = tk.BooleanVar(value=bool(self.config.get("save_subtitle")))
        self.danmaku_var = tk.BooleanVar(value=bool(self.config.get("save_danmaku")))
        self.cover_var = tk.BooleanVar(value=bool(self.config.get("save_cover")))
        self.nfo_var = tk.BooleanVar(value=bool(self.config.get("save_nfo")))
        ttk.Checkbutton(opt, text="字幕", variable=self.subtitle_var).grid(row=1, column=0, sticky=tk.W)
        ttk.Checkbutton(opt, text="弹幕", variable=self.danmaku_var).grid(row=1, column=1, sticky=tk.W)
        ttk.Checkbutton(opt, text="封面", variable=self.cover_var).grid(row=2, column=0, sticky=tk.W)
        ttk.Checkbutton(opt, text="NFO", variable=self.nfo_var).grid(row=2, column=1, sticky=tk.W)

        # 右侧：Notebook 分集/任务
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(right)
        nb.pack(fill=tk.BOTH, expand=True)
        self.nb = nb

        # 分集页
        ep_frame = ttk.Frame(nb)
        nb.add(ep_frame, text="分集列表")
        cols = ("sel", "idx", "title", "duration", "cid")
        self.ep_tree = ttk.Treeview(ep_frame, columns=cols, show="headings", selectmode="extended")
        self.ep_tree.heading("sel", text="选")
        self.ep_tree.heading("idx", text="#")
        self.ep_tree.heading("title", text="标题")
        self.ep_tree.heading("duration", text="时长")
        self.ep_tree.heading("cid", text="CID")
        self.ep_tree.column("sel", width=36, anchor=tk.CENTER)
        self.ep_tree.column("idx", width=44, anchor=tk.CENTER)
        self.ep_tree.column("title", width=480, anchor=tk.W)
        self.ep_tree.column("duration", width=80, anchor=tk.CENTER)
        self.ep_tree.column("cid", width=120, anchor=tk.CENTER)
        ep_scroll = ttk.Scrollbar(ep_frame, orient=tk.VERTICAL, command=self.ep_tree.yview)
        self.ep_tree.configure(yscrollcommand=ep_scroll.set)
        self.ep_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ep_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.ep_tree.bind("<Button-1>", self.on_ep_click)
        ep_btns = ttk.Frame(ep_frame)
        ep_btns.pack(fill=tk.X, pady=4)
        ttk.Button(ep_btns, text="全选", command=lambda: self._toggle_all(True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(ep_btns, text="全不选", command=lambda: self._toggle_all(False)).pack(side=tk.LEFT)
        self.ep_count_var = tk.StringVar(value="")
        ttk.Label(ep_btns, textvariable=self.ep_count_var).pack(side=tk.RIGHT)

        # 任务页
        task_frame = ttk.Frame(nb)
        nb.add(task_frame, text="任务队列")
        tcols = ("title", "status", "progress", "speed", "size", "output")
        self.task_tree = ttk.Treeview(task_frame, columns=tcols, show="headings")
        for c, t, w in [("title", "标题", 260), ("status", "状态", 70),
                        ("progress", "进度", 90), ("speed", "速度", 90),
                        ("size", "大小", 90), ("output", "输出路径", 260)]:
            self.task_tree.heading(c, text=t)
            self.task_tree.column(c, width=w, anchor=tk.W if c in ("title", "output") else tk.CENTER)
        t_scroll = ttk.Scrollbar(task_frame, orient=tk.VERTICAL, command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=t_scroll.set)
        self.task_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        t_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        task_btns = ttk.Frame(task_frame)
        task_btns.pack(fill=tk.X, pady=4)
        ttk.Button(task_btns, text="暂停", command=lambda: self._task_action("pause")).pack(side=tk.LEFT, padx=4)
        ttk.Button(task_btns, text="继续", command=lambda: self._task_action("resume")).pack(side=tk.LEFT)
        ttk.Button(task_btns, text="取消", command=lambda: self._task_action("cancel")).pack(side=tk.LEFT, padx=4)
        ttk.Button(task_btns, text="重试失败", command=self.on_retry_failed).pack(side=tk.LEFT)
        ttk.Button(task_btns, text="恢复中断任务", command=self.on_resume_interrupted).pack(side=tk.LEFT, padx=4)
        ttk.Button(task_btns, text="清除已完成", command=self.on_clear_done).pack(side=tk.RIGHT)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(task_frame, textvariable=self.summary_var).pack(fill=tk.X)

        # 底部日志
        log_frame = ttk.LabelFrame(body, text="日志", padding=4)
        log_frame.pack(fill=tk.X, pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=7, wrap=tk.WORD, font=("Menlo", 11))
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(state=tk.DISABLED)

    def _redirect_output(self) -> None:
        """把 manager 的 print 重定向到 GUI 日志（保存原 stdout）。"""
        self._orig_stdout = None
        # 只重定向 manager 模块的 print，需在后台任务中临时替换；这里用日志 hook 方式
        logging.basicConfig(level=logging.INFO)
        handler = _TkLogHandler(self.events)
        logging.getLogger("bili").addHandler(handler)

    def _log(self, msg: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ---------- 任务持久化加载 ----------
    def _load_existing_tasks(self) -> None:
        tasks = self.storage.list_tasks()
        for t in tasks:
            self.task_map[t.task_id] = t
            self._insert_task_row(t)
        self._update_summary()
        if tasks:
            self.nb.select(1)
            self._log(f"已加载 {len(tasks)} 条历史任务")

    # ---------- 事件循环 ----------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "toast":
                    messagebox.showinfo("提示", str(payload))
                elif kind == "error":
                    messagebox.showerror("错误", str(payload))
                elif kind == "parse_done":
                    self._on_parse_done(payload)
                elif kind == "parse_fail":
                    self.parse_btn.configure(state=tk.NORMAL)
                    self._log("解析失败: " + str(payload))
                    messagebox.showerror("解析失败", str(payload))
                elif kind == "task_update":
                    self._refresh_task_row(payload)
                elif kind == "download_done":
                    self._on_download_done(payload)
                elif kind == "login_done":
                    self._on_login_done(payload)
                elif kind == "login_status":
                    self._on_login_status(payload)
                elif kind == "show_qr":
                    self._show_qr_window(payload[0], payload[1])
                elif kind == "qr_status":
                    if self._qr_status is not None and payload:
                        try:
                            self._qr_status.configure(text=str(payload))
                        except Exception:
                            pass
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    # ---------- 解析 ----------
    def on_parse(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入 B 站链接或 BV/av 号")
            return
        self.current_result = None
        self.ep_tree.delete(*self.ep_tree.get_children())
        self.info_var.set("正在解析...")
        self.parse_btn.configure(state=tk.DISABLED)
        self._log(f"解析: {shorten(url, 70)}")

        def work():
            try:
                result = self.parser.parse(url)
                self.events.put(("parse_done", result))
            except Exception as exc:
                self.events.put(("parse_fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_parse_done(self, result: ParseResult) -> None:
        self.parse_btn.configure(state=tk.NORMAL)
        self.current_result = result
        self.info_var.set(f"{result.series_title}\n{result.type} · {len(result.episodes)} 集 · ID: {result.media_id}")
        self.ep_tree.delete(*self.ep_tree.get_children())
        for ep in result.episodes:
            self.ep_tree.insert("", tk.END, iid=str(ep.index),
                                values=("✓", ep.index, ep.display_title,
                                        format_duration(ep.duration), ep.cid))
        self.selected_indices = {ep.index for ep in result.episodes}
        self._sync_range_from_selection()
        self._update_ep_count()
        self._log(f"解析完成: {result.series_title}，共 {len(result.episodes)} 集")

    def on_ep_click(self, event) -> None:
        """点击“选”列切换勾选。"""
        region = self.ep_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.ep_tree.identify_column(event.x)
        if col != "#1":  # 第一列为“选”
            return
        row = self.ep_tree.identify_row(event.y)
        if not row:
            return
        ep = next((e for e in (self.current_result.episodes if self.current_result else [])
                   if str(e.index) == row), None)
        if not ep:
            return
        if ep.index in self.selected_indices:
            self.selected_indices.discard(ep.index)
            mark = " "
        else:
            self.selected_indices.add(ep.index)
            mark = "✓"
        self.ep_tree.set(row, "sel", mark)
        self._sync_range_from_selection()
        self._update_ep_count()

    def _toggle_all(self, select: bool) -> None:
        if not self.current_result:
            return
        if select:
            self.selected_indices = {ep.index for ep in self.current_result.episodes}
        else:
            self.selected_indices.clear()
        for ep in self.current_result.episodes:
            self.ep_tree.set(str(ep.index), "sel", "✓" if select else " ")
        self._sync_range_from_selection()
        self._update_ep_count()

    def _sync_range_from_selection(self) -> None:
        if not self.current_result:
            return
        all_idx = [ep.index for ep in self.current_result.episodes]
        sel = sorted(self.selected_indices)
        self.range_var.set("all" if sel == all_idx else _indices_to_range(sel))

    def _update_ep_count(self) -> None:
        self.ep_count_var.set(f"已选 {len(self.selected_indices)} 集")

    # ---------- 下载 ----------
    def on_download(self) -> None:
        if self.downloading:
            messagebox.showwarning("提示", "已有下载任务正在进行")
            return
        if not self.current_result:
            messagebox.showwarning("提示", "请先解析链接")
            return

        range_text = self.range_var.get().strip()
        try:
            indices = parse_range_selection(range_text or "all", len(self.current_result.episodes))
        except Exception:
            indices = sorted(self.selected_indices)
        if not indices:
            messagebox.showwarning("提示", "没有选中的分集")
            return

        # 应用选项到配置
        quality = int(self.quality_var.get() or 0)
        self.config.update(
            quality=quality,
            save_subtitle=bool(self.subtitle_var.get()),
            save_danmaku=bool(self.danmaku_var.get()),
            save_cover=bool(self.cover_var.get()),
            save_nfo=bool(self.nfo_var.get()),
        )
        self.config.save()

        try:
            tasks = self.manager.build_tasks(
                self.current_result, indices,
                quality=quality,
                codec_pref=str(self.config.get("codec_pref") or ""),
            )
        except Exception as exc:
            messagebox.showerror("错误", f"构建任务失败: {exc}")
            return
        if not tasks:
            messagebox.showinfo("提示", "没有需要下载的任务（可能全部已下载）")
            return

        for t in tasks:
            self.task_map[t.task_id] = t
            self._insert_task_row(t)
        self.nb.select(1)
        self.downloading = True
        self.download_btn.configure(state=tk.DISABLED)
        self.parse_btn.configure(state=tk.DISABLED)

        def work():
            # 临时把 manager 的 print 重定向到队列，捕获完成信息
            import contextlib
            orig = None
            try:
                import sys
                orig = sys.stdout
                sys.stdout = QueueWriter(self.events)  # type: ignore
                self.manager.run(tasks)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                if orig is not None:
                    import sys
                    sys.stdout = orig
                self.events.put(("download_done", None))

        threading.Thread(target=work, daemon=True).start()
        self._log(f"开始下载 {len(tasks)} 个任务")

    def _on_download_done(self, _payload) -> None:
        self.downloading = False
        self.download_btn.configure(state=tk.NORMAL)
        self.parse_btn.configure(state=tk.NORMAL)
        for task_id, task in self.task_map.items():
            self._refresh_task_row(task)
        self._update_summary()

    # ---------- 任务控制 ----------
    def _selected_task_ids(self) -> List[str]:
        ids = []
        for iid in self.task_tree.selection():
            ids.append(iid)
        return ids

    def _task_action(self, action: str) -> None:
        ids = self._selected_task_ids()
        if not ids:
            messagebox.showinfo("提示", "请先在任务队列中选择任务")
            return
        def work():
            for tid in ids:
                try:
                    if action == "pause":
                        self.manager.pause(tid)
                    elif action == "resume":
                        self.manager.resume(tid)
                    elif action == "cancel":
                        self.manager.cancel(tid)
                    t = self.storage.get_task(tid)
                    if t:
                        self.task_map[tid] = t
                        self.events.put(("task_update", t))
                except Exception as exc:
                    self.events.put(("log", f"任务操作失败 {tid}: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def on_retry_failed(self) -> None:
        tasks = self.manager.retry_failed()
        for t in tasks:
            self.task_map[t.task_id] = t
            self._refresh_task_row(t)
        messagebox.showinfo("提示", f"已重置 {len(tasks)} 个失败任务")

    def on_resume_interrupted(self) -> None:
        if self.downloading:
            messagebox.showwarning("提示", "已有下载任务正在进行")
            return
        interrupted = self.storage.mark_interrupted()
        tasks = self.storage.list_tasks([STATUS_PENDING, STATUS_PAUSED, STATUS_DOWNLOADING])
        if not tasks:
            messagebox.showinfo("提示", "没有可恢复的任务")
            return
        for t in tasks:
            self.task_map[t.task_id] = t
        self.downloading = True
        self.download_btn.configure(state=tk.DISABLED)
        self.nb.select(1)
        self._log(f"恢复 {len(tasks)} 个任务{('（重置 ' + str(interrupted) + ' 个中断项）') if interrupted else ''}")

        def work():
            import sys
            orig = sys.stdout
            try:
                sys.stdout = QueueWriter(self.events)  # type: ignore
                self.manager.run(tasks)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                sys.stdout = orig
                self.events.put(("download_done", None))
        threading.Thread(target=work, daemon=True).start()

    def on_clear_done(self) -> None:
        self.storage.clear_tasks(only_terminal=True)
        for tid, t in list(self.task_map.items()):
            if t.status in TERMINAL_STATUSES:
                self.task_map.pop(tid, None)
                if self.task_tree.exists(tid):
                    self.task_tree.delete(tid)
        self._update_summary()

    # ---------- 任务行 UI ----------
    def _insert_task_row(self, task: DownloadTask) -> None:
        if self.task_tree.exists(task.task_id):
            self._refresh_task_row(task)
            return
        self.task_tree.insert("", tk.END, iid=task.task_id, values=(
            shorten(task.episode.display_title, 34),
            STATUS_TEXT.get(task.status, task.status),
            f"{task.progress:.1f}%",
            format_speed(task.speed) if task.speed else "-",
            format_size(task.downloaded_bytes) if task.downloaded_bytes else "-",
            shorten(task.output_path, 40),
        ))

    def _refresh_task_row(self, task: DownloadTask) -> None:
        if not self.task_tree.exists(task.task_id):
            self._insert_task_row(task)
            return
        self.task_tree.item(task.task_id, values=(
            shorten(task.episode.display_title, 34),
            STATUS_TEXT.get(task.status, task.status),
            f"{task.progress:.1f}%",
            format_speed(task.speed) if task.speed else "-",
            format_size(task.downloaded_bytes) if task.downloaded_bytes else "-",
            shorten(task.output_path, 40) or (task.error[:30] if task.error else ""),
        ))
        self._update_summary()

    def _update_summary(self) -> None:
        tasks = list(self.task_map.values())
        done = sum(1 for t in tasks if t.status == STATUS_DONE)
        failed = sum(1 for t in tasks if t.status == STATUS_FAILED)
        active = sum(1 for t in tasks if t.status in (STATUS_DOWNLOADING, STATUS_PENDING, STATUS_MERGING))
        self.summary_var.set(f"总计 {len(tasks)} · 下载中/待处理 {active} · 完成 {done} · 失败 {failed}")

    # ---------- 登录 ----------
    def _update_login_status(self) -> None:
        def work():
            try:
                info = self.api.account_info()
                self.events.put(("login_status", info))
            except Exception:
                self.events.put(("login_status", {"isLogin": self.api.is_logged_in,
                                                  "uname": "", "vip": False}))
        threading.Thread(target=work, daemon=True).start()

    def on_login(self) -> None:
        if self.api.is_logged_in:
            if messagebox.askyesno("登录", "当前已登录，是否重新扫码登录？"):
                self.api.clear_cookies()
            else:
                return
        self.login_btn.configure(state=tk.DISABLED)
        self._log("正在获取二维码...")

        def work():
            try:
                # 先生成二维码拿到 URL，再通过回调在 UI 线程显示
                def on_qr(url: str, key: str):
                    self.events.put(("show_qr", (url, key)))
                self.api.qr_login(timeout=180, on_qr_url=on_qr)
                self.events.put(("login_done", {"success": True}))
            except Exception as exc:
                self.events.put(("login_done", {"success": False, "error": str(exc)}))

        threading.Thread(target=work, daemon=True).start()

    def _on_login_status(self, info: dict) -> None:
        if info.get("isLogin"):
            name = info.get("uname") or "用户"
            self.login_label.configure(text=f"已登录：{name}", foreground="#27ae60")
        else:
            self.login_label.configure(text="未登录", foreground="#c0392b")

    def _on_login_done(self, result: dict) -> None:
        self.login_btn.configure(state=tk.NORMAL)
        self._close_qr_window()
        if result.get("success"):
            try:
                info = self.api.account_info()
            except Exception:
                info = {}
            name = info.get("uname") or "用户"
            self.login_label.configure(text=f"已登录：{name}", foreground="#27ae60")
            self._log(f"登录成功：{name}")
            messagebox.showinfo("登录", f"登录成功：{name}")
        else:
            self._log("登录失败: " + str(result.get("error", "")))
            messagebox.showerror("登录失败", str(result.get("error", "未知错误")))

    def _show_qr_window(self, url: str, key: str) -> None:
        """弹出二维码窗口，显示二维码图片（优先本地生成，失败则显示链接）。"""
        self._close_qr_window()
        win = tk.Toplevel(self)
        win.title("扫码登录")
        win.geometry("340x420")
        win.transient(self)
        self._qr_window = win
        ttk.Label(win, text="请用 B 站 App 扫描二维码", font=("Helvetica", 12, "bold")).pack(pady=8)
        canvas = tk.Canvas(win, width=300, height=300, bg="white")
        canvas.pack()
        self._qr_canvas = canvas
        status = ttk.Label(win, text="等待扫码...")
        status.pack(pady=6)
        self._qr_status = status

        # 生成二维码图片（尝试用纯 Python 生成显示，失败则显示 URL 文本）
        img = self._make_qr_image(url)
        if img is not None:
            self._qr_photo = img
            canvas.create_image(150, 150, image=img)
        else:
            canvas.create_text(150, 140, text="二维码生成失败\n请复制下方链接用浏览器打开", width=280)
        # 显示可复制链接
        link = tk.Text(win, height=3, wrap=tk.WORD)
        link.insert(tk.END, url)
        link.configure(state=tk.DISABLED)
        link.pack(fill=tk.X, padx=10)
        ttk.Button(win, text="复制链接", command=lambda: self.clipboard_append(url)).pack(pady=4)
        self._poll_qr_status(key)

    def _make_qr_image(self, url: str):
        """尝试用二维码算法生成 PhotoImage；无第三方库时用简化矩阵图替代。"""
        try:
            # 优先使用 PIL（若环境自带）
            from PIL import Image  # type: ignore
            import qrcode  # type: ignore
            img = qrcode.make(url)
            photo = ImageTkPhotoImage(img)
            return photo
        except Exception:
            pass
        # 无第三方库：生成一个占位图 + 提示复制 URL
        return None

    def _poll_qr_status(self, key: str) -> None:
        """轮询扫码状态，更新二维码窗口标题。"""
        if self._qr_window is None or not self._qr_window.winfo_exists():
            return
        def work():
            try:
                res = self.api.get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                                   params={"qrcode_key": key}, wbi=False)
                state = res.get("code") if isinstance(res, dict) else 0
                msg = {86101: "未扫描", 86090: "已扫描，请在 App 确认", 86038: "已过期"}.get(state, "")
                if state == 0:
                    return
                self.events.put(("qr_status", msg))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        self._poll_job = self.after(2000, lambda: self._poll_qr_status(key))

    def _close_qr_window(self) -> None:
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if self._qr_window is not None:
            try:
                self._qr_window.destroy()
            except Exception:
                pass
            self._qr_window = None

    # ---------- 设置 ----------
    def on_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("设置")
        win.geometry("460x480")
        win.transient(self)
        vars: Dict[str, Any] = {}

        row = 0
        def add_entry(label: str, key: str, width: int = 32):
            nonlocal row
            ttk.Label(win, text=label).grid(row=row, column=0, sticky=tk.W, padx=10, pady=5)
            v = tk.StringVar(value=str(self.config.get(key) or ""))
            ttk.Entry(win, textvariable=v, width=width).grid(row=row, column=1, sticky=tk.W, pady=5)
            vars[key] = v
            row += 1

        ttk.Label(win, text="下载设置", font=("Helvetica", 11, "bold")).grid(row=row, column=0, columnspan=2, padx=10, pady=(10, 4))
        row += 1
        add_entry("下载目录：", "download_dir")
        add_entry("分片线程数 (4-32)：", "threads")
        add_entry("并发任务数 (1-8)：", "concurrency")
        add_entry("限速 KB/s (0=不限)：", "speed_limit_kbps")
        add_entry("命名模板：", "name_template", 34)
        ttk.Label(win, text="网络", font=("Helvetica", 11, "bold")).grid(row=row, column=0, columnspan=2, padx=10, pady=(10, 4))
        row += 1
        add_entry("代理 (http/socks5)：", "proxy")
        add_entry("请求间隔(秒)：", "request_interval")

        def save():
            try:
                updates = {}
                for key, v in vars.items():
                    raw = v.get().strip()
                    old = self.config.get(key)
                    if isinstance(old, bool):
                        updates[key] = raw.lower() in ("1", "true", "yes")
                    elif isinstance(old, int):
                        updates[key] = int(float(raw or 0))
                    elif isinstance(old, float):
                        updates[key] = float(raw or 0)
                    else:
                        updates[key] = raw
                self.config.update(**updates)
                self.config.save()
                self._log("设置已保存")
                win.destroy()
            except Exception as exc:
                messagebox.showerror("错误", f"保存设置失败: {exc}", parent=win)
        ttk.Button(win, text="保存", command=save).grid(row=row, column=0, columnspan=2, pady=14)
        self._settings_window = win

    # ---------- 关闭 ----------
    def _on_close(self) -> None:
        self.downloading = False
        self._stop_flag.set()
        self._close_qr_window()
        try:
            self.destroy()
        except Exception:
            pass


class _TkLogHandler(logging.Handler):
    """把 bili.* 日志转发到 GUI 事件队列。"""
    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


def _indices_to_range(indices: List[int]) -> str:
    if not indices:
        return ""
    parts = []
    start = prev = indices[0]
    for n in indices[1:]:
        if n == prev + 1:
            prev = n
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _ImageTkPhotoImage(img):
    """把 PIL 图片转成 Tk PhotoImage。"""
    import tkinter as tk  # noqa
    from PIL import ImageTk  # type: ignore
    return ImageTk.PhotoImage(img)


# 兼容别名，供 _make_qr_image 使用
ImageTkPhotoImage = _ImageTkPhotoImage


def main() -> None:
    """GUI 入口：任何启动异常都写入日志并尽量弹窗提示，避免“静默空白”。"""
    import traceback
    log_path = os.path.join(APP_DIR, "gui_error.log")
    try:
        app = DownloadApp()
        # 确保窗口一定可见并置前（避免被其他窗口遮挡或最小化的观感）
        try:
            app.deiconify()
            app.lift()
            app.attributes("-topmost", True)
            app.after(600, lambda: app.attributes("-topmost", False))
            app.focus_force()
        except tk.TclError:
            pass
        app.mainloop()
    except Exception:
        msg = traceback.format_exc()
        try:
            ensure_dir(APP_DIR)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("BiliDownload GUI 启动失败\n" + msg)
        except Exception:
            log_path = "<无法写入日志>"
        try:
            import tkinter.messagebox as _mb
            _mb.showerror("BiliDownload 启动失败", f"{msg}\n\n日志：{log_path}")
        except Exception:
            print("BiliDownload 启动失败：\n" + msg, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
