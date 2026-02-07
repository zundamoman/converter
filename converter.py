import streamlit as st
import geopandas as gpd
import json
import tempfile
import zipfile
import os
import io
import re
import configparser
from shapely.geometry import shape, Polygon, MultiPolygon, LineString

# ページ基本設定
st.set_page_config(page_title="Agri Data Converter", layout="wide")

# --- サイドバーでツールを切り替え ---
st.sidebar.title("🛠️ ツール選択")
tool_mode = st.sidebar.radio(
    "使用する機能を選んでください：",
    ("DJI 境界線変換", "トプコン A-Bライン変換")
)

# --- 共通のヘルプ表示 ---
st.sidebar.info("複数のファイルを一気にドロップして、1つのZIPでまとめてダウンロードできます。")

# ----------------------------------------------------------------
# モード1：DJI 境界線データ → SHP 変換
# ----------------------------------------------------------------
if tool_mode == "DJI 境界線変換":
    st.title("🚁 DJI 境界線データ → SHP 変換ツール")
    st.write("DJIの「圃場データ」ファイルをアップロードしてください。")

    uploaded_files = st.file_uploader("DJIファイルをドロップ", accept_multiple_files=True)

    if uploaded_files:
        # アップロードファイルの一覧表示（20個以上でもスクロールで確認可能）
        st.subheader(f"📄 アップロード済み: {len(uploaded_files)} 件")
        with st.expander("ファイル名を確認する", expanded=True):
            # 高さを固定してスクロール可能にする（擬似的に多数表示に対応）
            st.markdown(
                f'<div style="max-height: 300px; overflow-y: auto;">'
                f'{"<br>".join([f"✅ {f.name}" for f in uploaded_files])}'
                f'</div>', 
                unsafe_allow_html=True
            )

        zip_buffer = io.BytesIO()
        success_count = 0
        
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            with tempfile.TemporaryDirectory() as tmpdir:
                for uploaded_file in uploaded_files:
                    try:
                        raw_bytes = uploaded_file.read()
                        text_content = raw_bytes.decode("utf-8")
                        json_match = re.search(r'\{.*\}', text_content, re.DOTALL)
                        if not json_match: continue
                        
                        data = json.loads(json_match.group(0))
                        features = []
                        if "features" in data:
                            for feat in data["features"]:
                                if "Polygon" not in feat["geometry"]["type"]: continue
                                geom = shape(feat["geometry"])
                                if geom.has_z:
                                    if geom.geom_type == 'Polygon':
                                        geom = Polygon([(p[0], p[1]) for p in geom.exterior.coords])
                                    elif geom.geom_type == 'MultiPolygon':
                                        geom = MultiPolygon([Polygon([(p[0], p[1]) for p in poly.exterior.coords]) for poly in geom.geoms])
                                if not geom.is_empty:
                                    props = {str(k): (str(v) if isinstance(v, (dict, list)) else v) for k, v in feat.get("properties", {}).items()}
                                    props['geometry'] = geom
                                    features.append(props)

                        base_name = os.path.splitext(uploaded_file.name)[0]
                        if features:
                            gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
                            shp_path = os.path.join(tmpdir, base_name + ".shp")
                            gdf.to_file(shp_path, driver='ESRI Shapefile', encoding='utf-8')
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                f_path = os.path.join(tmpdir, base_name + ext)
                                if os.path.exists(f_path):
                                    zf.write(f_path, arcname=f"{base_name}/{base_name}{ext}")
                            success_count += 1
                    except Exception: continue

        if success_count > 0:
            st.success(f"✅ {success_count} 件のDJIデータを変換しました。")
            st.download_button("Shapefile (.zip) を保存", zip_buffer.getvalue(), "dji_converted.zip")

# ----------------------------------------------------------------
# モード2：トプコン A-Bライン変換
# ----------------------------------------------------------------
else:
    st.title("🚜 トプコン A-Bライン一括変換")
    st.write("トプコンの `.ini` ファイルをアップロードしてください。")

    uploaded_files = st.file_uploader("iniファイルをドロップ", type="ini", accept_multiple_files=True)

    if uploaded_files:
        st.subheader(f"📄 アップロード済み: {len(uploaded_files)} 件")
        with st.expander("ファイル名を確認する", expanded=True):
            st.markdown(
                f'<div style="max-height: 300px; overflow-y: auto;">'
                f'{"<br>".join([f"✅ {f.name}" for f in uploaded_files])}'
                f'</div>', 
                unsafe_allow_html=True
            )

        zip_buffer = io.BytesIO()
        success_count = 0
        
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            with tempfile.TemporaryDirectory() as tmpdir:
                for uploaded_file in uploaded_files:
                    try:
                        raw_data = uploaded_file.read()
                        content = None
                        for enc in ['utf-8', 'utf-16', 'shift-jis']:
                            try:
                                content = raw_data.decode(enc)
                                break
                            except: continue
                        if not content: continue

                        config = configparser.ConfigParser()
                        config.read_string(content)
                        if 'APoint' in config and 'BPoint' in config:
                            line = LineString([
                                (float(config['APoint']['Longitude']), float(config['APoint']['Latitude'])),
                                (float(config['BPoint']['Longitude']), float(config['BPoint']['Latitude']))
                            ])
                            base_name = os.path.splitext(uploaded_file.name)[0]
                            gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                            file_out = os.path.join(tmpdir, base_name)
                            gdf.to_file(file_out + ".shp", driver='ESRI Shapefile', encoding='utf-8')
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                if os.path.exists(file_out + ext):
                                    zf.write(file_out + ext, arcname=f"{base_name}/{base_name}{ext}")
                            success_count += 1
                    except Exception: continue

        if success_count > 0:
            st.success(f"✅ {success_count} 件のトプコンデータを変換しました。")
            st.download_button("Shapefile (.zip) を保存", zip_buffer.getvalue(), "topcon_converted.zip")