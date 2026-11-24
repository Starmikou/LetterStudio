# -*- coding: utf-8 -*-
"""Letter Studio —— 给 Blender 场景批量造文字素材的小工具

双击 LetterStudio.pyw 或运行 python LetterStudio.py 即可。

流程：
  1. 选字号（这个字号 = 文字最终在 1920x1080 画面里占多少像素高）
  2. 选字体（点「查看系统字体」可以预览本机所有字体）
  3. 输入文字；公式用 $+LaTeX 语法
  4. 点「导出到 Blender 文件」，选目标 .blend
  5. 打开那个 .blend，文字就在世界原点 (0,0,0)
"""
import os
import sys
import io
import json
import math
import queue
import shutil
import tempfile
import threading
import subprocess
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 打包成无控制台程序时 stdout 可能是 None，subprocess 会因此报错
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, io.StringIO())

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 打包成 exe 后代码在 _MEIPASS 里，而用户配置要写到 exe 旁边
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = HERE
RES_DIR = getattr(sys, "_MEIPASS", HERE)


def _part(name):
    """取旁边的辅助脚本（源码运行时在本目录，打包后在解包目录）"""
    p = os.path.join(RES_DIR, name)
    return p if os.path.exists(p) else os.path.join(HERE, name)


BLENDER_CANDIDATES = [
    r"D:\mmd工具包\blender\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe",
]
DEFAULT_FONT = "Arial"
DEFAULT_SIZE = 72
SUPERSAMPLE = 4          # PNG 超采样倍数：最终画面 72px 的字母，出图用 288px，缩放不糊
DEFAULT_LATEX = r"E=mc^2"

# 主字体缺字形时用的后备字体（顺序即优先级）
CJK_FALLBACK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\NotoSansSC-Regular.otf",
]

MIXTEX_URL = "https://miktex.org/download"
BLENDER_URL = "https://www.blender.org/download/"
MIXTEX_CN_URL = "https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/win32/miktex/setup/windows-x64/"
NOTO_URL = "https://fonts.google.com/noto/specimen/Noto+Sans+SC"

APP_TITLE = "Letter Studio — Blender 文字素材生成器"


class LatexMissing(RuntimeError):
    """本机没有 LaTeX 编译器时抛出，界面上会弹出安装引导"""


def _reg_blender_dirs():
    """从注册表和常见安装目录里找 Blender"""
    out = []
    try:
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in roots:
            try:
                k = winreg.OpenKey(hive, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if "blender" not in sub.lower():
                    continue
                try:
                    kk = winreg.OpenKey(k, sub)
                    loc, _ = winreg.QueryValueEx(kk, "InstallLocation")
                    kk.Close()
                    if loc and os.path.isdir(loc):
                        out.append(loc)
                except OSError:
                    pass
            k.Close()
    except Exception:
        pass
    for base in (r"C:\Program Files\Blender Foundation",
                 os.path.expandvars(r"%ProgramFiles%\Blender Foundation"),
                 r"D:\Program Files\Blender Foundation"):
        if os.path.isdir(base):
            try:
                for d in os.listdir(base):
                    full = os.path.join(base, d)
                    if os.path.isdir(full):
                        out.append(full)
            except OSError:
                pass
    return out


def find_blender():
    """按「配置 → 已知路径 → 注册表/安装目录 → PATH」的顺序找 blender.exe"""
    try:
        cfgp = load_cfg().get("blender")
    except Exception:
        cfgp = None
    if cfgp and os.path.exists(cfgp):
        return cfgp
    for p in BLENDER_CANDIDATES:
        if os.path.exists(p):
            return p
    cands = []
    for d in _reg_blender_dirs():
        cands.append(os.path.join(d, "blender.exe"))
        try:
            for sub in os.listdir(d):
                cands.append(os.path.join(d, sub, "blender.exe"))
        except OSError:
            pass
    for p in cands:
        if os.path.exists(p):
            return p
    found = shutil.which("blender")
    return found or ""


def find_latex():
    """返回 (latex.exe, dvisvgm.exe)；缺一个就返回 None"""
    win = sys.platform == "win32"
    want = ("latex.exe", "dvisvgm.exe") if win else ("latex", "dvisvgm")
    out = []
    for n in want:
        p = shutil.which(n)
        if not p:
            for base in (r"D:\MiKTeX\miktex\bin\x64",
                         r"C:\Program Files\MiKTeX\miktex\bin\x64",
                         r"C:\MiKTeX\miktex\bin\x64"):
                q = os.path.join(base, n)
                if os.path.exists(q):
                    p = q
                    break
        out.append(p or None)
    return out[0], out[1]


def find_python():
    for p in (r"D:\python\python.exe", sys.executable):
        if p and os.path.exists(p):
            return p
    return sys.executable


CFG_PATH = os.path.join(APP_DIR, "_cache", "settings.json")


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 字形覆盖检测
def _mask_sig(mask):
    """字形的指纹（尺寸 + 像素和），用来识别「缺字时画出来的那个空方框」

    注意：Pillow 的 mask 是 ImagingCore，没有 tobytes()，要先转成灰度图。
    """
    from PIL import Image
    try:
        data = Image.frombytes("L", mask.size, bytes(mask)).tobytes()
    except Exception:
        try:
            data = bytes(mask)
        except Exception:
            data = b""
    return (tuple(mask.size), sum(data))


def glyph_coverage(line, main_font_path):
    """{「字体文件名|字符」: 有没有字形}

    为什么在母进程算：Blender 自带的 Python 没有 PIL，而 Blender 自己的 FONT 对象
    又判断不出缺字——缺字时它画一个 .notdef 方框，和真字形没法区分。
    这里用 PIL/FreeType 画出字形，并和该字体的 .notdef 方框比对。
    """
    from PIL import ImageFont
    cov = {}
    cands = [main_font_path] if main_font_path else []
    cands += [p for p in CJK_FALLBACK_FONTS if os.path.exists(p)]
    for path in cands:
        key_base = os.path.basename(path).lower()
        try:
            ft = ImageFont.truetype(path, 40)
        except Exception:
            continue
        try:
            nd_sig = _mask_sig(ft.getmask("\uffff"))
        except Exception:
            nd_sig = None
        for ch in line:
            if ch.isspace():
                continue
            k = "%s|%s" % (key_base, ch)
            if k in cov:
                continue
            try:
                sig = _mask_sig(ft.getmask(ch))
            except Exception:
                cov[k] = False
                continue
            cov[k] = (sig[1] > 0) and (sig != nd_sig)
    return cov


# ---------------------------------------------------------------- 后台任务
class Job:
    """在子线程里跑命令，把日志丢给回调"""

    def __init__(self, on_done):
        self.q = queue.Queue()
        self.on_done = on_done

    def run(self, cmd, cwd=None, timeout=600, tag="task"):
        def worker():
            try:
                r = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                                   capture_output=True, text=True,
                                   errors="replace", stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.on_done(tag, r.returncode, (r.stdout or "") + (r.stderr or ""))
            except subprocess.TimeoutExpired:
                self.on_done(tag, -1, "超时：命令执行超过 %d 秒" % timeout)
            except Exception:
                self.on_done(tag, -1, traceback.format_exc())

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------- 字体预览窗
class FontPicker(tk.Toplevel):
    def __init__(self, master, fonts, current, on_pick):
        super().__init__(master)
        self.title("选择字体（本机已安装）")
        self.geometry("760x560")
        self.fonts = fonts
        self.on_pick = on_pick
        self.current = current

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="搜索：").pack(side="left")
        self.var_q = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.var_q)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<KeyRelease>", lambda e: self.refresh())
        ttk.Button(top, text="关闭", command=self.destroy).pack(side="left", padx=6)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.tree = ttk.Treeview(body, columns=("name", "path"), show="tree",
                                 selectmode="browse")
        self.tree.column("#0", width=300)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(right, text="预览", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.preview = tk.Canvas(right, width=430, height=300, bg="white",
                                 highlightthickness=1, highlightbackground="#ccc")
        self.preview.pack(fill="both", expand=True, pady=4)
        ttk.Label(right, text="双击左侧字体名即可选用",
                  foreground="#666").pack(anchor="w")
        ttk.Button(right, text="用这个字体", command=self.pick).pack(anchor="e", pady=6)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.draw_preview())
        self.tree.bind("<Double-1>", lambda e: self.pick())

        self.refresh()
        self.transient(master)

    def refresh(self):
        q = self.var_q.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for f in self.fonts:
            if q and q not in f["family"].lower():
                continue
            self.tree.insert("", "end", iid=f["family"], text=f["family"],
                             values=(f["path"],))
        kids = self.tree.get_children()
        if kids:
            iid = self.current if self.current in kids else kids[0]
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _sel_font(self):
        sel = self.tree.selection()
        if not sel:
            return None
        fam = sel[0]
        return next((f for f in self.fonts if f["family"] == fam), None)

    def draw_preview(self):
        f = self._sel_font()
        self.preview.delete("all")
        if not f:
            return
        text = self.preview_text or "Aa 汉字 123 ∑∫√ αβγ"
        y = 26
        for px in (22, 34, 48):
            try:
                self.preview.create_text(14, y, anchor="nw", fill="#111",
                                         font=(f["family"], px), text=text[:26])
            except Exception:
                pass
            y += px + 26
        self.preview.create_text(14, 250, anchor="nw", fill="#888",
                                 font=("Consolas", 8), text=f["path"])

    preview_text = "Aa Bb 汉字 123 ∑ ∫ √ α β γ"

    def pick(self):
        f = self._sel_font()
        if f:
            self.on_pick(f["family"])
            self.destroy()


# ---------------------------------------------------------------- 主窗口
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("880x680")
        self.minsize(760, 560)

        self.cfg = load_cfg()
        self.fonts = []
        self.rendered = []      # 每个待导出项：dict(name, png, png_w, png_h, design_px, text, mode, preview)
        self.busy = False

        self.var_size = tk.IntVar(value=int(self.cfg.get("size", DEFAULT_SIZE)))
        self.var_font = tk.StringVar(value=self.cfg.get("font", DEFAULT_FONT))
        self.var_blender = tk.StringVar(value=self.cfg.get("blender") or find_blender())
        self.var_status = tk.StringVar(value="就绪")
        self.var_warn = tk.StringVar(value="")

        self._build_ui()
        self.after(80, self._load_fonts_async)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI
    def _build_ui(self):
        pad = dict(padx=12, pady=5)

        # 顶部：字号 / 字体 / Blender
        box = ttk.LabelFrame(self, text="1. 基本设置")
        box.pack(fill="x", **pad)

        r1 = ttk.Frame(box)
        r1.pack(fill="x", padx=8, pady=4)
        ttk.Label(r1, text="字号（像素高，相对 1080p 画面）：").pack(side="left")
        sp = ttk.Spinbox(r1, from_=8, to=1000, width=8, textvariable=self.var_size)
        sp.pack(side="left")
        ttk.Label(r1, text="～%dpx 的字母在画面里就是 %d 像素高"
                  % (DEFAULT_SIZE, DEFAULT_SIZE), foreground="#777").pack(side="left", padx=8)

        r2 = ttk.Frame(box)
        r2.pack(fill="x", padx=8, pady=4)
        ttk.Label(r2, text="字体：").pack(side="left")
        self.cmb_font = ttk.Combobox(r2, textvariable=self.var_font, width=34)
        self.cmb_font.pack(side="left")
        ttk.Button(r2, text="查看系统字体…", command=self.open_font_picker).pack(side="left", padx=6)
        self.lbl_fontstat = ttk.Label(r2, text="正在读取字体列表…", foreground="#777")
        self.lbl_fontstat.pack(side="left", padx=8)

        r3 = ttk.Frame(box)
        r3.pack(fill="x", padx=8, pady=4)
        ttk.Label(r3, text="Blender：").pack(side="left")
        ttk.Entry(r3, textvariable=self.var_blender).pack(side="left", fill="x",
                                                         expand=True)
        ttk.Button(r3, text="浏览…", command=self.pick_blender).pack(side="left", padx=6)
        ttk.Button(r3, text="测试", command=self.test_blender).pack(side="left")
        ttk.Button(r3, text="环境自检…", command=self.open_health).pack(side="left", padx=(8, 0))

        # 中部：文字输入
        box2 = ttk.LabelFrame(self, text="2. 文字内容  （一行一条；公式用 $...$ 包起来，"
                                         "例如 $E=mc^2$ 或 $\\int_0^1 x^2 dx$）")
        box2.pack(fill="both", expand=True, **pad)

        tools = ttk.Frame(box2)
        tools.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Button(tools, text="＋ 插入公式", command=self.insert_formula).pack(side="left")
        ttk.Button(tools, text="清空", command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=6)
        self.lbl_warn = ttk.Label(tools, textvariable=self.var_warn, foreground="#b00")
        self.lbl_warn.pack(side="left", padx=8)

        self.txt = tk.Text(box2, height=9, wrap="word",
                           font=("Microsoft YaHei UI", 12), undo=True)
        self.txt.pack(fill="both", expand=True, padx=8, pady=6)
        self.txt.insert("1.0", self.cfg.get("last_text", "i\n$E=mc^2$"))

        # 按钮区
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=4)
        self.btn_preview = ttk.Button(bar, text="生成预览", command=self.do_preview)
        self.btn_preview.pack(side="left")
        self.btn_export = ttk.Button(bar, text="导出到 Blender 文件…",
                                     command=self.do_export, state="disabled")
        self.btn_export.pack(side="left", padx=8)

        # 预览 + 日志
        box3 = ttk.LabelFrame(self, text="3. 预览与日志")
        box3.pack(fill="both", expand=True, **pad)
        left = ttk.Frame(box3)
        left.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        self.canvas = tk.Canvas(left, bg="#f4f4f4", height=190,
                                highlightthickness=1, highlightbackground="#ccc")
        self.canvas.pack(fill="both", expand=True)
        self.log = tk.Text(box3, width=46, height=12, font=("Consolas", 9),
                           state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                           insertbackground="#d4d4d4")
        self.log.pack(side="left", fill="both", padx=(0, 8), pady=6)

        self.lbl_status = ttk.Label(self, textvariable=self.var_status,
                                    relief="sunken", anchor="w")
        self.lbl_status.pack(fill="x", side="bottom")

    def say(self, msg):
        self.var_status.set(msg)
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    # ---------------- 字体
    def _load_fonts_async(self):
        def work():
            try:
                import letter_fonts as lf
                fonts = lf.scan()
            except Exception:
                fonts = []
            self.after(0, lambda: self._fonts_ready(fonts))
        threading.Thread(target=work, daemon=True).start()

    def _fonts_ready(self, fonts):
        self.fonts = fonts
        names = [f["family"] for f in fonts]
        self.cmb_font["values"] = names
        if self.var_font.get() not in names:
            import letter_fonts as lf
            hit = lf.pick(fonts, self.var_font.get()) or lf.pick(fonts, DEFAULT_FONT)
            if hit:
                self.var_font.set(hit["family"])
        self.lbl_fontstat.configure(text="%d 个字体" % len(names))
        self.say("已载入 %d 个字体" % len(names))

    def open_font_picker(self):
        if not self.fonts:
            messagebox.showinfo("稍等", "字体列表还在读取中…")
            return
        FontPicker(self, self.fonts, self.var_font.get(), self.var_font.set)

    def pick_blender(self):
        p = filedialog.askopenfilename(title="选择 blender.exe",
                                       filetypes=[("Blender", "blender.exe"), ("可执行文件", "*.exe")])
        if p:
            self.var_blender.set(p)
            self.cfg["blender"] = p
            save_cfg(self.cfg)

    def check_environment(self):
        """逐项自检：返回 [(项目, 是否通过, 状态说明, 缺失时的说明/下载地址)]"""
        rows = []

        # 1) Blender
        bl = self.var_blender.get().strip() or find_blender()
        if bl and os.path.exists(bl):
            rows.append(("Blender", True, bl, ""))
        else:
            rows.append(("Blender", False, "未找到",
                         "生成文字图必须要有 Blender（免费开源）。\n下载：%s" % BLENDER_URL))

        # 2) LaTeX（只有公式需要）
        lat, dvi = find_latex()
        if lat and dvi:
            rows.append(("LaTeX（公式排版）", True, os.path.dirname(lat), ""))
        else:
            miss = []
            if not lat:
                miss.append("latex.exe")
            if not dvi:
                miss.append("dvisvgm.exe")
            rows.append(("LaTeX（公式排版）", False,
                         "缺少 " + "、".join(miss) + " —— 只有写 $公式$ 才需要",
                         "公式排版需要 MiKTeX（免费）。\n"
                         "下载：%s\n国内镜像（下载更快）：%s\n"
                         "装完重启本程序即可，不影响中文/英文文字功能。" % (MIXTEX_URL, MIXTEX_CN_URL)))

        # 3) 中文字体（写中文才需要）
        cjk = [p for p in CJK_FALLBACK_FONTS if os.path.exists(p)]
        if cjk:
            rows.append(("中文字体", True, os.path.basename(cjk[0]), ""))
        else:
            rows.append(("中文字体", False, "未找到微软雅黑/黑体/宋体",
                         "写中文需要至少一个中文字体。Windows 一般自带宋体，\n"
                         "如果没有，可免费下载思源黑体：%s" % NOTO_URL))

        # 4) Pillow（PIL）—— 用来量字形、算缺字
        try:
            import PIL
            from PIL import Image, ImageFont  # noqa: F401
            rows.append(("图像库 Pillow", True, getattr(PIL, "__version__", "已安装"), ""))
        except Exception as e:
            rows.append(("图像库 Pillow", False, str(e)[:60],
                         "缺少 Pillow，程序无法启动。请反馈这个问题。"))

        # 5) 字体库缓存
        cache = os.path.join(HERE, "_cache", "font_cache_v6.json")
        if os.path.exists(cache):
            rows.append(("字体库缓存", True, "已建立", ""))
        else:
            rows.append(("字体库缓存", True, "首次启动会自动扫描（约 2 秒）", ""))

        # 6) 写入权限（导出要能写 .blend）
        try:
            t = os.path.join(HERE, "_cache", ".wtest")
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "w", encoding="utf-8") as f:
                f.write("x")
            os.remove(t)
            rows.append(("本地写入权限", True, "正常", ""))
        except Exception as e:
            rows.append(("本地写入权限", False, str(e)[:60],
                         "程序目录不可写，设置和预览图会存不下来。\n"
                         "建议把整个文件夹放到 D 盘或桌面再运行。"))

        return rows

    def open_health(self):
        """环境自检对话框：绿勾=可用，红叉=缺什么 + 怎么装"""
        rows = self.check_environment()
        bad = [r for r in rows if not r[1]]

        win = tk.Toplevel(self)
        win.title("环境自检")
        win.geometry("760x460")
        win.transient(self)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Label(head, text="依赖检查", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        msg = "全部就绪，可以放心用" if not bad else "有 %d 项未就绪（见下方红叉）" % len(bad)
        ttk.Label(head, text="  " + msg,
                  foreground="#0a7a0a" if not bad else "#b00").pack(side="left")

        body = tk.Frame(win)
        body.pack(fill="both", expand=True, padx=14, pady=6)

        for name, ok, detail, hint in rows:
            row = tk.Frame(body)
            row.pack(fill="x", pady=3, anchor="w")
            tk.Label(row, text="✔" if ok else "✘",
                     fg="#0a7a0a" if ok else "#c00",
                     font=("Segoe UI", 12, "bold"), width=2).pack(side="left", anchor="n")
            tk.Label(row, text=name, font=("Microsoft YaHei UI", 10, "bold"),
                     width=18, anchor="w").pack(side="left", anchor="n")
            sub = tk.Frame(row)
            sub.pack(side="left", fill="x", expand=True)
            tk.Label(sub, text=detail, fg="#333", anchor="w",
                     justify="left", wraplength=520).pack(side="top", anchor="w")
            if hint:
                tk.Label(sub, text=hint, fg="#b00", anchor="w", justify="left",
                         wraplength=520, font=("Microsoft YaHei UI", 9)).pack(side="top", anchor="w")
                for url, label in ((BLENDER_URL, "打开 Blender 下载页"),
                                   (MIXTEX_URL, "打开 MiKTeX 下载页"),
                                   (MIXTEX_CN_URL, "清华镜像（MiKTeX）"),
                                   (NOTO_URL, "思源黑体")):
                    if url in hint:
                        ttk.Button(sub, text=label,
                                   command=lambda u=url: self._open_url(u)).pack(side="left", pady=2)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=(4, 12))
        ttk.Button(btns, text="重新检查", command=lambda: (win.destroy(), self.open_health())
                   ).pack(side="left")
        ttk.Button(btns, text="复制报告", command=lambda: self._copy_health(rows)
                   ).pack(side="left", padx=8)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right")

    def _open_url(self, url):
        try:
            os.startfile(url)
        except Exception as e:
            messagebox.showinfo("请手动访问", "%s\n\n（自动打开失败：%s）" % (url, e))

    def _copy_health(self, rows):
        lines = ["Letter Studio 环境自检"]
        for name, ok, detail, hint in rows:
            lines.append("[%s] %s —— %s" % ("OK" if ok else "缺失", name, detail))
        txt = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(txt)
        messagebox.showinfo("已复制", "自检结果已复制到剪贴板，可以直接发给客服/作者。")

    def test_blender(self):
        exe = self.var_blender.get().strip()
        if not os.path.exists(exe):
            messagebox.showerror("找不到 Blender", "路径无效：\n%s" % exe)
            return
        self.say("正在测试 Blender：%s" % exe)

        def done(tag, code, out):
            ver = "?"
            for line in out.splitlines():
                if line.startswith("Blender"):
                    ver = line.strip()
                    break
            self.after(0, lambda: self.say("Blender 可用：%s" % ver))
        Job(done).run([exe, "-b", "--version"], timeout=60, tag="ver")

    # ---------------- 生成
    def current_font_path(self):
        import letter_fonts as lf
        return lf.find_path(self.fonts, self.var_font.get())

    def _lines(self):
        raw = self.txt.get("1.0", "end").splitlines()
        return [ln.strip() for ln in raw if ln.strip()]

    def insert_formula(self):
        self.txt.insert("insert", "$%s$" % DEFAULT_LATEX)

    def do_preview(self):
        if self.busy:
            return
        lines = self._lines()
        if not lines:
            messagebox.showwarning("没有内容", "先在中间输入要做的文字。")
            return
        exe = self.var_blender.get().strip()
        if not os.path.exists(exe):
            messagebox.showerror("找不到 Blender",
                                 "请在上面指定 blender.exe 的路径。")
            return
        try:
            size = int(self.var_size.get())
            if size <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("字号不对", "字号必须是正整数。")
            return

        self._set_busy(True, "正在生成预览…")
        self.rendered = []
        workdir = os.path.join(tempfile.gettempdir(), "LetterStudioWork")
        os.makedirs(workdir, exist_ok=True)

        def worker():
            try:
                items = []
                for idx, line in enumerate(lines):
                    item = self._render_line(idx, line, size, exe, workdir)
                    items.append(item)
                    self.after(0, lambda i=item: self.say("  已生成 %s" % i["label"]))
                self.after(0, lambda: self._preview_done(items))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self._preview_failed(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _render_line(self, idx, line, size, exe, workdir):
        """单行 -> PNG；公式（含 $）走 Manim 的 LaTeX，其余走 Blender 字体"""
        out_px = max(64, size * SUPERSAMPLE)
        base = "item_%02d" % idx
        png = os.path.join(workdir, base + ".png")
        meta = os.path.join(workdir, base + ".json")
        spec = os.path.join(workdir, base + ".spec.json")

        # 只有「整行就是用 $...$ 包起来的纯公式」才走 LaTeX。
        # 行内混排（中文 + $公式$）LaTeX 处理不了中文，会直接报
        # "Unicode character 混 (U+6DF7) not set up for use with LaTeX"。
        is_tex = line.strip().startswith("$") and line.strip().endswith("$") \
            and line.strip().count("$") == 2
        if not is_tex and "$" in line:
            try:
                self.say("  ! 第 %d 行含 $ 但不是纯公式（LaTeX 不支持行内中文），按普通文字渲染" % (idx + 1))
            except Exception:
                pass
        if is_tex:
            tex = line
            if tex.startswith("$") and tex.endswith("$") and len(tex) > 2:
                tex = tex[1:-1]
            tex = tex.strip()
            _lat, _dvi = find_latex()
            if not _lat or not _dvi:
                raise LatexMissing(
                    "这一行是公式（%s），但本机没有安装 LaTeX 编译器。\n"
                    "公式功能需要 MiKTeX（免费），装完重启本程序即可。" % line)
            svg = os.path.join(workdir, base + ".svg")
            py = find_python()
            r = subprocess.run([py, _part("latex_to_svg.py"),
                                "--tex", tex, "--svg", svg,
                                "--tmpdir", os.path.join(workdir, base + "_tex")],
                               capture_output=True, text=True, timeout=400,
                               errors="replace", stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (r.stdout or "") + (r.stderr or "")
            if "LATEX_OK" not in out:
                raise RuntimeError("公式渲染失败：%s\n%s" % (tex, out.strip()[:400]))
            sp = {"mode": "svg", "svg_path": svg}
            label = line
        else:
            sp = {"mode": "text", "text": line,
                  "font_path": self.current_font_path() or ""}
            label = line

        # 多行时每行要有各自的输出路径，否则后一条会盖掉前一条
        sp.update({"design_px": size, "out_px": out_px,
                   "out_png": png, "meta_json": meta,
                   "coverage": self._coverage(line)})
        with open(spec, "w", encoding="utf-8") as f:
            json.dump(sp, f, ensure_ascii=False)

        r = subprocess.run([exe, "-b", "-P", _part("letter_render.py"),
                            "--", "--spec", spec],
                           capture_output=True, text=True, timeout=600,
                           errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "") + (r.stderr or "")
        if not os.path.exists(meta):
            raise RuntimeError("渲染失败：%s\n%s" % (label, out.strip()[-600:]))
        with open(meta, "r", encoding="utf-8") as f:
            info = json.load(f)
        if not info.get("glyph_w") or not info.get("glyph_h"):
            raise RuntimeError("渲染结果里没有可见字形：%s" % label)

        # 生成透明底预览小图
        prev = os.path.join(workdir, base + "_prev.png")
        W, H = int(info.get("glyph_w") or 0), int(info.get("glyph_h") or 0)
        print("DBGWH", W, H)
        try:
            from PIL import Image
            im = Image.open(png).convert("RGBA")
            bb = im.getchannel("A").getbbox()
            if bb:
                im = im.crop(bb)
            if im.width > 420:
                k = 420.0 / im.width
                im = im.resize((420, max(1, int(im.height * k))), Image.LANCZOS)
            bg = Image.new("RGBA", (max(im.width + 20, 90), im.height + 16), (244, 244, 244, 255))
            bg.alpha_composite(im, (10, 8))
            bg.convert("RGB").save(prev)
        except Exception:
            prev = None

        return dict(name="文字_%02d" % idx, label=label, png=png, prev=prev,
                    png_w=info.get("glyph_w"), png_h=info.get("glyph_h"),
                    design_px=size, mode=sp["mode"], text=line,
                    warn=info.get("warn", ""))

    def _coverage(self, line):
        """算这行里每个字在主字体/后备字体里到底有没有字形。

        用模块级函数算，不挂在 self 上：这样测试时传进来的假对象也能直接用。
        """
        return glyph_coverage(line, self.current_font_path() or "")

    def _preview_done(self, items):
        self.rendered = items
        self._draw_previews()
        self._set_busy(False, "预览完成，可以导出了")
        self.btn_export.configure(state="normal")
        warns = [i["warn"] for i in items if i.get("warn")]
        self.var_warn.set(warns[0] if warns else "")

    def offer_latex_install(self):
        """公式需要 LaTeX 但本机没装时，给出明确的安装引导"""
        ans = messagebox.askyesno(
            "需要装一个 LaTeX（免费）",
            "你输入了公式，公式排版需要 LaTeX 编译器（MiKTeX），本机还没有装。\n\n"
            "要现在打开下载页面吗？装完重启本程序就能用了。\n\n"
            "· 下载地址：%s\n"
            "· 安装时选「为当前用户安装」最省事\n"
            "· 装完不用重启电脑，重启本程序即可" % MIXTEX_URL)
        if ans:
            try:
                os.startfile(MIXTEX_URL)
            except Exception as e:
                messagebox.showinfo("请手动访问", "%s\n\n（自动打开失败：%s）" % (MIXTEX_URL, e))

    def _preview_failed(self, msg):
        self._set_busy(False, "出错了")
        if "没有安装 LaTeX" in msg:
            self.say("缺少 LaTeX —— 请看弹窗里的安装说明")
            self.offer_latex_install()
            return
        self.say("错误：%s" % msg)
        messagebox.showerror("生成失败", msg[:1200])

    def _draw_previews(self):
        self.canvas.delete("all")
        y = 12
        for it in self.rendered:
            self.canvas.create_text(10, y, anchor="nw", fill="#444",
                                    font=("Microsoft YaHei UI", 9),
                                    text="%s  ·  %dpx  ·  %s" % (
                                        it["label"], it["design_px"],
                                        "LaTeX 公式" if it["mode"] == "svg" else "字体"))
            y += 18
            if it.get("prev") and os.path.exists(it["prev"]):
                try:
                    self._imgs = getattr(self, "_imgs", [])
                    img = tk.PhotoImage(file=it["prev"])
                    self._imgs.append(img)
                    self.canvas.create_image(20, y, anchor="nw", image=img)
                    y += img.height() + 10
                except Exception:
                    y += 10
            else:
                y += 10

    # ---------------- 导出
    def do_export(self):
        if self.busy or not self.rendered:
            return
        blend = filedialog.askopenfilename(
            title="选择要写入的 .blend 文件",
            filetypes=[("Blender 文件", "*.blend")])
        if not blend:
            return

        # 写别人的场景前先备份，改坏了能退回
        bak = blend + ".before_letter_studio"
        n = 1
        while os.path.exists(bak):
            bak = "%s.before_letter_studio.%d" % (blend, n)
            n += 1
        shutil.copyfile(blend, bak)

        exe = self.var_blender.get().strip()
        workdir = os.path.join(tempfile.gettempdir(), "LetterStudioWork")
        job_path = os.path.join(workdir, "import_job.json")
        job = {"blend": blend,
               "items": [dict(name=i["name"], png=i["png"], png_w=i["png_w"],
                              png_h=i["png_h"], design_px=i["design_px"],
                              text=i["text"]) for i in self.rendered]}
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)

        self.say("已备份原文件到：%s" % bak)
        self._set_busy(True, "正在写入 %s …" % os.path.basename(blend))

        def done(tag, code, out):
            ok = "LETTER_STUDIO_IMPORT_OK" in out
            self.after(0, lambda: self._export_done(ok, out, blend, bak))
        Job(done).run([exe, "-b", blend, "-P", _part("letter_import.py"),
                       "--", "--job", job_path], timeout=600, tag="import")

    def _export_done(self, ok, out, blend, bak=""):
        self._set_busy(False, "导出完成" if ok else "导出失败")
        if not ok:
            self.say(out.strip()[-1500:])
            messagebox.showerror("导出失败", out.strip()[-1200:])
            return
        self.say("已写入：%s" % blend)
        messagebox.showinfo(
            "完成",
            "文字已写入：\n%s\n\n"
            "用 Blender 打开这个文件就能看到，文字就在世界原点 (0,0,0)。\n"
            "想移动/旋转/缩放它，请动 *_CTRL 那个空物体（文字挂在它下面）。\n\n"
            "· 按 小键盘 0 切到相机视角，按 1080p 画幅标定过，字号=画面上的像素高\n"
            "· 原来的文件已备份到：\n%s"
            % (blend, bak))

    # ---------------- 杂项
    def _set_busy(self, busy, status):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_preview.configure(state=state)
        if busy:
            self.btn_export.configure(state="disabled")
        self.var_status.set(status)
        self.update_idletasks()

    def _on_close(self):
        self.cfg.update({
            "size": int(self.var_size.get() or DEFAULT_SIZE),
            "font": self.var_font.get(),
            "blender": self.var_blender.get(),
            "last_text": self.txt.get("1.0", "end").strip(),
        })
        save_cfg(self.cfg)
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            import tkinter.messagebox as mb
            mb.showerror("启动失败", traceback.format_exc())
        except Exception:
            print(traceback.format_exc())
