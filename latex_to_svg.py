# -*- coding: utf-8 -*-
"""LaTeX 公式 -> SVG（直接调用 latex + dvisvgm，不经过 Manim）

为什么不用 Manim：本机 Manim 0.21 调 latex 用的是 `--halt-on-error`，
而 MiKTeX 在「cwd 下含其它 latex 文件的目录」里会挑错文件跑崩；
实测同一份 .tex 换个干净目录就正常。直接调 latex+dvisvgm 一次成功。

用法（由 GUI 调用）:
  python latex_to_svg.py --tex "E=mc^2" --svg out.svg
成功 stdout: LATEX_OK <svg路径>；失败: LATEX_FAIL <原因>，返回码 1。

踩过的坑（别再改回去）:
  1) 路径含非 ASCII 时 MiKTeX 会 Emergency stop，连 .log 都不写 —— 统一在 ASCII 临时目录里编译。
  2) 编译目录必须干净：同目录里放着别的 .tex 会让 MiKTeX 挑错文件、进程直接崩（0xC0000409）。
  3) subprocess 一律 errors="replace"；本机控制台是 GBK，打印含 \ufffd 的文本会 UnicodeEncodeError，
     所以错误信息打印前要过滤。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

TEMPLATE = r"""\documentclass[preview,%(pt)dpt]{standalone}
\usepackage[english]{babel}
\usepackage{amsmath}
\usepackage{amssymb}
\begin{document}
\begin{preview}
\(%(tex)s\)
\end{preview}
\end{document}
"""

LATEX_CANDIDATES = [
    r"D:\MiKTeX\miktex\bin\x64\latex.exe",
    r"C:\Program Files\MiKTeX\miktex\bin\x64\latex.exe",
    "latex",
]
DVISVGM_CANDIDATES = [
    r"D:\MiKTeX\miktex\bin\x64\dvisvgm.exe",
    r"C:\Program Files\MiKTeX\miktex\bin\x64\dvisvgm.exe",
    "dvisvgm",
]


def _find(cands):
    for c in cands:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            w = shutil.which(c)
            if w:
                return w
    return None


def _clean(s, limit=500):
    """去掉无法在 GBK 控制台打印的字符"""
    s = " ".join((s or "").split())
    out = []
    for ch in s:
        try:
            ch.encode("gbk")
            out.append(ch)
        except Exception:
            out.append("?")
    return "".join(out)[:limit]


def _is_ascii(path):
    try:
        path.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _work_dir():
    """ASCII、可写、且每次都是全新的干净目录

    候选顺序：本程序目录下的 _texwork（权限一定够）→ 系统 TEMP。
    注意：目录名用固定前缀 + 进程号，不要用 tempfile.mkdtemp —— 某些沙箱/ACL 环境下
    新建的随机名目录会被拒绝写入（实测 PermissionError）。
    """
    cands = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "_texwork")]
    for key in ("TEMP", "TMP"):
        cands.append(os.environ.get(key))
    cands.append(tempfile.gettempdir())

    for c in cands:
        if not c:
            continue
        c = os.path.abspath(c)
        if not _is_ascii(c):
            continue
        # 每次都用一个全新的子目录，避免上次残留的 .tex/.log 干扰 MiKTeX
        for tag in range(64):
            d = os.path.join(c, "run_%d_%d" % (os.getpid(), tag))
            try:
                shutil.rmtree(d, ignore_errors=True)
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, ".w")
                with open(probe, "w") as f:
                    f.write("1")
                os.remove(probe)
                return d
            except Exception:
                continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_texwork")


def render(tex, out_svg, fontsize_pt=12, timeout=180):
    """返回 (ok, message)"""
    latex = _find(LATEX_CANDIDATES)
    dvisvgm = _find(DVISVGM_CANDIDATES)
    if not latex:
        return False, "找不到 latex.exe（请安装 MiKTeX 或 TeX Live）"
    if not dvisvgm:
        return False, "找不到 dvisvgm.exe（MiKTeX 自带，检查 PATH）"

    tex = tex.strip()
    if tex.startswith("$") and tex.endswith("$") and len(tex) > 2:
        tex = tex[1:-1].strip()
    if not tex:
        return False, "公式内容为空"

    text = TEMPLATE % {"tex": tex, "pt": fontsize_pt}
    name = "lsf_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    work = _work_dir()
    try:
        texfile = os.path.join(work, name + ".tex")
        with open(texfile, "w", encoding="utf-8") as f:
            f.write(text)

        try:
            r = subprocess.run([latex, "-interaction=nonstopmode", "-halt-on-error",
                                name + ".tex"],
                               cwd=work, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
        except Exception as e:
            return False, "调用 latex 出错：%s" % e
        if r.returncode != 0:
            log = os.path.join(work, name + ".log")
            detail = ""
            if os.path.exists(log):
                try:
                    with open(log, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.read().splitlines()
                    errs = [ln for ln in lines if ln.startswith("!")]
                    detail = " | ".join(errs[:3])
                except Exception:
                    pass
            if not detail:
                detail = (r.stdout or "")[-300:] + (r.stderr or "")[-300:]
            return False, "LaTeX 编译失败：%s" % _clean(detail)

        try:
            r2 = subprocess.run([dvisvgm, "--no-fonts", "--exact-bbox", "--clipjoin",
                                 name + ".dvi", "-o", name + ".svg"],
                                cwd=work, capture_output=True, text=True,
                                errors="replace", timeout=timeout)
        except Exception as e:
            return False, "调用 dvisvgm 出错：%s" % e
        svg = os.path.join(work, name + ".svg")
        if r2.returncode != 0 or not os.path.exists(svg):
            return False, "dvisvgm 转 SVG 失败：%s" % _clean(
                (r2.stderr or "") + (r2.stdout or ""))

        os.makedirs(os.path.dirname(os.path.abspath(out_svg)), exist_ok=True)
        shutil.copyfile(svg, out_svg)
        return True, out_svg
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tex", required=True)
    ap.add_argument("--svg", required=True)
    ap.add_argument("--pt", type=int, default=12)
    ap.add_argument("--tmpdir", default="")
    args = ap.parse_args()

    ok, msg = render(args.tex, args.svg, args.pt)
    if ok:
        print("LATEX_OK %s" % msg)
        return 0
    print("LATEX_FAIL %s" % msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
