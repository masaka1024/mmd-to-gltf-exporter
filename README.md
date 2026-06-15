
MMD to glTF Exporter
Blender アドオン — mmd_tools で読み込んだ MMD モデルを glTF (GLB) 形式に変換・エクスポートします。
機能
ステップ	内容
Step 1	MMD シェーダー → Principled BSDF へマテリアル変換
Step 2	日本語ボーン名 → 英語名に変換（Unity / UE 対応）
Step 3	GLB としてエクスポート（非表示オブジェクト除外）
動作環境
Blender 4.2 以上（3.x 系でも動作しますが非推奨）
mmd_tools がインストールされていること
インストール
右上の Code → Download ZIP でこのリポジトリをダウンロード
Blender を起動 → 編集 → プリファレンス → アドオン → インストール
ダウンロードした ZIP 内の `mmd\_to\_gltf\_exporter.py` を選択
アドオン一覧で 「MMD to glTF Exporter」 を有効化
使い方
mmd_tools で MMD モデル（.pmx）を読み込む
`3D ビューポート > サイドバー (N キー) > MMD Exporter` タブを開く
Step 1 → Step 2 → Step 3 の順にボタンを押す
変更履歴
v2.5.5（最新）
Windows パス正規化を改善（バックスラッシュ統一）
画像キャッシュを一括構築する仕組みに変更（処理速度向上）
SDEF シェイプキーを一時ミュートしてエクスポート時の破綻を防止
mmd_tools 内部オブジェクト（`.dummy\_armature` 等）を自動非表示化
Blender 4.2 (EEVEE Next) のアルファ設定に対応
ライセンス
MIT License — 詳細は LICENSE を参照してください。
