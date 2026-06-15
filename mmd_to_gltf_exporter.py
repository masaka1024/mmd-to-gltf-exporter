bl_info = {
    "name": "MMD to glTF Exporter",
    "author": "Custom Addon",
    "version": (2, 5, 5),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MMD Exporter",
    "description": "mmd_toolsで読み込んだMMDモデルをglTF(GLB)に変換してエクスポート",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import StringProperty
from bpy.types import Panel, Operator

_BLENDER_VERSION = bpy.app.version

# ============================================================
# 画像キャッシュ
# キー: 正規化済み絶対パス → bpy.types.Image
# None はキャッシュしない（後からロードされた場合に対応するため）
# ConvertMaterials.execute() の先頭でクリアする
# ============================================================

IMAGE_CACHE = {}


# ============================================================
# 内部ユーティリティ
# ============================================================

def _normalize_path(filepath):
    """
    Windows / Mac / Linux 共通のパス正規化。
    - バックスラッシュをスラッシュに統一
    - os.path.normpath でドット・二重スラッシュを解消
    - os.path.normcase で Windows のケース違いを吸収（Mac/Linux では no-op）
    """
    if not filepath:
        return ""
    return os.path.normcase(os.path.normpath(filepath.replace('\\', '/')))


def _set_blend_mode(mat, use_alpha):
    """Blenderバージョンに応じたアルファ設定"""
    try:
        if _BLENDER_VERSION < (4, 2, 0):
            mat.blend_method = 'HASHED' if use_alpha else 'OPAQUE'
            if hasattr(mat, 'shadow_method'):
                mat.shadow_method = 'HASHED' if use_alpha else 'OPAQUE'
        else:
            # Blender 4.2+ (EEVEE Next)
            if use_alpha:
                mat.surface_render_method = 'DITHERED'
    except Exception:
        pass


def _build_image_cache():
    """
    bpy.data.images を走査して IMAGE_CACHE を一括構築する。
    変換ループ前に1回だけ呼ぶことで、マテリアルごとの線形探索を排除する。
    キーは正規化済み絶対パス。None はキャッシュしない。
    basename → Image の逆引き辞書も返す（相対パス救済用）。
    """
    IMAGE_CACHE.clear()
    by_basename = {}
    for img in bpy.data.images:
        if img.source != 'FILE':
            continue
        norm = _normalize_path(bpy.path.abspath(img.filepath))
        if norm:
            IMAGE_CACHE[norm] = img
        bn = os.path.normcase(os.path.basename(img.filepath.replace('\\', '/')))
        if bn and bn not in by_basename:
            by_basename[bn] = img
    return by_basename


def _find_or_load_image(filepath, by_basename=None):
    """
    既存のImageデータを探す。なければファイルから読み込む。
    失敗した場合は None を返す（None はキャッシュしない）。

    マッチ優先順位:
      1. IMAGE_CACHE（正規化絶対パス）
      2. by_basename（ファイル名一致）
      3. ディスクから新規読み込み

    Windows対応:
      os.path.normcase() / os.path.normpath() でパスの差異を吸収
    """
    if not filepath:
        return None

    filepath_norm = filepath.replace('\\', '/')
    abs_path      = bpy.path.abspath(filepath_norm)
    abs_path_norm = _normalize_path(abs_path)
    basename      = os.path.basename(filepath_norm)
    basename_norm = os.path.normcase(basename)

    # 1. キャッシュヒット
    if abs_path_norm in IMAGE_CACHE:
        return IMAGE_CACHE[abs_path_norm]

    image = None

    # 2. basename 逆引き
    if image is None and by_basename and basename_norm:
        image = by_basename.get(basename_norm)
        if image:
            print(f"[MMD Exporter] 画像(basename一致): {image.name} ← {basename}")

    # 3. ディスクから読み込み
    if image is None and os.path.exists(abs_path):
        try:
            image = bpy.data.images.load(abs_path)
            print(f"[MMD Exporter] 画像(新規読み込み): {abs_path}")
        except Exception as e:
            print(f"[MMD Exporter] 画像読み込み失敗: {abs_path} - {e}")

    if image is None:
        print(f"[MMD Exporter] 画像が見つかりません: {filepath}")
        return None  # None はキャッシュしない

    # キャッシュに登録
    IMAGE_CACHE[abs_path_norm] = image
    return image


def _build_principled_material(mat, image, diffuse, alpha,
                                is_double_sided=False, sph_image=None):
    """
    マテリアルのノードツリーをリセットして
    Principled BSDF + Image Texture (またはソリッドカラー) を構築する。
    sph_image: 乗算スフィアマップ画像（あれば MixRGB Multiply で重ねる）
    """
    mat.use_nodes = True

    try:
        mat.use_backface_culling = not is_double_sided
    except Exception:
        pass

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out = nodes.new('ShaderNodeOutputMaterial')
    out.location = (500, 300)

    p = nodes.new('ShaderNodeBsdfPrincipled')
    p.location = (200, 300)
    p.inputs['Metallic'].default_value = 0.0
    p.inputs['Roughness'].default_value = 0.9
    links.new(p.outputs['BSDF'], out.inputs['Surface'])

    # ── ベーステクスチャ ──
    tex_node = None
    if image:
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = image
        tex_node.location = (-500, 350)

    # ── スフィアマップ（乗算）──
    if sph_image:
        sph_node = nodes.new('ShaderNodeTexImage')
        sph_node.image = sph_image
        sph_node.location = (-500, 50)

        tex_coord = nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (-750, 50)
        links.new(tex_coord.outputs['Normal'], sph_node.inputs['Vector'])

        mix = nodes.new('ShaderNodeMixRGB')
        mix.blend_type = 'MULTIPLY'
        mix.inputs['Fac'].default_value = 1.0
        mix.location = (-100, 250)

        if tex_node:
            links.new(tex_node.outputs['Color'], mix.inputs['Color1'])
        else:
            r, g, b = diffuse[0], diffuse[1], diffuse[2]
            mix.inputs['Color1'].default_value = (r, g, b, 1.0)

        links.new(sph_node.outputs['Color'], mix.inputs['Color2'])
        links.new(mix.outputs['Color'], p.inputs['Base Color'])

    elif tex_node:
        links.new(tex_node.outputs['Color'], p.inputs['Base Color'])

    else:
        r, g, b = diffuse[0], diffuse[1], diffuse[2]
        p.inputs['Base Color'].default_value = (r, g, b, 1.0)

    # ── アルファ ──
    has_alpha = False
    if tex_node and image and image.depth in (32, 128) and float(alpha) < 1.0:
        has_alpha = True
        links.new(tex_node.outputs['Alpha'], p.inputs['Alpha'])

    p.inputs['Alpha'].default_value = float(alpha)
    _set_blend_mode(mat, use_alpha=has_alpha or float(alpha) < 1.0)


# ============================================================
# エクスポート前処理ユーティリティ
# ============================================================

def _hide_mmd_internal_objects():
    """
    mmd_tools 内部用オブジェクト（.dummy_armature 等）をエクスポート前に非表示化する。
    use_visible=True と組み合わせることで GLB への混入を防ぐ。
    戻り値: 非表示化したオブジェクトのリスト（復元用）
    """
    hidden = []
    for obj in bpy.data.objects:
        if obj.name.startswith('.dummy_armature') or 'mmd_bind' in obj.name:
            if not obj.hide_viewport:
                obj.hide_viewport = True
                hidden.append(obj)
                print(f"[MMD Exporter] 内部オブジェクトを一時非表示: {obj.name}")
    return hidden


def _mute_sdef_shape_keys():
    """
    mmd_sdef_c / mmd_sdef_r0 / mmd_sdef_r1 シェイプキーをミュートする。
    これらは SDEF 変形用の内部係数であり、モーフターゲットとして
    エクスポートされると見た目が崩れる原因になる。
    戻り値: ミュートしたシェイプキーのリスト（復元用）
    """
    muted = []
    for mesh in bpy.data.meshes:
        if not mesh.shape_keys:
            continue
        for kb in mesh.shape_keys.key_blocks:
            if kb.name.startswith('mmd_sdef_') and not kb.mute:
                kb.mute = True
                muted.append(kb)
                print(f"[MMD Exporter] SDEFシェイプキーを一時ミュート: {kb.name} ({mesh.name})")
    return muted


# ============================================================
# Step 1: マテリアル変換
# ============================================================

class MMD_OT_ConvertMaterials(Operator):
    bl_idname = "mmd.convert_materials"
    bl_label = "マテリアルを変換"
    bl_description = (
        "mmd_toolsのマテリアルをPrincipled BSDFに変換します。\n"
        "テクスチャ・色情報は mmd_material プロパティから直接読み取ります。"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # 画像インデックスを一度だけ構築（変換ループ中の線形探索を排除）
        by_basename = _build_image_cache()

        converted = 0
        skipped = 0

        for mat in bpy.data.materials:
            if self._convert(mat, by_basename):
                converted += 1
            else:
                skipped += 1

        self.report({'INFO'}, f"変換完了: {converted}個変換、{skipped}個スキップ")
        return {'FINISHED'}

    def _convert(self, mat, by_basename):
        """
        1つのマテリアルを変換する。
        成功: True / スキップ: False
        """
        try:
            # エッジ用マテリアルは変換しない（真っ黒になる原因）
            if mat.name.startswith('mmd_edge.'):
                return False

            mmd_mat = getattr(mat, 'mmd_material', None)

            # mmd_shader が存在しないマテリアルはMMD以外 or 変換済みとしてスキップ
            has_mmd_shader = (
                mat.use_nodes
                and mat.node_tree is not None
                and mat.node_tree.nodes.get('mmd_shader') is not None
            )
            if not has_mmd_shader:
                return False

            # ── ベーステクスチャを取得 ──
            image = None
            if mmd_mat and getattr(mmd_mat, 'texture', ''):
                image = _find_or_load_image(mmd_mat.texture, by_basename)

            if image is None and mat.node_tree:
                tex_node = mat.node_tree.nodes.get('mmd_base_tex')
                if tex_node and tex_node.type == 'TEX_IMAGE' and tex_node.image:
                    image = tex_node.image
                    print(f"[MMD Exporter] {mat.name}: mmd_base_texノードから取得: {image.name}")

            # ── スフィアマップ（乗算）を取得 ──
            # mmd_toolsのsphere_texture_typeはバージョンにより値が異なる:
            #   '1' または 'MULT' または 'Multiply' = 乗算
            sph_image = None
            if mmd_mat:
                sph_type = getattr(mmd_mat, 'sphere_texture_type', '0')
                print(f"[MMD Exporter] {mat.name}: sphere_type_raw={repr(sph_type)}")
                if sph_type in ('1', 'MULT', 'Multiply', 'multiply'):
                    sph_path = getattr(mmd_mat, 'sphere_texture', '')
                    if sph_path:
                        sph_image = _find_or_load_image(sph_path, by_basename)
                        if sph_image:
                            print(f"[MMD Exporter] {mat.name}: スフィアマップ(MULT)={sph_image.name}")
                        else:
                            print(f"[MMD Exporter] {mat.name}: スフィアマップ読み込み失敗: {sph_path}")

            # ── 色・アルファ・両面フラグを取得 ──
            diffuse = tuple(mat.diffuse_color[:3])
            if mmd_mat:
                alpha           = float(getattr(mmd_mat, 'alpha', 1.0))
                is_double_sided = bool(getattr(mmd_mat, 'is_double_sided', False))
            else:
                alpha           = mat.diffuse_color[3] if len(mat.diffuse_color) > 3 else 1.0
                is_double_sided = not getattr(mat, 'use_backface_culling', True)

            tex_name = image.name if image else "テクスチャなし"
            sph_name = sph_image.name if sph_image else "なし"
            print(f"[MMD Exporter] {mat.name}: tex={tex_name}, sph={sph_name}, "
                  f"color=({diffuse[0]:.2f},{diffuse[1]:.2f},{diffuse[2]:.2f}), "
                  f"alpha={alpha:.2f}, double_sided={is_double_sided}")

            _build_principled_material(mat, image, diffuse, alpha, is_double_sided, sph_image)
            return True

        except Exception as e:
            self.report({'WARNING'}, f"'{mat.name}' スキップ: {e}")
            return False


# ============================================================
# Step 2: ボーン名の英語変換
# ============================================================

class MMD_OT_RenameBones(Operator):
    bl_idname = "mmd.rename_bones"
    bl_label = "ボーン名を英語に変換"
    bl_description = "mmd_toolsの英語名プロパティ、またはビルトイン変換テーブルを使ってボーン名を英語化します"
    bl_options = {'REGISTER', 'UNDO'}

    JP_TO_EN = {
        "センター": "Center",    "グルーブ": "Groove",   "腰": "Waist",
        "上半身": "UpperBody",   "上半身2": "UpperBody2","下半身": "LowerBody",
        "首": "Neck",            "頭": "Head",
        "左肩": "ShoulderL",    "右肩": "ShoulderR",
        "左腕": "ArmL",         "右腕": "ArmR",
        "左ひじ": "ElbowL",     "右ひじ": "ElbowR",
        "左手首": "WristL",     "右手首": "WristR",
        "左足": "LegL",         "右足": "LegR",
        "左ひざ": "KneeL",      "右ひざ": "KneeR",
        "左足首": "AnkleL",     "右足首": "AnkleR",
        "左つま先": "ToeL",     "右つま先": "ToeR",
        "両目": "Eyes",         "左目": "EyeL",         "右目": "EyeR",
    }

    def execute(self, context):
        renamed = 0
        failed = 0

        for obj in bpy.data.objects:
            if obj.type != 'ARMATURE':
                continue
            for bone in obj.data.bones:
                try:
                    mmd_bone = getattr(bone, 'mmd_bone', None)
                    if mmd_bone:
                        en_name = getattr(mmd_bone, 'name_e', '').strip()
                        if en_name:
                            bone.name = en_name
                            renamed += 1
                            continue
                except Exception:
                    pass

                if bone.name in self.JP_TO_EN:
                    bone.name = self.JP_TO_EN[bone.name]
                    renamed += 1
                else:
                    failed += 1

        self.report({'INFO'}, f"リネーム完了: {renamed}個成功、{failed}個は変換テーブルなし")
        return {'FINISHED'}


# ============================================================
# Step 3: GLBエクスポート
# ============================================================

class MMD_OT_ExportGLTF(Operator):
    bl_idname = "mmd.export_gltf"
    bl_label = "GLBとしてエクスポート"
    bl_description = "Unity / Unreal Engine向けにGLBファイルを書き出します（非表示オブジェクト除外）"

    filepath: StringProperty(subtype='FILE_PATH', default="//exported_model.glb")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath)
        if not filepath.endswith('.glb'):
            filepath += '.glb'

        # ── 使用中の画像のみ GLB に埋め込む ──
        # 全画像ではなく、マテリアルのノードで実際に参照している画像のみ対象にする。
        # Windows では os.path.exists() + pack() が Defender の影響で遅いため
        # 呼び出し回数を最小化する。
        used_images = set()
        for mat in bpy.data.materials:
            if not mat.use_nodes or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    used_images.add(node.image)

        packed, missing = 0, []
        for img in used_images:
            if img.source != 'FILE' or img.packed_file:
                continue
            abs_path = bpy.path.abspath(img.filepath)
            if os.path.exists(abs_path):
                try:
                    img.pack()
                    packed += 1
                except RuntimeError as e:
                    print(f"[MMD Exporter] パック失敗（ロック？）: {img.name} - {e}")
                    missing.append(img.name)
                except Exception as e:
                    print(f"[MMD Exporter] パック失敗（その他）: {img.name} - {e}")
                    missing.append(img.name)
            else:
                missing.append(img.name)

        if packed:
            self.report({'INFO'}, f"{packed}個の画像をGLBに埋め込みました")
        if missing:
            self.report({'WARNING'}, f"ファイルが見つからない/ロックされている画像: {', '.join(missing[:5])}")

        # ── エクスポート前: mmd内部データを一時退避 ──
        hidden_objs = _hide_mmd_internal_objects()
        muted_keys  = _mute_sdef_shape_keys()

        try:
            result = self._do_export(filepath)
        finally:
            # エラー時も必ず元に戻す
            for obj in hidden_objs:
                obj.hide_viewport = False
            for kb in muted_keys:
                kb.mute = False

        return result

    def _do_export(self, filepath):
        # ── RNAプロパティを確認してバージョン対応のパラメータだけ渡す ──
        try:
            rna_ids = {p.identifier for p in bpy.ops.export_scene.gltf.get_rna_type().properties}
        except Exception:
            rna_ids = set()

        desired = {
            'export_format':       'GLB',
            'export_materials':    'EXPORT',
            'export_texcoords':    True,
            'export_normals':      True,
            'export_animations':   True,
            'export_morph':        True,
            'export_morph_normal': True,
            'export_skins':        True,
            'export_yup':          True,
            'export_apply':        True,
            'export_colors':       True,
            'export_nla_strips':   True,
            'use_visible':         True,
        }

        kwargs = {'filepath': filepath}
        for k, v in desired.items():
            if not rna_ids or k in rna_ids:
                kwargs[k] = v

        try:
            bpy.ops.export_scene.gltf(**kwargs)
            self.report({'INFO'}, f"エクスポート完了: {filepath}")
            return {'FINISHED'}
        except TypeError as e:
            self.report({'WARNING'}, f"最小パラメータで再試行: {e}")
            try:
                bpy.ops.export_scene.gltf(filepath=filepath, export_format='GLB')
                self.report({'INFO'}, f"エクスポート完了（デフォルト設定）: {filepath}")
                return {'FINISHED'}
            except Exception as e2:
                self.report({'ERROR'}, f"エクスポート失敗: {e2}")
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"エクスポート失敗: {e}")
            return {'CANCELLED'}


# ============================================================
# サイドバーパネル
# ============================================================

class MMD_PT_ExporterPanel(Panel):
    bl_label = "MMD → glTF Exporter"
    bl_idname = "MMD_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MMD Exporter"

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Step 1: マテリアル変換", icon='MATERIAL')
        box.label(text="MMDシェーダー → Principled BSDF", icon='INFO')
        box.operator("mmd.convert_materials", icon='NODE_MATERIAL')

        layout.separator()

        box = layout.box()
        box.label(text="Step 2: ボーン名を英語化", icon='ARMATURE_DATA')
        box.label(text="日本語ボーン名を英語に変換", icon='INFO')
        box.operator("mmd.rename_bones", icon='BONE_DATA')

        layout.separator()

        box = layout.box()
        box.label(text="Step 3: GLBエクスポート", icon='EXPORT')
        box.label(text="Unity / Unreal Engine向け・非表示除外", icon='INFO')
        box.operator("mmd.export_gltf", icon='FILE_TICK')


# ============================================================
# 登録・解除
# ============================================================

classes = [
    MMD_OT_ConvertMaterials,
    MMD_OT_RenameBones,
    MMD_OT_ExportGLTF,
    MMD_PT_ExporterPanel,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    print("MMD to glTF Exporter v2.5.5: 有効化されました")

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    print("MMD to glTF Exporter v2.5.5: 無効化されました")

if __name__ == "__main__":
    register()
