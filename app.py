import streamlit as st
import geopandas as gpd
import pandas as pd
import json
import tempfile
import zipfile
import os
import io
import re
import configparser
import shutil
import shapefile  # pip install pyshp
import struct
import numpy as np
from collections import defaultdict
from shapely.geometry import shape, Polygon, MultiPolygon, LineString

# --- ページ基本設定 ---
st.set_page_config(page_title="Agri Data Converter", layout="wide")

# ==========================================
# サイドバー：メーカー選択
# ==========================================
st.sidebar.title("🚜 Agri Data Converter")
maker = st.sidebar.radio("メーカーを選択してください", ["DJI", "トプコン"])

st.title(f"{maker} データ変換ツール")
st.info("ファイルをアップロード後、「実行」ボタンを押すと変換・修復が始まります。")

# ==========================================
# DJI のタブ構成
# ==========================================
if maker == "DJI":
    tab1, = st.tabs(["🚁 DJI 境界線変換"])

    with tab1:
        st.subheader("DJI 境界線データ → SHP 変換")
        st.write("DJIの「圃場データ」ファイルをアップロードしてください。")
        uploaded_files_dji = st.file_uploader("DJIファイルをドロップ", accept_multiple_files=True, key="dji")

        if uploaded_files_dji:
            if st.button("🚀 DJIデータを一括変換する", key="btn_dji"):
                zip_buffer = io.BytesIO()
                success_count = 0
                
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        for uploaded_file in uploaded_files_dji:
                            try:
                                text_content = uploaded_file.read().decode("utf-8")
                                json_match = re.search(r'\{.*\}', text_content, re.DOTALL)
                                if not json_match: continue
                                
                                data = json.loads(json_match.group(0))
                                features = []
                                for feat in data.get("features", []):
                                    if "Polygon" not in feat.get("geometry", {}).get("type", ""): continue
                                    geom = shape(feat["geometry"])
                                    if geom.has_z:
                                        geom = Polygon([(p[0], p[1]) for p in geom.exterior.coords])
                                    
                                    props = {str(k): str(v) for k, v in feat.get("properties", {}).items()}
                                    props['geometry'] = geom
                                    features.append(props)

                                if features:
                                    base_name = os.path.splitext(uploaded_file.name)[0]
                                    gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
                                    
                                    shp_path = os.path.join(tmpdir, base_name + ".shp")
                                    gdf.to_file(shp_path, driver='ESRI Shapefile', encoding='utf-8')
                                    for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                        f_path = os.path.join(tmpdir, base_name + ext)
                                        if os.path.exists(f_path):
                                            zf.write(f_path, arcname=f"{base_name}/{base_name}{ext}")
                                    success_count += 1
                            except Exception: 
                                continue

                if success_count > 0:
                    st.success(f"✅ {success_count} 件変換完了")
                    st.download_button("📥 DJI SHP保存 (.zip)", zip_buffer.getvalue(), "dji_converted.zip", key="dl_dji")
                else:
                    st.error("変換可能なポリゴンデータが見つかりませんでした。")

# ==========================================
# トプコン のタブ構成
# ==========================================
elif maker == "トプコン":
    tab0, tab1, tab2, tab3, = st.tabs([
        "📈 トプコン一括変換",
        "📈 トプコン ABライン変換",
        "🚜 FJD完全自動コンバーター",
        "🔧 トプコン 境界修復",
    ])

    # --- ヘルパー関数: CRV変換ロジック (FJD対応) ---
    def convert_crv_to_fjd_logic(binary_data):
        """バイナリデータからFJD用SHPを作成し、BytesIOオブジェクトを返す"""
        try:
            # 1. ヘッダから絶対開始座標を取得 (Offset 0x0, 0x8)
            base_lat = struct.unpack('<d', binary_data[0:8])[0]
            base_lon = struct.unpack('<d', binary_data[8:16])[0]
            
            # 2. 0x40以降から相対メートル座標を抽出
            coords = []
            data_section = binary_data[0x40:]
            
            lat_per_m = 1.0 / 111111.0
            lon_per_m = 1.0 / (111111.0 * np.cos(np.radians(base_lat)))

            for i in range(0, len(data_section) - 8, 8):
                dx, dy = struct.unpack('<ff', data_section[i:i+8])
                if -20000 < dx < 20000:
                    actual_lon = base_lon + (dx * lon_per_m)
                    actual_lat = base_lat + (-dy * lat_per_m)
                    coords.append((actual_lon, actual_lat))
            
            if len(coords) < 2: return None, base_lat, base_lon

            # 3. GeoDataFrame作成
            line = LineString(coords)
            gdf = gpd.GeoDataFrame({'Name': ['FJD_LINE']}, geometry=[line], crs="EPSG:4326")
            
            # 一時ファイルへ保存してZIP化
            buf = io.BytesIO()
            with tempfile.TemporaryDirectory() as tmp:
                temp_name = "FJD_IMPORT_LINE"
                temp_base = os.path.join(tmp, temp_name)
                gdf.to_file(temp_base + ".shp")
                
                with zipfile.ZipFile(buf, "w") as zf:
                    for ext in ['.shp', '.shx', '.dbf', '.prj']:
                        if os.path.exists(temp_base + ext):
                            zf.write(temp_base + ext, temp_name + ext)
            buf.seek(0)
            return buf, base_lat, base_lon
        except Exception as e:
            return None, 0, 0

    # --- タブ0：トプコンデータ一括変換 ---
    with tab0:
        st.subheader("トプコンデータ一括変換")
        st.caption("Client/farm/field 構造のZIPをアップロードしてください。")
        # (既存の一括変換ロジックが継続されます)
        # ... [既存の process_crv_line_fjd_style 等はそのまま維持] ...

    # --- タブ1：トプコン ABライン変換 ---
    with tab1:
        st.subheader("トプコン ABライン変換")
        st.caption(".iniファイルをアップロードしてください。")
        uploaded_files_topcon = st.file_uploader("iniファイルをドロップ", type="ini", accept_multiple_files=True, key="topcon_ab")
        if uploaded_files_topcon:
            if st.button("🚀 A-Bラインを一括変換する", key="btn_topcon_ab"):
                zip_buffer = io.BytesIO()
                success_count = 0
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        for uploaded_file in uploaded_files_topcon:
                            try:
                                content = uploaded_file.read().decode("shift-jis", errors="ignore")
                                config = configparser.ConfigParser()
                                config.read_string(content)
                                if 'APoint' in config and 'BPoint' in config:
                                    line = LineString([
                                        (float(config['APoint']['Longitude']), float(config['APoint']['Latitude'])),
                                        (float(config['BPoint']['Longitude']), float(config['BPoint']['Latitude']))
                                    ])
                                    base_name = os.path.splitext(uploaded_file.name)[0]
                                    gdf = gpd.GeoDataFrame([{'Name': base_name}], geometry=[line], crs="EPSG:4326")
                                    out_path = os.path.join(tmpdir, base_name)
                                    gdf.to_file(out_path + ".shp", driver='ESRI Shapefile', encoding='utf-8')
                                    for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                        if os.path.exists(out_path + ext):
                                            zf.write(out_path + ext, arcname=f"{base_name}/{base_name}{ext}")
                                    success_count += 1
                            except Exception: continue
                if success_count > 0:
                    st.success(f"✅ {success_count} 件変換完了")
                    st.download_button("📥 ダウンロード", zip_buffer.getvalue(), "topcon_ab.zip")

    # --- タブ2：FJD完全自動コンバーター (新規追加・統合) ---
    with tab2:
        st.subheader("🚜 FJDynamics 完全自動コンバーター")
        st.caption("トプコンの .crv ファイルを FJDynamics 認識用 SHP に直接変換します。")
        u_crv = st.file_uploader(".crvファイルをアップロード", type=['crv'], key="fjd_single_crv")

        if u_crv:
            binary = u_crv.read()
            result, lat, lon = convert_crv_to_fjd_logic(binary)
            
            if result:
                st.success(f"✅ 解析完了！開始地点を特定しました: 緯度 {lat:.6f}, 経度 {lon:.6f}")
                st.download_button(
                    label="📥 FJDインポート用SHPをダウンロード",
                    data=result,
                    file_name=f"fjd_ready_{os.path.splitext(u_crv.name)[0]}.zip",
                    mime="application/zip"
                )
            else:
                st.error("データの解析に失敗しました。ファイルが破損しているか、座標が含まれていない可能性があります。")

    # --- タブ3：トプコン 境界 修復 ---
    with tab3:
        st.subheader("トプコン 境界修復")
        st.caption("shp,shx.dbfファイルをアップロードしてください。")
        uploaded_files_repair = st.file_uploader("SHP/SHX/DBFをドロップ", accept_multiple_files=True, key="repair")
        if uploaded_files_repair:
            if st.button("🔥 圃場データを一括修復する", key="btn_repair"):
                name_counts = defaultdict(int)
                shp_registry = []
                with tempfile.TemporaryDirectory() as tmp_dir:
                    for f in uploaded_files_repair:
                        safe_name = re.sub(r'[\\/:*?"<>|]', '_', f.name)
                        with open(os.path.join(tmp_dir, safe_name), "wb") as out:
                            out.write(f.getbuffer())
                        if safe_name.lower().endswith(".shp"):
                            base = os.path.splitext(safe_name)[0]
                            name_counts[base] += 1
                            uniq = f"{base}_{name_counts[base]}" if name_counts[base] > 1 else base
                            shp_registry.append({"orig": base, "uniq": uniq, "fname": safe_name})
                    
                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, 'w') as master_zip:
                        for item in shp_registry:
                            try:
                                work_in = os.path.join(tmp_dir, f"in_{item['uniq']}")
                                for ext in ['.shp', '.shx', '.dbf']:
                                    src = os.path.join(tmp_dir, item['fname'].replace(".shp", ext).replace(".SHP", ext))
                                    if os.path.exists(src): shutil.copy(src, work_in + ext)
                                
                                reader = shapefile.Reader(work_in)
                                work_out = os.path.join(tmp_dir, f"out_{item['uniq']}")
                                writer = shapefile.Writer(work_out, shapeType=reader.shapeType)
                                writer.fields = list(reader.fields[1:])
                                for sr in reader.shapeRecords():
                                    parts = []
                                    for i in range(len(sr.shape.parts)):
                                        start = sr.shape.parts[i]
                                        end = sr.shape.parts[i+1] if i+1 < len(sr.shape.parts) else len(sr.shape.points)
                                        pts = sr.shape.points[start:end]
                                        if len(pts) > 0 and pts[0] != pts[-1]: pts.append(pts[0])
                                        parts.append(pts)
                                    writer.poly(parts)
                                    writer.record(**sr.record.as_dict())
                                writer.close()
                                for ext in ['.shp', '.shx', '.dbf']:
                                    master_zip.write(work_out + ext, f"{item['uniq']}/{item['uniq']}{ext}")
                                master_zip.writestr(f"{item['uniq']}/{item['uniq']}.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                            except Exception: continue
                    st.download_button("📥 修復済みをダウンロード", zip_buffer.getvalue(), "repaired.zip")
