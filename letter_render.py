# -*- coding: utf-8 -*-
"""
Blender 侧渲染脚本：生成文字资产的 PNG（透明底）+ meta.json

由 LetterStudio.py 以 --background 方式调用，不要手动跑。

用法:
  blender.exe -b -P letter_render.py -- --spec <spec.json>

spec.json 关键字段:
  mode      : "text" | "svg"
  text      : 文字内容（mode=text）
  svg_path  : Manim 导出的 SVG 路径（mode=svg）
  font_path : 字体文件绝对路径（mode=text，可为空）
  design_px : 设计字号（例如 72）＝该文字在 1920x1080 画面里占多少像素高
  out_px    : 输出 PNG 边长（= design_px * 超采样倍数）
  out_png   : 输出 PNG 绝对路径
  meta_json : 输出 meta 绝对路径
"""
import bpy
import sys
import os
import json
import argparse
import math

CAM_LENS = 50.0
CAM_SENSOR = 36.0
CAM_DEPTH = 10.0

# 由 main() 从 spec 里灌进来：{"arial.ttf|中": True/False, ...}
COVERAGE = {}


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--spec", required=True)
    return p.parse_args(argv)


def fail(msg):
    print("LETTER_STUDIO_ERROR: " + str(msg))
    sys.exit(3)


def frame_world_height():
    """深 CAM_DEPTH 处，画面在世界空间里的高度（对应 1080 像素）"""
    return 2 * CAM_DEPTH * (CAM_SENSOR / 2.0) / CAM_LENS


def obj_bounds(ob):
    """世界空间包围盒 (w, h, cx, cy)

    不用 ob.to_mesh()/evaluated matrix（本环境下它们会不刷新），
    直接手算每个点的世界坐标。
    """
    mw = ob.matrix_world
    pts = []
    if ob.type == 'MESH':
        pts = [mw @ v.co.copy() for v in ob.data.vertices]
    elif ob.type in ('CURVE', 'FONT'):
        # FONT 的轮廓是按需生成的，得先求值一次 to_mesh 才会建出来；
        # 但 to_mesh 不应用对象 scale，所以拿到几何后仍用 mw 自己算世界坐标
        dg = bpy.context.evaluated_depsgraph_get()
        ob_eval = ob.evaluated_get(dg)
        try:
            me_tmp = ob_eval.to_mesh()
            if me_tmp is not None and len(me_tmp.vertices):
                pts = [mw @ v.co.copy() for v in me_tmp.vertices]
            ob_eval.to_mesh_clear()
        except Exception:
            pts = []
        for sp in ob.data.splines:
            if sp.type == 'BEZIER':
                for bp in sp.bezier_points:
                    pts.append(mw @ bp.co.copy())
                    pts.append(mw @ bp.handle_left.copy())
                    pts.append(mw @ bp.handle_right.copy())
            else:
                for p in sp.points:
                    pts.append(mw @ p.co.copy())
    if not pts:
        fail("文字对象没有可测量的顶点")
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (max(xs) - min(xs), max(ys) - min(ys),
            (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)


def local_bounds_from_world(ob, w, h, cx, cy):
    """把世界空间的中心换算成对象本地坐标的偏移"""
    sx = ob.scale.x or 1.0
    sy = ob.scale.y or 1.0
    return cx / sx, cy / sy


CJK_FALLBACK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\NotoSansSC-Regular.otf",
]


def glyph_coverage_cached(text, font_path):
    """{(font_path, ch): 是否有字形} —— 由 LetterStudio.py 预先算好传进来。

    原因：Blender 自带的 Python 里没有 PIL/FreeType，而用 Blender 的 FONT 对象
    又判断不出缺字（.notdef 和真字形都是 8 个点的方框）。母进程有 PIL，顺手算完更省事。
    """
    return COVERAGE


def _probe_w(font, ch):
    """有字形 → 返回 1.0；缺字形 → 0.0。数据来自 spec 里的 coverage 表。"""
    path = getattr(font, "filepath", "") or ""
    key = "%s|%s" % (os.path.basename(path).lower(), ch)
    if key in COVERAGE:
        return 1.0 if COVERAGE[key] else 0.0
    # 没预计算就保守认为有（宁可画出方框，也不要悄悄丢掉字）
    return 1.0


def split_by_coverage(text, main_font):
    """把文字按「主字体有没有这个字形」切成若干段：[(片段, 是否用主字体)]"""
    segs = []
    fallback = None
    for ch in text:
        ok = ch.isspace() or _probe_w(main_font, ch) > 0.5
        if segs and segs[-1][1] == ok:
            segs[-1][0] += ch
        else:
            segs.append([ch, ok])
    return [(s, ok) for s, ok in segs]


def pick_cjk_fallback(text, main_font):
    """挑一个能覆盖 text 里所有缺字字符的后备字体；挑不到返回 None"""
    cjk = [c for c in text if not c.isspace() and _probe_w(main_font, c) <= 0.5]
    if not cjk:
        return None
    for p in CJK_FALLBACK_FONTS:
        if not os.path.exists(p):
            continue
        try:
            f = bpy.data.fonts.load(p, check_existing=True)
        except Exception:
            continue
        if all(_probe_w(f, c) > 0.5 for c in cjk):
            return f
    return None


def missing_chars(text, main_font):
    out = []
    for ch in text:
        if ch.isspace():
            continue
        if ch not in out and _probe_w(main_font, ch) <= 0.5:
            out.append(ch)
    return out


def seg_advance(ob, seg, font):
    """量一段文字的「排版前进宽度」——也就是下一个字应该从哪开始。

    Blender 的 FONT 对象没有直接暴露 advance 接口，这里用一个小技巧：
    在同一字体下量 "本段" 与 "本段 + 一个参考字符" 的墨迹宽度差，
    差值就是本段的步进宽度（不含最后那个参考字符本身）。
    """
    try:
        ref = "H"
        whole = ob.data.body
        ob.data.body = seg + ref
        bpy.context.view_layer.update()
        w2, h2, cx2, cy2 = obj_bounds_local(ob)
        ob.data.body = whole
        bpy.context.view_layer.update()
        w1, h1, cx1, cy1 = obj_bounds_local(ob)
        return max(0.0, w2 - w1 + 0.0)
    except Exception:
        return 0.0


def obj_bounds_local(ob):
    """量 MESH/CURVE/FONT 在当前变换下的外接框（不返回世界坐标，同 obj_bounds）"""
    pts = []
    mw = ob.matrix_world
    for v in ob.data.vertices:
        pts.append(mw @ v.co.copy())
    if not pts:
        return 0.0, 0.0, 0.0, 0.0
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (max(xs) - min(xs), max(ys) - min(ys),
            (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)


def _append_mesh(dst, src):
    """把 src 网格的顶点/边/面追加到 dst（就地改，不用 join 操作符）。

    用 bpy.ops.object.join() 会依赖选中状态、活动对象和光标位置，
    在后台模式下容易把别的物体一起合进来，这里直接拼数据更稳。
    """
    nv = len(dst.vertices)
    ne = len(dst.edges)
    nm = len(dst.loops)
    np = len(dst.polygons)

    vco = [v.co.copy() for v in dst.vertices] + [v.co.copy() for v in src.vertices]
    edges = [tuple(e.vertices) for e in dst.edges] + \
            [(e.vertices[0] + nv, e.vertices[1] + nv) for e in src.edges]
    polys = [tuple(p.vertices) for p in dst.polygons] + \
            [tuple(i + nv for i in p.vertices) for p in src.polygons]

    dst.clear_geometry()
    dst.from_pydata(vco, edges, polys)
    dst.update()
    dst.validate()


def build_text_objects(spec):
    """按字形覆盖把一行字拆成多个 FONT 对象，逐一转换后 join 成一个。

    Blender 原生的 FONT 对象不会为非 ASCII 缺字做字体回退（缺了就是空白），
    所以要手工挑一个含中文字形的后备字体（微软雅黑 / 黑体 / 宋体）。
    """
    import math as _m
    from mathutils import Matrix

    text = spec.get("text", "")
    if not text:
        fail("文字内容为空")
    warn = ""
    font_path = spec.get("font_path") or ""
    main = None
    if font_path and os.path.exists(font_path):
        try:
            main = bpy.data.fonts.load(font_path, check_existing=False)
        except Exception as e:
            warn = "字体加载失败(%s)，回退到内置字体" % e
    else:
        warn = "未找到字体文件，回退到内置字体"

    segs = split_by_coverage(text, main)
    fallback = pick_cjk_fallback(text, main) if main is not None else None
    miss = ""
    parts = []
    pen = 0.0
    for seg, ok in segs:
        if not ok:
            for ch in seg:
                if not ch.isspace() and ch not in miss:
                    miss += ch

    # 关键：本环境里 Blender 3.6 的 FONT 对象有「场景里只有一个时轮廓不生成」的毛病，
    # 建两个以上的文字对象反而只求值出最后一个。所以用单对象按「字符」逐个排版：
    # 每个字符单独转 MESH、量出墨迹，再按字体自己的步进宽度推进笔位，
    # 最后把所有字形拼进同一个网格。空格没有轮廓，必须靠步进宽度推进，
    # 否则中英文之间会挤在一起（这就是 "中文 ABC" 变成 "中A文BC" 的原因）。
    def _curve_obj(body, font):
        cu = bpy.data.curves.new("LSText", type='FONT')
        cu.body = body
        cu.align_x = 'LEFT'
        cu.align_y = 'BOTTOM_BASELINE'
        cu.resolution_u = 24
        if font is not None:
            cu.font = font
        ob = bpy.data.objects.new("LSText", cu)
        bpy.context.scene.collection.objects.link(ob)
        bpy.context.view_layer.update()
        return ob

    def _to_mesh(ob):
        bpy.ops.object.select_all(action='DESELECT')
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        bpy.ops.object.convert(target='MESH')
        return bpy.context.view_layer.objects.active

    def _ink(me):
        if me is None or not len(me.vertices):
            return None
        xs = [v.co.x for v in me.vertices]
        ys = [v.co.y for v in me.vertices]
        return (min(xs), max(xs), min(ys), max(ys))

    REF = "H"     # 参考字符：用来测「一个字符占用多宽」

    def _char_advance(ch, font):
        """一个字符在给定字体里占多宽（= 下一个字从哪里开始）。

        做法：同一个参考字符 "H"，分别量 "H" 和 "ch+H" 的墨迹右边缘，差值就是
        ch 的步进宽度。注意必须把被测字符放在 **前面**：Blender 会吞掉行尾空格，
        "H " 和 "H" 量出来一模一样，只有 " H" 才会把 H 往右推。
        """
        base = _curve_obj(REF, font)
        base = _to_mesh(base)
        lead = _curve_obj(ch + REF, font)
        lead = _to_mesh(lead)
        try:
            ib, il = _ink(base.data), _ink(lead.data)
            if ib and il:
                return max(0.0, il[1] - ib[1])
        finally:
            for o in (base, lead):
                if o.name in bpy.data.objects:
                    bpy.data.objects.remove(o, do_unlink=True)
        return 0.0

    base_ob = None
    base_me = None
    for seg, ok in segs:
        font = main if ok else fallback
        for ch in seg:
            if ch.isspace():
                # 空格：没有轮廓，只能推进笔位
                pen += _char_advance(ch, font) if font is not None else 0.0
                continue
            tmp = _curve_obj(ch, font)
            tmp = _to_mesh(tmp)
            box = _ink(tmp.data)
            if box is None:
                continue
            # 这个字形的左边缘对齐当前笔位
            dx = pen - box[0]
            for v in tmp.data.vertices:
                v.co.x += dx
            tmp.data.update()
            if base_me is None:
                base_me = tmp.data
                base_ob = tmp
            else:
                _append_mesh(base_me, tmp.data)
                bpy.data.objects.remove(tmp, do_unlink=True)
            pen += _char_advance(ch, font)
            bpy.context.view_layer.update()

    if base_me is None:
        fail("这些字符在所有候选字体里都找不到字形")
    ob = base_ob
    if miss and fallback is not None:
        msg = "「%s」这些字形当前字体没有，已自动改用微软雅黑/黑体后备字体" % miss
    elif miss:
        msg = "当前字体缺这些字形：%s（系统里也没找到中文字体，会显示成方框）" % miss
    else:
        msg = warn
    return ob, msg, list(miss)


def build_svg_object(spec):
    path = spec["svg_path"]
    if not os.path.exists(path):
        fail("SVG 不存在: " + path)
    before = set(bpy.context.scene.objects)
    try:
        bpy.ops.import_curve.svg(filepath=path)
    except Exception as e:
        fail("SVG 导入失败: %s" % e)
    curves = [o for o in bpy.context.scene.objects
              if o not in before and o.type in ("CURVE", "FONT")]
    if not curves:
        curves = [o for o in bpy.context.scene.objects if o.type in ("CURVE", "FONT")]
    if not curves:
        fail("SVG 里没有可用的曲线对象")
    bpy.ops.object.select_all(action='DESELECT')
    for o in curves:
        o.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]
    if len(curves) > 1:
        try:
            bpy.ops.object.join()
        except Exception:
            pass
    ob = bpy.context.view_layer.objects.active
    ob.name = "LSText"
    return ob, ""


def build_svg_text_via_svg(spec):
    """把文字转成 SVG 再导进来。

    Blender 原生的 FONT 对象不会为缺失字形做字体回退（缺了就是空白），
    而导入的 SVG 走的是系统 SVG 解析，会按 font-family 列表挑能显示的字形。
    """
    import html
    text = spec.get("text", "")
    font_path = spec.get("font_path") or ""
    fam = ""
    if font_path:
        base = os.path.splitext(os.path.basename(font_path))[0].lower()
        fam = {"arial": "Arial", "msyh": "Microsoft YaHei", "simhei": "SimHei",
               "simsun": "SimSun", "noto": "Noto Sans SC"}.get(base, "")
    fams = ", ".join([f for f in [fam, "Microsoft YaHei", "SimHei", "SimSun",
                                  "Noto Sans SC", "Arial"] if f])
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="200">'
           '<text x="10" y="150" font-family="%s" font-size="120" fill="#000">%s</text>'
           '</svg>') % (fams, html.escape(text))
    path = os.path.join(os.path.dirname(spec["out_png"]), os.path.basename(spec["out_png"]) + ".txt.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    before = set(bpy.context.scene.objects)
    bpy.ops.import_curve.svg(filepath=path)
    curv = [o for o in bpy.context.scene.objects
            if o not in before and o.type in ("CURVE", "FONT")]
    if not curv:
        return None, "SVG 路径也没有生成字形"
    bpy.ops.object.select_all(action='DESELECT')
    for o in curv:
        o.select_set(True)
    bpy.context.view_layer.objects.active = curv[0]
    if len(curv) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = "LSText"
    return ob, ""


def fit_to_design_px(ob, spec):
    """缩放到「设计字号」对应的世界高，并把外接框中心放到对象原点"""
    design_px = float(spec.get("design_px", 72))
    out_px = int(spec.get("out_px", 216))
    world_per_px = frame_world_height() / 1080.0
    target_h = design_px * world_per_px

    # 1) 缩放：改对象 scale，量世界高度，迭代到目标值
    for i in range(6):
        w, h, cx, cy = obj_bounds(ob)
        if h <= 1e-9:
            fail("文字高度为 0（字体缺少这些字形？）")
        k = target_h / h
        if abs(k - 1.0) < 5e-4:
            break
        ob.scale = tuple(s * k for s in ob.scale)
        bpy.context.view_layer.update()

    # 2) 居中：把世界中心换算成本地偏移，直接挪几何
    w, h, cx, cy = obj_bounds(ob)
    lx, ly = cx / (ob.scale.x or 1.0), cy / (ob.scale.y or 1.0)
    if ob.type == 'FONT':
        ob.data.offset_x -= lx
        ob.data.offset_y -= ly
        # align_* 是「对齐原点」，和 offset 会互相打架：实测设成 CENTER 后
        # 宽文字会整体偏到一边、甚至把一半挤出画面（中文首字被裁掉）。
        # 既然已经用 offset 把外接框中心搬到原点了，就不要再让 Blender 自己对齐。
        ob.data.align_x = 'LEFT'
        ob.data.align_y = 'BOTTOM_BASELINE'
    elif ob.type == 'CURVE':
        for sp in ob.data.splines:
            for bp in sp.bezier_points:
                bp.co.x -= lx
                bp.co.y -= ly
                bp.handle_left.x -= lx
                bp.handle_left.y -= ly
                bp.handle_right.x -= lx
                bp.handle_right.y -= ly
            for p in sp.points:
                p.co.x -= lx
                p.co.y -= ly
    else:
        for v in ob.data.vertices:
            v.co.x -= lx
            v.co.y -= ly
    bpy.context.view_layer.update()

    # 3) 立起来正对相机：文字躺在 XY 平面朝 +Z，相机在 -Y → 绕 X +90°
    ob.rotation_euler = (math.radians(90), 0.0, 0.0)
    ob.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()

    # 4) 量旋转后的真实世界尺寸（此时高度落在 Z 轴、宽度落在 X 轴）
    wz, hz = mesh_world_size(ob)
    if hz <= 1e-9 and h > 1e-9:
        hz = h          # 量不到就退回旋转前的本地高度，别把结果写成 0
    return dict(
        world_w=round(float(wz), 6),
        world_h=round(float(hz), 6),
        design_px=design_px,
        world_per_px=round(world_per_px, 8),
        cam_depth=CAM_DEPTH,
        out_px=out_px,
    )


def mesh_world_size(ob):
    """旋转摆正之后的 (世界宽, 世界高)"""
    mw = ob.matrix_world
    xs, zs = [], []
    if ob.type == 'MESH':
        pts = [mw @ v.co for v in ob.data.vertices]
    else:
        pts = []
        for sp in ob.data.splines:
            if sp.type == 'BEZIER':
                for bp in sp.bezier_points:
                    pts.append(mw @ bp.co)
            else:
                for pp in sp.points:
                    pts.append(mw @ pp.co)
    for p in pts:
        xs.append(p.x)
        zs.append(p.z)
    if not xs:
        return 0.0, 0.0
    return (max(xs) - min(xs)), (max(zs) - min(zs))


def make_black_material():
    m = bpy.data.materials.new("LS_BLACK")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    return m


def setup_scene(out_px, keep_objects=()):
    """清理启动场景里的物体（但不能连带删掉刚建好的文字），再装相机/世界"""
    sc = bpy.context.scene
    keep = {o.name for o in keep_objects if o is not None}
    for ob in list(sc.objects):
        if ob.name in keep:
            continue
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        bpy.data.objects.remove(ob, do_unlink=True)

    sc.render.engine = 'BLENDER_EEVEE'
    sc.render.resolution_x = out_px
    sc.render.resolution_y = out_px
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    sc.render.image_settings.color_depth = '8'
    sc.view_settings.view_transform = 'Standard'
    sc.view_settings.look = 'None'
    try:
        sc.eevee.taa_render_samples = 16
    except Exception:
        pass

    w = sc.world or bpy.data.worlds.new("W")
    sc.world = w
    w.use_nodes = True
    for n in w.node_tree.nodes:
        if n.type == 'BACKGROUND':
            n.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
            n.inputs[1].default_value = 0.0

    cam_data = bpy.data.cameras.new("LS_CAM")
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = frame_world_height()
    cam_data.clip_start = 0.01
    cam_data.clip_end = 100.0
    cam = bpy.data.objects.new("LS_CAM", cam_data)
    sc.collection.objects.link(cam)
    cam.location = (0.0, -CAM_DEPTH, 0.0)
    cam.rotation_euler = (math.radians(90), 0.0, 0.0)
    sc.camera = cam
    return cam


def crop_alpha(png, meta):
    """按 alpha 裁掉空白，返回实际字形像素尺寸"""
    try:
        from PIL import Image
        im = Image.open(png)
        bbox = im.getchannel("A").getbbox()
        if bbox is None:
            meta["glyph_w"], meta["glyph_h"] = 0, 0
            meta["alpha_bbox"] = None
            return
        meta["glyph_w"] = bbox[2] - bbox[0]
        meta["glyph_h"] = bbox[3] - bbox[1]
        meta["alpha_bbox"] = list(bbox)
    except Exception:
        # 没有 PIL 时退回 Blender 自己的像素缓冲
        try:
            import numpy as np
            img = bpy.data.images.load(png)
            wpx, hpx = img.size
            buf = np.empty(wpx * hpx * 4, dtype=np.float32)
            img.pixels.foreach_get(buf)
            a = buf.reshape(hpx, wpx, 4)[:, :, 3]
            ys, xs = np.nonzero(a > 0.02)
            if len(xs) == 0:
                meta["glyph_w"], meta["glyph_h"] = 0, 0
                return
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            meta["glyph_w"] = x1 - x0 + 1
            meta["glyph_h"] = y1 - y0 + 1
            meta["alpha_bbox"] = [x0, y0, x1, y1]
        except Exception as e:
            meta["glyph_w"] = meta.get("out_px")
            meta["glyph_h"] = meta.get("out_px")
            meta["crop_warn"] = str(e)


def main():
    global COVERAGE
    args = parse_args()
    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)
    COVERAGE = spec.get("coverage") or {}

    # 注意：不能 read_homefile(use_empty=True)，本环境后台 EEVEE 渲不出空场景
    if spec.get("mode") == "svg":
        ob, warn = build_svg_object(spec)
    else:
        ob, warn, _miss = build_text_objects(spec)
        ob.data.materials.clear()   # join 之后可能带进多余材质，统一换黑材质

    info = fit_to_design_px(ob, spec)

    bpy.ops.object.select_all(action='DESELECT')
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    if ob.type != 'MESH':
        try:
            bpy.ops.object.convert(target='MESH')
            ob = bpy.context.view_layer.objects.active
        except Exception as e:
            fail("转网格失败: %s" % e)
    ob.data.materials.clear()
    ob.data.materials.append(make_black_material())

    out_px = int(spec.get("out_px", 216))
    out_png = spec["out_png"]
    setup_scene(out_px, keep_objects=(ob,))
    bpy.context.scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)

    if not os.path.exists(out_png):
        fail("渲染没有产出文件: " + out_png)

    crop_alpha(out_png, info)
    info["png"] = out_png
    info["warn"] = warn
    info["text"] = spec.get("text", "")
    info["mode"] = spec.get("mode", "text")
    meta_path = spec.get("meta_json")
    if meta_path:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    print("LETTER_STUDIO_OK: " + json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
