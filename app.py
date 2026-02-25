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
import shapefile
import struct
import numpy as np
from collections import defaultdict
from shapely.geometry import shape, Polygon, MultiPolygon, LineString

# --- ページ基本設定 ---
st.set_page_config(page_title="Agri Data Converter", layout="wide")

# ==========================================
# 共通ヘルパー関数
# ==========================================

def process_crv_binary(binary_data, base_name):
    """
    バイナリデータから座標を抽出し、GeoDataFrameを返す
    座標計算式:
    $actual\_lat = base\_lat + (-dy \times \frac{1}{111111.0})$
    $actual\_lon = base\_lon + (dx \times \frac{1}{111111.0 \times \cos(\text{rad}(base\_lat))})$
    """
    if len(binary_data) < 0x48:
        return None

    # ヘッダから基準座標取得
    base_lat = struct.unpack('<d', binary_data[0:8])[0]
    base_lon = struct.unpack('<d', binary_data[8:16])[0]

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

    if len(coords) >= 2:
        line = LineString(coords)
        return gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
    return None

def process_ab_line_ini(content, base_name):
    """INIテキストからABラインのGeoDataFrameを返す"""
    config = configparser.ConfigParser()
    try:
        config.read_string(content)
        if 'APoint' in config and 'BPoint' in config:
            lat_a, lon_a = float(config['APoint']['Latitude']), float(config['APoint']['Longitude'])
            lat_b, lon_b = float(config['BPoint']['Latitude']), float(config['BPoint']['Longitude'])
            line = LineString([(lon_a, lat_a), (lon_b, lat_b)])
            return gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
    except:
        pass
    return None

# ==========================================
# サイドバー：メーカー選択
# ==========================================
st.sidebar.title("🚜 Agri Data Converter")
maker = st.sidebar.radio("メーカーを選択してください", ["DJI", "トプコン"])

st.title(f"{maker} データ変換ツール")

# ==========================================
# DJI セクション
# ==========================================
if maker == "DJI":
    tab1, = st.tabs(["🚁 DJI 境界線変換"])
    with tab1:
        st.subheader("DJI 境界線データ → SHP 変換")
        uploaded_files_dji = st.file_uploader("DJIファイルをアップロード (複数可)", accept_multiple_files=True, key="dji")

        if uploaded_files_dji and st.button("🚀 変換開始", key="btn_dji"):
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
                        except: continue
            if success_count > 0:
                st.success(f"✅ {success_count} 件完了")
                st.download_button("📥 ダウンロード", zip_buffer.getvalue(), "dji_converted.zip")

# ==========================================
# トプコン セクション
# ==========================================
elif maker == "トプコン":
    tab1, tab2, tab3, tab4 = st.tabs([
        "🚀 統合一括変換 (ZIP)",
        "📈 ABライン変換 (.ini)",
        "📈 曲線変換 (.crv)",
        "🔧 境界修復 (SHP)",
    ])

    # --- 1. 統合一括変換 ---
    with tab1:
        st.subheader("トプコン統合一括変換")
        st.caption("ABLines/Boundaries/Curvesフォルダを含むZIPをアップロードしてください。")
        u_zip = st.file_uploader("ZIPファイルをアップロード", type="zip", key="top_integrated")
        if u_zip and st.button("変換とクリーンアップを開始"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                extract_path = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(u_zip, 'r') as z:
                    z.extractall(extract_path)

                for root, dirs, files in os.walk(extract_path, topdown=False):
                    if any(d in dirs for d in ["ABLines", "Boundaries", "Curves"]):
                        temp_save = os.path.join(tmp_dir, "temp_shp")
                        os.makedirs(temp_save, exist_ok=True)

                        # AB Lines
                        ab_dir = os.path.join(root, "ABLines")
                        if os.path.exists(ab_dir):
                            for f in os.listdir(ab_dir):
                                if f.lower().endswith(".ini"):
                                    with open(os.path.join(ab_dir, f), 'rb') as fb:
                                        raw = fb.read()
                                    for enc in ['utf-8', 'shift-jis', 'utf-16']:
                                        try:
                                            gdf = process_ab_line_ini(raw.decode(enc), os.path.splitext(f)[0])
                                            if gdf is not None:
                                                gdf.to_file(os.path.join(temp_save, os.path.splitext(f)[0]+".shp"))
                                            break
                                        except: continue

                        # Boundaries
                        bn_dir = os.path.join(root, "Boundaries")
                        if os.path.exists(bn_dir):
                            for f in os.listdir(bn_dir):
                                if f.lower().endswith(".shp"):
                                    # 既存の境界修復ロジックを流用（簡略化）
                                    base = os.path.splitext(f)[0]
                                    try:
                                        reader = shapefile.Reader(os.path.join(bn_dir, base))
                                        writer = shapefile.Writer(os.path.join(temp_save, base), shapeType=reader.shapeType)
                                        writer.fields = list(reader.fields[1:])
                                        for sr in reader.shapeRecords():
                                            parts = []
                                            for i in range(len(sr.shape.parts)):
                                                s, e = sr.shape.parts[i], (sr.shape.parts[i+1] if i+1 < len(sr.shape.parts) else len(sr.shape.points))
                                                pts = sr.shape.points[s:e]
                                                if pts and pts[0] != pts[-1]: pts.append(pts[0])
                                                parts.append(pts)
                                            writer.poly(parts)
                                            writer.record(**sr.record.as_dict())
                                        writer.close()
                                        with open(os.path.join(temp_save, base+".prj"), "w") as pf:
                                            pf.write('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                                    except: pass

                        # Curves
                        cv_dir = os.path.join(root, "Curves")
                        if os.path.exists(cv_dir):
                            for f in os.listdir(cv_dir):
                                if f.lower().endswith(".crv"):
                                    with open(os.path.join(cv_dir, f), 'rb') as fb:
                                        gdf = process_crv_binary(fb.read(), os.path.splitext(f)[0])
                                        if gdf is not None:
                                            gdf.to_file(os.path.join(temp_save, os.path.splitext(f)[0]+".shp"))

                        # クリーンアップと配置
                        for d in ["ABLines", "Boundaries", "Curves"]:
                            target = os.path.join(root, d)
                            if os.path.exists(target): shutil.rmtree(target)
                        
                        for item in os.listdir(temp_save):
                            shutil.move(os.path.join(temp_save, item), root)
                        shutil.rmtree(temp_save)

                final_zip = os.path.join(tmp_dir, "topcon_fjd_output")
                shutil.make_archive(final_zip, 'zip', extract_path)
                with open(final_zip + ".zip", "rb") as f:
                    st.success("✅ 統合変換完了")
                    st.download_button("📥 ダウンロード", f, "topcon_integrated.zip")

    # --- 2. ABライン変換 (複数INI) ---
    with tab2:
        st.subheader("ABライン一括変換")
        u_inis = st.file_uploader("iniファイルをアップロード (複数可)", type="ini", accept_multiple_files=True)
        if u_inis and st.button("🚀 ABライン変換開始"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_inis:
                    base = os.path.splitext(f.name)[0]
                    content = f.read().decode("shift-jis", errors="ignore")
                    gdf = process_ab_line_ini(content, base)
                    if gdf is not None:
                        out = os.path.join(td, base)
                        gdf.to_file(out + ".shp")
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(out + ext):
                                zf.write(out + ext, f"{base}/{base}{ext}")
                st.success("✅ 変換完了")
                st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_ablines.zip")

    # --- 3. 曲線変換 (複数CRV) ---
    with tab3:
        st.subheader("曲線一括変換")
        u_crvs = st.file_uploader("crvファイルをアップロード (複数可)", type="crv", accept_multiple_files=True)
        if u_crvs and st.button("🚀 曲線変換開始"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_crvs:
                    base = os.path.splitext(f.name)[0]
                    gdf = process_crv_binary(f.read(), base)
                    if gdf is not None:
                        out = os.path.join(td, base)
                        gdf.to_file(out + ".shp")
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(out + ext):
                                zf.write(out + ext, f"{base}/{base}{ext}")
                st.success("✅ 変換完了")
                st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_curves.zip")

    # --- 4. 境界修復 (複数SHPセット) ---
    with tab4:
        st.subheader("境界修復一括処理")
        u_shps = st.file_uploader("SHP/SHX/DBFをアップロード (複数可)", accept_multiple_files=True)
        if u_shps and st.button("🚀 修復開始"):
            # 内部処理は既存のロジックと同様に複数ファイルを一時フォルダに保存してから処理
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w') as master_zip, tempfile.TemporaryDirectory() as td:
                for f in u_shps:
                    with open(os.path.join(td, f.name), "wb") as tmp_f:
                        tmp_f.write(f.getbuffer())
                
                for f_name in os.listdir(td):
                    if f_name.lower().endswith(".shp"):
                        base = os.path.splitext(f_name)[0]
                        try:
                            reader = shapefile.Reader(os.path.join(td, base))
                            out_path = os.path.join(td, "fixed_" + base)
                            writer = shapefile.Writer(out_path, shapeType=reader.shapeType)
                            writer.fields = list(reader.fields[1:])
                            for sr in reader.shapeRecords():
                                parts = [[(p[0], p[1]) for p in sr.shape.points]] # 簡易化
                                if parts[0][0] != parts[0][-1]: parts[0].append(parts[0][0])
                                writer.poly(parts)
                                writer.record(**sr.record.as_dict())
                            writer.close()
                            for ext in ['.shp', '.shx', '.dbf']:
                                master_zip.write(out_path + ext, f"{base}/{base}{ext}")
                            master_zip.writestr(f"{base}/{base}.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                        except: continue
                st.success("✅ 修復完了")
                st.download_button("📥 ダウンロード", zip_buf.getvalue(), "repaired_boundaries.zip")
