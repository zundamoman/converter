# Agri Data Converter

Streamlitで動作する農業データ変換ツールです。

## 現在の主な機能

### DJI
- 境界線データ → SHP

### Topcon
- **Topcon XML圃場データ → SHP / GeoJSON / ISOXML（試験）**
- 複数ZIPアップロード
- ZIP内の複数XML / 複数PFD（圃場）に対応
- ZIP内ZIPにも対応
- XMLの情報から `Client → Farm → Field` を自動分類
- `Boundaries` と `ABlines` を分離
- 変換後ZIPには今回生成したファイルだけを格納
- 既存のABライン / 曲線 / 境界修復機能も維持

## Topcon XMLの出力構造

```text
Converted_SHP.zip
└─ Client
   └─ Farm
      └─ Field
         ├─ Boundaries
         │  ├─ Boundary_001.shp
         │  ├─ Boundary_001.shx
         │  ├─ Boundary_001.dbf
         │  ├─ Boundary_001.prj
         │  └─ Boundary_001.cpg
         └─ ABlines
            ├─ ABLine_001.shp
            ├─ ABLine_001.shx
            ├─ ABLine_001.dbf
            ├─ ABLine_001.prj
            └─ ABLine_001.cpg
```

## 対応メーカーについて

Topconは提供された実データでXML構造を確認しています。

John Deere等、他メーカーについては現時点では対応確認済みとはしていません。

## ISOXMLについて

ISOXML出力は現在、境界線・ABラインのジオメトリを中心とした試験実装です。
メーカー・端末ごとの完全なISOXML互換性は、実機での検証が必要です。

## Streamlit Community Cloud

GitHubのこのリポジトリをStreamlit Community Cloudに接続し、
`app.py` をMain file pathとしてデプロイします。
