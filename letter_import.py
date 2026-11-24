# -*- coding: utf-8 -*-
"""Blender 侧：把一批文字资产导入目标 .blend，放在世界原点 (0,0,0)

由 letter_studio.py 调用，不要手动跑。

用法:
  blender.exe -b <target.blend> -P letter_import.py -- --job <job.json>
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


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True)
    return p.parse_args(argv)


def fail(msg):
    print("LETTER_STUDIO_ERROR: " + str(msg))
    sys.exit(3)


def frame_world_height():
    return 2 * CAM_DEPTH * (CAM_SENSOR / 2.0) / CAM_LENS


def get_collection(sc):
    name = "文字资产"
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        sc.collection.children.link(col)
    return col


def load_white_image(png):
    """把「黑字透明底」的 PNG 反相成「白字透明底」并 pack 进 blend"""
    import numpy as np
    src = bpy.data.images.load(png, check_existing=False)
    wpx, hpx = src.size
    buf = np.empty(wpx * hpx * 4, dtype=np.float32)
    src.pixels.foreach_get(buf)
    arr = buf.reshape(-1, 4)
    # 反相 RGB（黑->白），保留 alpha
    arr[:, 0:3] = 1.0 - arr[:, 0:3]
    out = bpy.data.images.new("TXT_" + os.path.splitext(os.path.basename(png))[0],
                              width=wpx, height=hpx, alpha=True)
    out.pixels.foreach_set(arr.reshape(-1))
    out.pack()
    bpy.data.images.remove(src)
    return out


def make_material(img):
    m = bpy.data.materials.new("M_" + os.path.splitext(os.path.basename(img.filepath_raw))[0])
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    transp = nt.nodes.new("ShaderNodeBsdfTransparent")
    emit = nt.nodes.new("ShaderNodeEmission")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.extension = 'CLIP'
    tex.interpolation = 'Linear'
    emit.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    emit.inputs["Strength"].default_value = 1.0
    nt.links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(emit.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    m.blend_method = 'BLEND'
    m.use_backface_culling = False
    if hasattr(m, "shadow_method"):
        m.shadow_method = 'NONE'
    return m


def import_one(sc, col, item, world_per_px, len_all=1):
    png = item["png"]
    if not os.path.exists(png):
        fail("PNG 不存在: " + png)
    gw = item.get("png_w")
    gh = item.get("png_h")
    design_px = float(item.get("design_px", 72))
    world_h = design_px * world_per_px

    # 原图是「黑字 + 透明底」，而 Allen Becker 风格的场景底色是深色 —— 直接贴上去等于看不见。
    # 所以贴图用反相后的「白字 + 透明底」：深底上清晰，浅底上再改材质颜色即可。
    img = load_white_image(png)
    if not gw or not gh:
        gw, gh = img.size
    if not gh:
        fail("PNG 尺寸读取失败: " + png)
    world_w = world_h * (float(gw) / float(gh))

    bpy.ops.mesh.primitive_plane_add(size=1.0, location=(0, 0, 0))
    plane = bpy.context.active_object
    plane.name = item.get("name", "letter")
    for c in plane.users_collection:
        c.objects.unlink(plane)
    col.objects.link(plane)

    plane.rotation_mode = 'XYZ'
    plane.rotation_euler = (math.radians(90), 0, 0)   # 正对 -Y 方向的相机
    plane.scale = (world_w, world_h, 1.0)

    plane.data.materials.append(make_material(img))

    # 每一行都精确落在世界原点 (0,0,0)，不做自动排布。
    # 原因：这套场景的相机画面很窄（原点附近可见范围只有几个世界单位），
    # 按字号自动排开很容易把后面几行推出画面，反而不如让用户在 Blender 里
    # 直接拖各自的 _CTRL 空物体来得直观。每个素材都有独立的父级空物体。

    ctrl = bpy.data.objects.new(plane.name + "_CTRL", None)
    ctrl.empty_display_type = 'CIRCLE'
    ctrl.empty_display_size = max(0.05, min(world_w, world_h) * 0.6)
    col.objects.link(ctrl)
    plane.parent = ctrl
    plane.matrix_parent_inverse.identity()
    plane.location = (0, 0, 0)

    # 每行都落在世界原点 (0,0,0)
    ctrl.location = (0.0, 0.0, 0.0)

    for o in (ctrl, plane):
        o["asset"] = "text_asset"
        o["design_px"] = design_px
        o["source_text"] = item.get("text", "")
        o["notes"] = "动 %s_CTRL 摆放；平面已绕X+90°正对相机" % plane.name
    return ctrl, plane, (world_w, world_h)


def main():
    args = parse_args()
    with open(args.job, "r", encoding="utf-8") as f:
        job = json.load(f)

    sc = bpy.context.scene
    col = get_collection(sc)
    world_per_px = frame_world_height() / 1080.0   # 按 1080p 设计

    created = []
    items = job["items"]
    for i, item in enumerate(items):
        item.setdefault("index", i)
        # 先删同名旧对象，保证幂等
        for suffix in ("", "_CTRL"):
            old = bpy.data.objects.get(item["name"] + suffix)
            if old is not None:
                bpy.data.objects.remove(old, do_unlink=True)
        ctrl, plane, (ww, wh) = import_one(sc, col, item, world_per_px,
                                           len_all=len(items))
        created.append(dict(
            name=item["name"], ctrl=ctrl.name, mesh=plane.name,
            world_w=round(ww, 6), world_h=round(wh, 6),
            px_h=round(wh / world_per_px, 2),
        ))
        print("LETTER_IMPORTED: %s  世界尺寸 %.4f x %.4f  ≈ %.1f px 高" % (
            plane.name, ww, wh, wh / world_per_px))

    bpy.ops.wm.save_as_mainfile(filepath=job["blend"])
    print("LETTER_STUDIO_IMPORT_OK: " + json.dumps(created, ensure_ascii=False))


if __name__ == "__main__":
    main()
