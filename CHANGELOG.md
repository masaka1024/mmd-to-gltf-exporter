# Changelog

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
