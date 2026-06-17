bl_info = {
    "name": "MMD to glTF Exporter",
    "author": "Custom Addon / revised by M365 Copilot",
    "version": (2, 3, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MMD Exporter",
    "description": "mmd_toolsで読み込んだMMDモデルをglTF/GLBに変換してエクスポートします",
    "category": "Import-Export",
}

import os
import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import Operator, Panel
from bpy_extras.io_utils import ExportHelper

_BLENDER_VERSION = bpy.app.version

IMAGE_CACHE = {}


# ============================================================
# 共通ユーティリティ
# ============================================================

def _normalize_path(filepath):
    if not filepath:
        return ""
    return os.path.normcase(os.path.normpath(filepath.replace("\\", "/")))


def _safe_abspath(filepath):
    if not filepath:
        return ""
    try:
        return bpy.path.abspath(filepath)
    except Exception:
        return filepath


def _set_blend_mode(mat, use_alpha):
    try:
        if _BLENDER_VERSION < (4, 2, 0):
            mat.blend_method = "HASHED" if use_alpha else "OPAQUE"
            if hasattr(mat, "shadow_method"):
                mat.shadow_method = "HASHED" if use_alpha else "OPAQUE"
        else:
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "DITHERED" if use_alpha else "OPAQUE"
    except Exception as e:
        print(f"[MMD Exporter] blend mode設定に失敗: {mat.name} / {e}")


def _build_image_cache():
    IMAGE_CACHE.clear()
    by_basename = {}

    for img in bpy.data.images:
        if img.source != "FILE":
            continue

        abs_path = _safe_abspath(img.filepath)
        norm = _normalize_path(abs_path)

        if norm:
            IMAGE_CACHE[norm] = img

        basename = os.path.normcase(
            os.path.basename(img.filepath.replace("\\", "/"))
        )

        if basename and basename not in by_basename:
            by_basename[basename] = img

    return by_basename


def _find_or_load_image(filepath, by_basename=None, search_dirs=None):
    if not filepath:
        return None

    search_dirs = search_dirs or []
    candidates = []

    abs_path = _safe_abspath(filepath)
    candidates.append(abs_path)

    basename = os.path.basename(filepath.replace("\\", "/"))

    for directory in search_dirs:
        if directory and basename:
            candidates.append(os.path.join(directory, basename))
            candidates.append(os.path.join(directory, filepath))

    for candidate in candidates:
        norm = _normalize_path(candidate)

        if not norm:
            continue

        if norm in IMAGE_CACHE:
            return IMAGE_CACHE[norm]

        if os.path.exists(candidate):
            try:
                img = bpy.data.images.load(candidate, check_existing=True)
                IMAGE_CACHE[norm] = img
                return img
            except Exception as e:
                print(f"[MMD Exporter] 画像読み込み失敗: {candidate} / {e}")

    if by_basename and basename:
        return by_basename.get(os.path.normcase(basename))

    return None


def _get_model_search_dirs():
    dirs = []

    if bpy.data.filepath:
        base = os.path.dirname(bpy.data.filepath)
        dirs.append(base)
        dirs.append(os.path.join(base, "textures"))
        dirs.append(os.path.join(base, "Textures"))
        dirs.append(os.path.join(base, "texture"))
        dirs.append(os.path.join(base, "Texture"))

    return dirs


def _get_principled_socket(node, names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None


def _link_if_possible(tree, output_socket, input_socket):
    if output_socket and input_socket:
        tree.links.new(output_socket, input_socket)


# ============================================================
# MMDマテリアル判定
# ============================================================

def _is_mmd_material(mat):
    """
    変換対象のMMDマテリアルか判定する。
    以下のいずれかに該当する場合はスキップ:
      - mmd_edge. で始まるエッジマテリアル（変換すると真っ黒になる）
      - mmd_material プロパティを持たない（MMD以外のマテリアル）
      - 既にPrincipled BSDFに変換済み（mmd_shaderノードがなく、
        かつPrincipled BSDFノードが存在する）
    """
    # エッジマテリアルは除外
    if mat.name.startswith("mmd_edge."):
        return False

    # mmd_material プロパティがなければMMD以外とみなす
    mmd_mat = getattr(mat, "mmd_material", None)
    if mmd_mat is None:
        return False

    # ノードツリーが存在しない場合は変換不可
    if not mat.use_nodes or mat.node_tree is None:
        return False

    nodes = mat.node_tree.nodes

    # mmd_shader ノードが存在する → 未変換のMMDマテリアル
    if nodes.get("mmd_shader") is not None:
        return True

    # mmd_shader はないが Principled BSDF もない → 変換が必要な状態
    has_principled = any(n.type == "BSDF_PRINCIPLED" for n in nodes)
    if not has_principled:
        return True

    # Principled BSDF が存在する → 変換済みとしてスキップ
    return False


# ============================================================
# MMDマテリアル情報取得
# ============================================================

_PALE_CACHE = {}

# 眼球マテリアルらしさを判定するためのキーワード
_EYE_KEYWORDS = (
    "eye", "iris", "pupil", "hitomi", "目", "瞳", "眼", "白目", "黒目",
    "eyeball", "eyewhite", "sirome", "kurome",
)


def _looks_like_eye_material(mat, base_image):
    """
    マテリアルが「眼球系（base が薄くスフィアで見た目を作る）」かどうかを
    名前ベースで推定する。肌や髪を巻き込まないよう、白地判定だけに頼らず
    マテリアル名・画像名にアイ系キーワードが含まれるかで判定する。
    """
    names = [mat.name.lower()]
    if base_image is not None:
        names.append(base_image.name.lower())
        try:
            names.append(base_image.filepath.lower())
        except Exception:
            pass

    for n in names:
        for kw in _EYE_KEYWORDS:
            if kw in n:
                return True
    return False


def _is_pale_base_image(image, threshold=0.75):
    """
    base画像が白っぽい下地かどうかを判定する。
    眼球マテリアルなど、base が淡くスフィアマップで見た目を作るタイプを
    検出するために使う。RGBの平均が threshold を超えたら淡いと判定。

    image.pixels への個別アクセスは極端に遅いため、numpy で一括取得し、
    結果は画像名をキーにキャッシュする。
    """
    if image is None:
        return False

    # キャッシュ参照（同じ画像を何度も走査しない）
    key = image.name
    if key in _PALE_CACHE:
        return _PALE_CACHE[key]

    result = False
    try:
        if image.has_data and len(image.pixels) >= 4:
            import numpy as np

            # 一括コピーは1回だけ。これが高速化の肝。
            px = np.array(image.pixels[:], dtype=np.float32)
            rgb = px.reshape(-1, 4)[:, :3]
            mean_val = float(rgb.mean())
            result = mean_val >= threshold
    except Exception:
        result = False

    _PALE_CACHE[key] = result
    return result


def _extract_images_from_nodes(mat):
    """
    nodes.clear() する前に、既存のノードツリーから画像オブジェクトを取得する。

    mmd_tools で読み込んだマテリアルは、テクスチャをファイルパス文字列ではなく
    ノードツリー内の Image Texture ノードとして保持している。
    慣習的に以下のノード名が使われる:
      - mmd_base_tex   : ベーステクスチャ
      - mmd_sphere_tex : スフィアマップ
      - mmd_toon_tex   : トゥーンテクスチャ（base には使わない）
    名前で見つからない場合は、全 Image Texture ノードから推定する。
    """
    base_image = None
    sphere_image = None

    if not mat.use_nodes or mat.node_tree is None:
        return base_image, sphere_image

    nodes = mat.node_tree.nodes

    # ── 名前で特定（mmd_tools の慣習）──
    base_node = nodes.get("mmd_base_tex")
    if base_node and getattr(base_node, "image", None):
        candidate = base_node.image
        # mmd_base_tex に toon/sphere 画像が入っているケースを除外。
        # （base が無いマテリアルで mmd_tools が toon を流用する場合がある）
        cand_name = candidate.name.lower()
        if not ("toon" in cand_name
                or cand_name.endswith(".spa")
                or cand_name.endswith(".sph")):
            base_image = candidate

    sphere_node = nodes.get("mmd_sphere_tex")
    if sphere_node and getattr(sphere_node, "image", None):
        sphere_image = sphere_node.image

    # ── フォールバック: 名前で base が見つからない場合 ──
    if base_image is None:
        toon_image = None
        sph_image_fallback = None
        candidates = []

        for node in nodes:
            if node.type != "TEX_IMAGE":
                continue
            img = getattr(node, "image", None)
            if not img:
                continue

            name_lower = node.name.lower()
            label_lower = (node.label or "").lower()
            img_name_lower = img.name.lower()

            # トゥーン判定: ノード名・ラベル・画像名のいずれかに toon を含む
            is_toon = (
                "toon" in name_lower
                or "toon" in label_lower
                or "toon" in img_name_lower
            )
            # スフィア判定: 名前ヒント、または加算/乗算スフィアの拡張子
            is_sphere = (
                "sphere" in name_lower
                or "sphere" in label_lower
                or "_sph" in name_lower
                or "sphere" in img_name_lower
                or img_name_lower.endswith(".spa")
                or img_name_lower.endswith(".sph")
            )

            if is_toon:
                toon_image = toon_image or img
                continue
            if is_sphere:
                sph_image_fallback = sph_image_fallback or img
                continue

            candidates.append(img)

        if candidates:
            base_image = candidates[0]
        # 重要: base候補が無い場合でも toon/sphere を base に採用しない。
        # toon は陰影用、sphere は反射用であり、これを base に使うと
        # 単色マテリアル（例: 黒いベルト）が白く塗られてしまう。
        # base が無いマテリアルは画像なし(None)のまま返し、
        # 呼び出し側で diffuse_color を使わせる。

        if sphere_image is None:
            sphere_image = sph_image_fallback

    # ── 診断: それでも画像が一切見つからなかった場合 ──
    if base_image is None:
        tex_node_names = [
            n.name for n in nodes if n.type == "TEX_IMAGE"
        ]
        print(
            f"[MMD Exporter] '{mat.name}': テクスチャ画像が見つかりません。"
            f" TEX_IMAGEノード={tex_node_names if tex_node_names else 'なし'}"
        )

    return base_image, sphere_image


def _extract_mmd_material_info(mat):
    diffuse = getattr(mat, "diffuse_color", (1.0, 1.0, 1.0, 1.0))
    alpha = diffuse[3] if len(diffuse) >= 4 else 1.0

    texture_path = ""
    sphere_path = ""
    sphere_texture_type = "0"
    is_double_sided = True

    mmd_mat = getattr(mat, "mmd_material", None)

    if mmd_mat:
        if hasattr(mmd_mat, "diffuse_color"):
            dc = mmd_mat.diffuse_color
            if len(dc) >= 3:
                diffuse = (dc[0], dc[1], dc[2], alpha)

        if hasattr(mmd_mat, "alpha"):
            alpha = float(mmd_mat.alpha)
            diffuse = (diffuse[0], diffuse[1], diffuse[2], alpha)

        for attr in ("texture", "texture_filepath", "texture_path"):
            if hasattr(mmd_mat, attr):
                value = getattr(mmd_mat, attr)
                if value and isinstance(value, str):
                    texture_path = value
                    break

        for attr in (
            "sphere_texture",
            "sphere_texture_filepath",
            "sphere_texture_path",
        ):
            if hasattr(mmd_mat, attr):
                value = getattr(mmd_mat, attr)
                if value and isinstance(value, str):
                    sphere_path = value
                    break

        # スフィアマップの種類を取得（乗算か否かの判定に使用）
        if hasattr(mmd_mat, "sphere_texture_type"):
            sphere_texture_type = str(getattr(mmd_mat, "sphere_texture_type", "0"))

        for attr in ("is_double_sided", "double_sided"):
            if hasattr(mmd_mat, attr):
                try:
                    is_double_sided = bool(getattr(mmd_mat, attr))
                    break
                except Exception:
                    pass

    return diffuse, alpha, texture_path, sphere_path, sphere_texture_type, is_double_sided


# ============================================================
# Principled BSDFマテリアル構築
# ============================================================

def _build_principled_material(
    mat,
    image=None,
    diffuse=(1.0, 1.0, 1.0, 1.0),
    alpha=1.0,
    is_double_sided=True,
    sph_image=None,
    apply_sphere=False,
):
    mat.use_nodes = True
    mat.diffuse_color = (diffuse[0], diffuse[1], diffuse[2], alpha)
    mat.use_backface_culling = not is_double_sided

    # テクスチャにアルファチャンネルがあるか、またはアルファ値が半透明なら透過設定
    # channels==4 を優先し、取得できない場合は depth でフォールバック判定する
    tex_has_alpha = False
    if image is not None:
        channels = getattr(image, "channels", None)
        if channels is not None:
            tex_has_alpha = channels == 4
        else:
            tex_has_alpha = getattr(image, "depth", 0) in (32, 128)
    use_alpha = tex_has_alpha or alpha < 0.999
    _set_blend_mode(mat, use_alpha)

    tree = mat.node_tree
    tree.nodes.clear()

    output = tree.nodes.new(type="ShaderNodeOutputMaterial")
    output.location = (500, 0)

    bsdf = tree.nodes.new(type="ShaderNodeBsdfPrincipled")
    bsdf.location = (250, 0)

    tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    base_color_socket = _get_principled_socket(bsdf, ["Base Color"])
    alpha_socket = _get_principled_socket(bsdf, ["Alpha"])
    roughness_socket = _get_principled_socket(bsdf, ["Roughness"])
    metallic_socket = _get_principled_socket(bsdf, ["Metallic"])

    if base_color_socket:
        base_color_socket.default_value = (
            diffuse[0],
            diffuse[1],
            diffuse[2],
            alpha,
        )

    if alpha_socket:
        alpha_socket.default_value = alpha

    if roughness_socket:
        roughness_socket.default_value = 0.6

    if metallic_socket:
        metallic_socket.default_value = 0.0

    if image:
        tex = tree.nodes.new(type="ShaderNodeTexImage")
        tex.location = (-500, 100)
        tex.image = image

        # ベースカラー用テクスチャは sRGB であるべき。
        # Non-Color や Linear になっていると色が暗く・濁って見える。
        try:
            if image.colorspace_settings.name not in ("sRGB", "Filmic sRGB"):
                image.colorspace_settings.name = "sRGB"
        except Exception:
            pass

        # スフィアマップ（乗算）を適用する場合。
        # MMDのスフィアは「カメラ空間での法線の向き」を 0〜1 のUVに
        # マッピングしてサンプリングする。TexCoordのNormalをそのまま
        # Vectorに繋ぐとサンプリング位置がずれて色が濁るため、
        # Vector Transform でカメラ空間に変換し、(N*0.5+0.5) でUV化する。
        if sph_image and apply_sphere:
            sph = tree.nodes.new(type="ShaderNodeTexImage")
            sph.location = (-300, -200)
            sph.image = sph_image
            # スフィアは色情報ではなく反射の強度的に使われるが、
            # MMDの実装に合わせ sRGB のまま扱う
            try:
                sph.image.colorspace_settings.name = "sRGB"
            except Exception:
                pass

            geom = tree.nodes.new(type="ShaderNodeNewGeometry")
            geom.location = (-1100, -250)

            # 法線をカメラ（ビュー）空間へ変換
            vec_xform = tree.nodes.new(type="ShaderNodeVectorTransform")
            vec_xform.location = (-900, -250)
            vec_xform.vector_type = "NORMAL"
            vec_xform.convert_from = "WORLD"
            vec_xform.convert_to = "CAMERA"
            _link_if_possible(tree, geom.outputs.get("Normal"), vec_xform.inputs.get("Vector"))

            # (N * 0.5 + 0.5) でUV座標(0〜1)に変換
            mad = tree.nodes.new(type="ShaderNodeVectorMath")
            mad.location = (-700, -250)
            mad.operation = "MULTIPLY_ADD"
            mad.inputs[1].default_value = (0.5, 0.5, 0.5)
            mad.inputs[2].default_value = (0.5, 0.5, 0.5)
            _link_if_possible(tree, vec_xform.outputs.get("Vector"), mad.inputs[0])
            _link_if_possible(tree, mad.outputs.get("Vector"), sph.inputs.get("Vector"))

            try:
                mix = tree.nodes.new(type="ShaderNodeMix")
                mix.location = (-50, 50)
                mix.data_type = "RGBA"
                mix.factor_mode = "UNIFORM"
                mix.blend_type = "MULTIPLY"

                if "Factor" in mix.inputs:
                    mix.inputs["Factor"].default_value = 1.0

                _link_if_possible(tree, tex.outputs.get("Color"), mix.inputs.get("A"))
                _link_if_possible(tree, sph.outputs.get("Color"), mix.inputs.get("B"))
                _link_if_possible(tree, mix.outputs.get("Result"), base_color_socket)

            except Exception:
                # Blender 4.1以前: ShaderNodeMixRGB を使う
                mix = tree.nodes.new(type="ShaderNodeMixRGB")
                mix.location = (-50, 50)
                mix.blend_type = "MULTIPLY"
                mix.inputs[0].default_value = 1.0

                _link_if_possible(tree, tex.outputs.get("Color"), mix.inputs[1])
                _link_if_possible(tree, sph.outputs.get("Color"), mix.inputs[2])
                _link_if_possible(tree, mix.outputs.get("Color"), base_color_socket)

        else:
            _link_if_possible(tree, tex.outputs.get("Color"), base_color_socket)

        # テクスチャにアルファチャンネルがある場合のみアルファをリンク
        # （リンクした後に default_value を上書きしない）
        if tex_has_alpha and alpha_socket:
            _link_if_possible(tree, tex.outputs.get("Alpha"), alpha_socket)


# ============================================================
# エクスポート前処理
# ============================================================

def _hide_mmd_internal_objects():
    hidden_states = []

    for obj in bpy.data.objects:
        if obj.name.startswith(".dummy_armature") or "mmd_bind" in obj.name:
            hidden_states.append((obj, obj.hide_viewport, obj.hide_render))
            obj.hide_viewport = True
            obj.hide_render = True

    print(f"[MMD Exporter] 内部オブジェクトを一時非表示: {len(hidden_states)}件")
    return hidden_states


def _restore_hidden_objects(hidden_states):
    for obj, hide_viewport, hide_render in hidden_states:
        try:
            obj.hide_viewport = hide_viewport
            obj.hide_render = hide_render
        except ReferenceError:
            pass


def _mute_sdef_shape_keys():
    muted_states = []

    for mesh in bpy.data.meshes:
        if not mesh.shape_keys:
            continue

        for key_block in mesh.shape_keys.key_blocks:
            if key_block.name.startswith("mmd_sdef_"):
                muted_states.append((key_block, key_block.mute))
                key_block.mute = True

    print(f"[MMD Exporter] SDEFシェイプキーを一時ミュート: {len(muted_states)}件")
    return muted_states


def _restore_sdef_shape_keys(muted_states):
    for key_block, mute in muted_states:
        try:
            key_block.mute = mute
        except ReferenceError:
            pass


# ============================================================
# Step 1: マテリアル変換
# ============================================================

class MMD_OT_ConvertMaterials(Operator):
    bl_idname = "mmd.convert_materials"
    bl_label = "マテリアルを変換"
    bl_description = "MMDマテリアルをPrincipled BSDFへ変換します（エッジ・変換済みはスキップ）"
    bl_options = {"REGISTER", "UNDO"}

    sphere_mode: EnumProperty(
        name="スフィアマップ",
        description="スフィアマップ（乗算）の適用方法",
        items=[
            ("NONE", "適用しない（推奨）",
             "スフィアを一切使わない。base画像のみ。glTF出力に最も安全"),
            ("AUTO", "自動（眼球のみ）",
             "眼球系マテリアル（名前にeye/目/瞳等を含み、base画像が白っぽい）にのみ適用"),
            ("ALL", "常に適用",
             "全マテリアルに適用。MMD本来寄りだが暗く濁る場合がある"),
        ],
        default="NONE",
    )

    def execute(self, context):
        by_basename = _build_image_cache()
        search_dirs = _get_model_search_dirs()
        _PALE_CACHE.clear()

        converted = 0
        skipped = 0
        missing_textures = []

        for mat in bpy.data.materials:
            # ── 変換対象外はスキップ ──
            if not _is_mmd_material(mat):
                skipped += 1
                continue

            # nodes.clear() される前に既存ノードから画像を取得（最優先）
            node_image, node_sphere = _extract_images_from_nodes(mat)

            diffuse, alpha, texture_path, sphere_path, sphere_texture_type, is_double_sided = (
                _extract_mmd_material_info(mat)
            )

            image = node_image
            sph_image = None

            # 既存ノードに画像がなければファイルパスから探す（フォールバック）
            if image is None and texture_path:
                image = _find_or_load_image(
                    texture_path,
                    by_basename=by_basename,
                    search_dirs=search_dirs,
                )

            if image is None and texture_path:
                missing_textures.append(texture_path)

            # 乗算スフィアマップのみ対応（加算・無効はスキップ）
            is_mult_sphere = sphere_texture_type in ("1", "MULT", "Multiply", "multiply")
            if node_sphere is not None:
                sph_image = node_sphere
            elif sphere_path and is_mult_sphere:
                sph_image = _find_or_load_image(
                    sphere_path,
                    by_basename=by_basename,
                    search_dirs=search_dirs,
                )

            # ── スフィア適用可否を決定 ──
            if self.sphere_mode == "ALL":
                apply_sphere = sph_image is not None
            elif self.sphere_mode == "NONE":
                apply_sphere = False
            else:  # AUTO: 眼球系マテリアル（名前がアイ系 かつ base が白地）のみ
                apply_sphere = (
                    sph_image is not None
                    and _looks_like_eye_material(mat, image)
                    and _is_pale_base_image(image)
                )
                if apply_sphere:
                    print(f"[MMD Exporter] '{mat.name}': 眼球と判定しスフィア適用")

            _build_principled_material(
                mat,
                image=image,
                diffuse=diffuse,
                alpha=alpha,
                is_double_sided=is_double_sided,
                sph_image=sph_image,
                apply_sphere=apply_sphere,
            )

            converted += 1

        if missing_textures:
            for path in missing_textures[:10]:
                print(f"[MMD Exporter] 未検出テクスチャ: {path}")
            self.report(
                {"WARNING"},
                f"マテリアル変換完了: {converted}件 / スキップ: {skipped}件 / 未検出テクスチャ: {len(missing_textures)}件",
            )
        else:
            self.report({"INFO"}, f"マテリアル変換完了: {converted}件 / スキップ: {skipped}件")

        return {"FINISHED"}


# ============================================================
# Step 2: ボーン名の英語変換
# ============================================================

_BONE_NAME_MAP = {
    "全ての親": "Root",
    "センター": "Center",
    "グルーブ": "Groove",
    "腰": "Waist",
    "上半身": "UpperBody",
    "上半身2": "UpperBody2",
    "首": "Neck",
    "頭": "Head",
    "両目": "Eyes",
    "左目": "Eye_L",
    "右目": "Eye_R",
    "左肩": "Shoulder_L",
    "左腕": "Arm_L",
    "左ひじ": "Elbow_L",
    "左手首": "Wrist_L",
    "右肩": "Shoulder_R",
    "右腕": "Arm_R",
    "右ひじ": "Elbow_R",
    "右手首": "Wrist_R",
    "左足": "Leg_L",
    "左ひざ": "Knee_L",
    "左足首": "Ankle_L",
    "左つま先": "Toe_L",
    "右足": "Leg_R",
    "右ひざ": "Knee_R",
    "右足首": "Ankle_R",
    "右つま先": "Toe_R",
    "左足ＩＫ": "LegIK_L",
    "右足ＩＫ": "LegIK_R",
    "左つま先ＩＫ": "ToeIK_L",
    "右つま先ＩＫ": "ToeIK_R",
}


def _iter_armatures():
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            yield obj


def _unique_bone_name(armature_data, desired_name, current_bone=None):
    if not desired_name:
        return None

    if desired_name not in armature_data.bones:
        return desired_name

    if current_bone and current_bone.name == desired_name:
        return desired_name

    index = 1
    while True:
        candidate = f"{desired_name}.{index:03d}"
        if candidate not in armature_data.bones:
            return candidate
        index += 1


class MMD_OT_RenameBones(Operator):
    bl_idname = "mmd.rename_bones"
    bl_label = "ボーン名を英語に変換"
    bl_description = "mmd_toolsの英語名プロパティ、または簡易変換テーブルでボーン名を英語化します"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armatures = list(_iter_armatures())

        if not armatures:
            self.report({"WARNING"}, "Armatureが見つかりません")
            return {"CANCELLED"}

        renamed = 0

        for arm_obj in armatures:
            for bone in arm_obj.data.bones:
                original_name = bone.name
                desired_name = None

                mmd_bone = getattr(bone, "mmd_bone", None)

                if mmd_bone:
                    for attr in ("name_e", "name_en", "english_name"):
                        if hasattr(mmd_bone, attr):
                            value = getattr(mmd_bone, attr)
                            if value:
                                desired_name = value
                                break

                if not desired_name:
                    desired_name = _BONE_NAME_MAP.get(original_name)

                if desired_name and desired_name != original_name:
                    new_name = _unique_bone_name(
                        arm_obj.data,
                        desired_name,
                        current_bone=bone,
                    )
                    if new_name and new_name != original_name:
                        bone.name = new_name
                        renamed += 1

        self.report({"INFO"}, f"ボーン名変換完了: {renamed}件")
        return {"FINISHED"}


# ============================================================
# Step 3: GLBエクスポート
# ============================================================

# export_apply を True にするとアーマチュアモディファイアが二重適用され
# スキンウェイトが崩れる既知の問題があるため False にする。
# モデファイアを適用したい場合は手動で適用してからエクスポートすること。
_GLTF_EXPORT_PARAMS = {
    "export_format": "GLB",
    "use_visible": True,
    "use_selection": False,
    "export_apply": False,   # ← True から False に変更（スキン崩れ防止）
    "export_yup": True,
    "export_texcoords": True,
    "export_normals": True,
    "export_tangents": True,
    "export_materials": "EXPORT",
    "export_colors": True,
    "export_skins": True,
}

_GLTF_EXPORT_PARAMS_FALLBACK = {
    "export_format": "GLB",
    "use_visible": True,
    "export_apply": False,
    "export_yup": True,
    "export_materials": "EXPORT",
    "export_skins": True,
}


class MMD_OT_ExportGLTF(Operator, ExportHelper):
    bl_idname = "mmd.export_gltf"
    bl_label = "GLBとしてエクスポート"
    bl_description = "Unity / Unreal Engine向けにGLBファイルを書き出します"

    filename_ext = ".glb"
    filter_glob: StringProperty(
        default="*.glb",
        options={"HIDDEN"},
    )

    export_animations: BoolProperty(
        name="アニメーションを出力",
        default=True,
    )

    export_morphs: BoolProperty(
        name="モーフを出力",
        default=True,
    )

    convert_materials_before_export: BoolProperty(
        name="出力前にマテリアル変換",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_animations")
        layout.prop(self, "export_morphs")
        layout.prop(self, "convert_materials_before_export")

    def execute(self, context):
        filepath = self.filepath
        if not filepath.lower().endswith(".glb"):
            filepath += ".glb"

        # マテリアル変換はエクスポートダイアログが閉じた後（execute内）で
        # 呼ぶ必要があるため、ここで直接関数を呼ぶ（bpy.ops経由ではなく）
        if self.convert_materials_before_export:
            by_basename = _build_image_cache()
            search_dirs = _get_model_search_dirs()
            _run_convert_materials(by_basename, search_dirs)

        hidden_states = _hide_mmd_internal_objects()
        muted_states = _mute_sdef_shape_keys()

        try:
            params = dict(_GLTF_EXPORT_PARAMS)
            params["filepath"] = filepath
            params["export_morph"] = self.export_morphs
            params["export_animations"] = self.export_animations

            try:
                bpy.ops.export_scene.gltf(**params)

            except TypeError:
                # Blenderのバージョン差で一部引数が未対応の場合のフォールバック
                fallback_params = dict(_GLTF_EXPORT_PARAMS_FALLBACK)
                fallback_params["filepath"] = filepath
                fallback_params["export_morph"] = self.export_morphs
                fallback_params["export_animations"] = self.export_animations
                bpy.ops.export_scene.gltf(**fallback_params)

        except Exception as e:
            self.report({"ERROR"}, f"GLBエクスポート失敗: {e}")
            return {"CANCELLED"}

        finally:
            _restore_sdef_shape_keys(muted_states)
            _restore_hidden_objects(hidden_states)

        self.report({"INFO"}, f"GLBエクスポート完了: {filepath}")
        return {"FINISHED"}


def _run_convert_materials(by_basename, search_dirs, sphere_mode="NONE"):
    """
    bpy.ops を経由せずマテリアル変換を直接実行する内部関数。
    エクスポートダイアログのコンテキスト問題を回避するために使用。
    """
    _PALE_CACHE.clear()

    for mat in bpy.data.materials:
        if not _is_mmd_material(mat):
            continue

        node_image, node_sphere = _extract_images_from_nodes(mat)

        diffuse, alpha, texture_path, sphere_path, sphere_texture_type, is_double_sided = (
            _extract_mmd_material_info(mat)
        )

        image = node_image
        sph_image = None

        if image is None and texture_path:
            image = _find_or_load_image(
                texture_path,
                by_basename=by_basename,
                search_dirs=search_dirs,
            )

        is_mult_sphere = sphere_texture_type in ("1", "MULT", "Multiply", "multiply")
        if node_sphere is not None:
            sph_image = node_sphere
        elif sphere_path and is_mult_sphere:
            sph_image = _find_or_load_image(
                sphere_path,
                by_basename=by_basename,
                search_dirs=search_dirs,
            )

        if sphere_mode == "ALL":
            apply_sphere = sph_image is not None
        elif sphere_mode == "NONE":
            apply_sphere = False
        else:  # AUTO
            apply_sphere = (
                sph_image is not None
                and _looks_like_eye_material(mat, image)
                and _is_pale_base_image(image)
            )

        _build_principled_material(
            mat,
            image=image,
            diffuse=diffuse,
            alpha=alpha,
            is_double_sided=is_double_sided,
            sph_image=sph_image,
            apply_sphere=apply_sphere,
        )


# ============================================================
# サイドバーパネル
# ============================================================

class MMD_PT_ExporterPanel(Panel):
    bl_label = "MMD → glTF Exporter"
    bl_idname = "MMD_PT_exporter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Exporter"

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)

        col.label(text="Step 1")
        col.operator(
            "mmd.convert_materials",
            icon="MATERIAL",
        )

        col.separator()

        col.label(text="Step 2")
        col.operator(
            "mmd.rename_bones",
            icon="ARMATURE_DATA",
        )

        col.separator()

        col.label(text="Step 3")
        col.operator(
            "mmd.export_gltf",
            icon="EXPORT",
        )


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
    print("MMD to glTF Exporter v2.3.0: 有効化されました")


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    print("MMD to glTF Exporter v2.3.0: 無効化されました")


if __name__ == "__main__":
    register()
