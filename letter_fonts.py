# -*- coding: utf-8 -*-
"""扫描系统字体，供 Letter Studio 的字体选择器使用。

用 FreeType（Pillow）读字体内部的族名/字重，比手工解析 TTF 的 name 表可靠得多，
也能自动挑出每个族的「常规」字重文件。注册表里的本地化名（微软雅黑/宋体…）作为别名补上。
结果缓存到 _cache/，之后启动 <0.1s。
"""
import os
import glob
import json
import winreg

CACHE_VERSION = 6

# 纯符号/图标字体，选来做文字没意义
SKIP_FAMILIES = {
    "Wingdings", "Wingdings 2", "Wingdings 3", "Webdings", "Symbol", "Marlett",
    "MS Outlook", "Bookshelf Symbol 7", "Rod", "code128", "codicon",
    "Font Awesome 5 Brands", "Font Awesome 5 Free", "Font Awesome 6 Brands",
    "Font Awesome 6 Free", "Material Design Icons", "Phosphor", "remixicon",
    "Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Emoji",
    "Segoe UI Historic", "Segoe UI Symbol", "FontAwesome", "yyb",
    "Javanese Text", "Microsoft Himalaya", "Microsoft Yi Baiti", "Mongolian Baiti",
    "Sylfaen", "Sans Serif Collection", "Cascadia Code", "Cascadia Mono",
}
SKIP_PREFIX = ("@", "myanmar text", "nirmala ui", "ebrima", "mv boli",
               "leelawadee", "malgun", "gadugi", "yu gothic",
               "simsun-ext", "mingliu-ext", "microsoft tai le",
               "microsoft new tai lue", "microsoft phagspa", "sitka",
               "segoe print", "segoe script", "ink free")

REGULAR = {"regular", "normal", "book", "roman", ""}
BOLDISH = {"bold", "semibold", "demibold", "extrabold", "black", "heavy"}


def _cache_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "_cache"),
              os.path.join(os.environ.get("TEMP", "."), "LetterStudio")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return os.path.join(d, "font_cache_v%d.json" % CACHE_VERSION)
        except Exception:
            continue
    return None


def _font_dirs():
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    prof = os.environ.get("LOCALAPPDATA", "")
    if prof:
        dirs.append(os.path.join(prof, "Microsoft", "Windows", "Fonts"))
    return [d for d in dirs if os.path.isdir(d)]


def _registry_aliases():
    """文件名(basename) -> 注册表里的显示名集合

    注意：Windows 的注册表在这里存的基本都是英文名（比如
    'SimSun & NSimSun (TrueType)'），并不会给中文名，所以中文别名得靠
    CJK_ALIASES 那张表来补，不能指望注册表。
    """
    out = {}
    windir = os.environ.get("WINDIR", r"C:\Windows")
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
    except OSError:
        return out
    i = 0
    while True:
        try:
            name, path, _ = winreg.EnumValue(k, i)
        except OSError:
            break
        i += 1
        if not isinstance(path, str) or not path.lower().endswith((".ttf", ".otf", ".ttc")):
            continue
        full = path if os.path.isabs(path) else os.path.join(windir, "Fonts", path)
        reg = name.split(" (")[0].split(" & ")[0].strip()
        out.setdefault(os.path.basename(full).lower(), set()).add(reg)
    k.Close()
    return out


# 常见字体族的中文名。Windows 的中文版系统里这些名字是「显示名」的一部分，
# 但注册表和字体文件里往往只有英文名，所以这里手工补一份，
# 让用户在列表/搜索框里输入「宋体」「雅黑」也能选中。
CJK_ALIASES = {
    "SimSun": "宋体",
    "NSimSun": "新宋体",
    "SimHei": "黑体",
    "SimKai": "楷体",
    "KaiTi": "楷体",
    "FangSong": "仿宋",
    "Microsoft YaHei": "微软雅黑",
    "Microsoft YaHei UI": "微软雅黑 UI",
    "Microsoft YaHei Light": "微软雅黑 Light",
    "Microsoft JhengHei": "微软正黑体",
    "Microsoft JhengHei UI": "微软正黑体 UI",
    "DengXian": "等线",
    "Noto Sans SC": "思源黑体",
    "Noto Serif SC": "思源宋体",
    "FZShuTi": "方正舒体",
    "FZYaoTi": "方正姚体",
    "LiSu": "隶书",
    "YouYuan": "幼圆",
    "STXihei": "华文细黑",
    "STKaiti": "华文楷体",
    "STSong": "华文宋体",
    "STFangsong": "华文仿宋",
    "STZhongsong": "华文中宋",
    "STHupo": "华文琥珀",
    "STLiti": "华文隶书",
    "STXingkai": "华文行楷",
    "STXinwei": "华文新魏",
    "STCaiyun": "华文彩云",
}


def _face_names(path):
    """返回 [(family, style)]，用 FreeType 读；失败返回 []"""
    try:
        from PIL import ImageFont
    except ImportError:
        return []
    res = []
    for idx in range(16):
        try:
            f = ImageFont.truetype(path, 24, index=idx)
            fam, style = f.getname()
        except Exception:
            break
        if fam:
            res.append((fam.strip(), (style or "").strip()))
    return res


def _should_skip(fam, style):
    low = fam.lower()
    if fam in SKIP_FAMILIES:
        return True
    for p in SKIP_PREFIX:
        if low.startswith(p):
            return True
    if "italic" in style.lower() or "oblique" in style.lower():
        return True
    return False


def _build():
    reg_alias = _registry_aliases()
    fams = {}   # family -> {"regular": path, "bold": path, "aliases": set()}

    files = []
    for d in _font_dirs():
        for ext in ("*.ttf", "*.otf", "*.ttc"):
            files.extend(glob.glob(os.path.join(d, ext)))
    for path in files:
        base = os.path.basename(path).lower()
        regs = reg_alias.get(base, set())
        for fam, style in _face_names(path):
            if _should_skip(fam, style):
                continue
            rec = fams.setdefault(fam, {})
            sl = style.lower()
            if sl in REGULAR:
                rec.setdefault("regular", path)
            elif sl in BOLDISH:
                rec.setdefault("bold", path)
            for reg in regs:
                if reg != fam and any(ord(c) > 127 for c in reg):
                    rec.setdefault("aliases", set()).add(reg)

    final = {}
    for fam, rec in fams.items():
        p = rec.get("regular") or rec.get("bold")
        if not p:
            continue
        final[fam] = p
        if "bold" in rec and rec["bold"] != p:
            final[fam + " Bold"] = rec["bold"]
        for a in rec.get("aliases", ()):
            final.setdefault(a, p)
        # 手工补的中文名：同时登记「族名」和「族名 + 中文粗体」两种
        zh = CJK_ALIASES.get(fam)
        if zh:
            final.setdefault(zh, p)
    return final


def scan(force=False):
    cp = _cache_path()
    if cp and not force and os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                data = json.load(f)
            out = [{"family": k, "path": v} for k, v in data.items() if os.path.exists(v)]
            if len(out) > 5:
                return sorted(out, key=lambda x: x["family"].lower())
        except Exception:
            pass
    final = _build()
    if cp:
        try:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(final, f, ensure_ascii=False, indent=0)
        except Exception:
            pass
    out = [{"family": k, "path": v} for k, v in final.items() if os.path.exists(v)]
    return sorted(out, key=lambda x: x["family"].lower())


def pick(fonts, family):
    for f in fonts:
        if f["family"] == family:
            return f
    low = (family or "").lower()
    cand = [f for f in fonts if low and low in f["family"].lower()]
    if cand:
        return cand[0]
    return None


def find_path(fonts, family):
    f = pick(fonts, family)
    return f["path"] if f else None


if __name__ == "__main__":
    import sys
    res = scan(force="--force" in sys.argv)
    print(len(res), "fonts")
    for r in res:
        print("  %-34s %s" % (r["family"], os.path.basename(r["path"])))
