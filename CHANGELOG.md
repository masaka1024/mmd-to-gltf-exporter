# Changelog

## v2.6.1
- FBX エクスポートの「モディファイアを適用」を既定オフに変更。シェイプキー（= MMD モーフ：まばたき・口パク等）を保持したままエクスポートできるようにした
  - Blender はシェイプキーを持つメッシュにモディファイアを適用できないため、オンにするとモーフが失われる。FBX は座標軸で向きを処理でき、適用なしでもジオメトリが裏返らないことを確認（Unity で動作確認済み）

## v2.6.0
- FBX エクスポート機能を追加（Unity / Unreal Engine 向け）
  - `add_leaf_bones=False`、`mesh_smooth_type=FACE`、テクスチャ埋め込みなど、ゲームエンジン取り込みに適した既定値
  - 前処理（内部オブジェクト非表示・SDEF ミュート）は glTF と共通化
- アドオン名を「MMD to glTF Exporter」→「MMD Exporter」に変更（glTF / FBX 両対応のため）
- パネル Step 3 を「GLB / FBX」の出力選択に変更
- 
## [2.5.5] - 2025
### 追加
- SDEF シェイプキー（`mmd_sdef_c/r0/r1`）をエクスポート前に一時ミュート
- mmd_tools 内部オブジェクト（`.dummy_armature`、`mmd_bind` 含む）の自動非表示化

### 改善
- 画像キャッシュを `_build_image_cache()` で一括構築し、変換ループ内の線形探索を排除
- Windows でのパス正規化強化（`os.path.normcase` + バックスラッシュ統一）
- 使用中の画像のみ GLB に埋め込む処理に変更（不要な pack() 呼び出しを削減）

### 対応
- Blender 4.2 (EEVEE Next) の `surface_render_method` によるアルファ設定

## [2.0.0]
### 追加
- スフィアマップ（.sph）の MixRGB Multiply ノードによる合成
- 両面表示フラグ（`is_double_sided`）への対応
- ボーン名英語変換テーブル（`JP_TO_EN`）

## [1.0.0]
- 初期リリース（マテリアル変換・GLB エクスポートの基本機能）
