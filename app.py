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
from shapely.geometry import shape, Polygon, LineString

# --- ページ基本設定 ---
st.set_page_config(page_title="Agri Data Converter", layout="wide")

# ==========================================
# 共通ロジック：各ファイル形式の変換関数
# ==========================================

def process_crv_to_gdf(binary_data, base_name):
    """バイナリ(.crv)から座標を抽出しGeoDataFrameを返す"""
    if len(binary_data) < 0x48: return None
    try:
        base_lat = struct.unpack('<d', binary_data[0:8])[0]
        base_lon = struct.unpack('<d', binary_data[8:16])[0]
        coords = []
        data_section = binary_data[0x40:]
        lat_per_m = 1.0 / 111111.0
        lon_per_m = 1.0 / (111111.0 * np.cos(np.radians(base_lat)))

        for i in range(0, len(data_section) - 8, 8):
            dx, dy = struct.unpack('<ff', data_section[i:i+8])
            if -50000 < dx < 50000:
                actual_lon = base_lon + (dx * lon_per_m)
                actual_lat = base_lat + (-dy * lat_per_m)
                coords.append((actual_lon, actual_lat))
        if len(coords) >= 2:
            return gpd.GeoDataFrame([{'Name': base_name}], geometry=[LineString(coords)], crs="EPSG:4326")
    except: pass
    return None

def process_ini_to_gdf(content, base_name):
    """INIテキスト(.ini)からABラインのGeoDataFrameを返す"""
    config = configparser.ConfigParser()
    try:
        config.read_string(content)
        p1 = config['APoint'] if 'APoint' in config else config['Point1'] if 'Point1' in config else None
        p2 = config['BPoint'] if 'BPoint' in config else config['Point2'] if 'Point2' in config else None
        
        if p1 and p2:
            lat_a, lon_a = float(p1['Latitude']), float(p1['Longitude'])
            lat_b, lon_b = float(p2['Latitude']), float(p2['Longitude'])
            line = LineString([(lon_a, lat_a), (lon_b, lat_b)])
            return gpd.GeoDataFrame([{'Name': base_name}], geometry=[line], crs="EPSG:4326")
    except: pass
    return None

def repair_shp_file(input_shp_no_ext, output_path_no_ext, base_name):
    """既存の境界SHPを修復して出力"""
    prj_wgs84 = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    try:
        reader = shapefile.Reader(input_shp_no_ext)
        writer = shapefile.Writer(output_path_no_ext, shapeType=reader.shapeType)
        writer.fields = list(reader.fields[1:])
        for i, shape_rec in enumerate(reader.shapeRecords()):
            geom = shape_rec.shape
            parts = []
            for pi in range(len(geom.parts)):
                s, e = geom.parts[pi], (geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points))
                pts = geom.points[s:e]
                if pts and pts[0] != pts[-1]: pts.append(pts[0])
                parts.append(pts)
            writer.poly(parts)
            rec = shape_rec.record.as_dict()
            rec.update({'id': str(i+1), 'Name': base_name})
            writer.record(**rec)
        writer.close()
        with open(output_path_no_ext + ".prj", "w") as f: f.write(prj_wgs84)
        return True
    except: return False

# ==========================================
# メイン UI
# ==========================================

st.sidebar.title("🚜 Agri Data Converter")
maker = st.sidebar.radio("メーカーを選択してください", ["DJI", "トプコン"])

st.title(f"{maker} データ変換ツール")

if maker == "DJI":
    st.subheader("DJI 境界線データ → SHP 変換")
    uploaded_files_dji = st.file_uploader("DJIファイルをアップロード", accept_multiple_files=True, key="dji_up")
    if uploaded_files_dji and st.button("🚀 DJI変換開始"):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf, tempfile.TemporaryDirectory() as tmpdir:
            for uf in uploaded_files_dji:
                try:
                    text_content = uf.read().decode("utf-8")
                    json_match = re.search(r'\{.*\}', text_content, re.DOTALL)
                    if not json_match: continue
                    data = json.loads(json_match.group(0))
                    features = []
                    for feat in data.get("features", []):
                        if "Polygon" in feat.get("geometry", {}).get("type", ""):
                            geom = shape(feat["geometry"])
                            if geom.has_z: geom = Polygon([(p[0], p[1]) for p in geom.exterior.coords])
                            props = {str(k): str(v) for k, v in feat.get("properties", {}).items()}
                            props['geometry'] = geom
                            features.append(props)
                    if features:
                        base = os.path.splitext(uf.name)[0]
                        gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
                        gdf.to_file(os.path.join(tmpdir, base + ".shp"))
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(os.path.join(tmpdir, base + ext)):
                                zf.write(os.path.join(tmpdir, base + ext), f"{base}/{base}{ext}")
                except: continue
        st.download_button("📥 ダウンロード", zip_buffer.getvalue(), "dji_converted.zip")

elif maker == "トプコン":
    t0, t1, t2, t3 = st.tabs(["🚀 統合一括変換", "📈 ABライン変換", "📈 曲線変換", "🔧 境界修復"])

    with t0:
        st.subheader("トプコンデータ一括変換")
        u_zip = st.file_uploader("ZIPをアップロード", type="zip", key="top_integrated")
        if u_zip and st.button("🚀 変換開始"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                ext_path = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(u_zip, 'r') as z: z.extractall(ext_path)
                for root, dirs, files in os.walk(ext_path, topdown=False):
                    dir_map = {d.lower(): d for d in dirs}
                    if any(k in dir_map for k in ["ablines", "boundaries", "curves"]):
                        field_out = os.path.join(tmp_dir, "field_out")
                        if os.path.exists(field_out): shutil.rmtree(field_out)
                        os.makedirs(field_out)
                        # 各処理
                        if "ablines" in dir_map:
                            ab_p = os.path.join(root, dir_map["ablines"])
                            for f in os.listdir(ab_p):
                                if f.lower().endswith(".ini"):
                                    with open(os.path.join(ab_p, f), 'rb') as fb:
                                        for enc in ['shift-jis', 'utf-8']:
                                            try:
                                                gdf = process_ini_to_gdf(fb.read().decode(enc), os.path.splitext(f)[0])
                                                if gdf is not None: gdf.to_file(os.path.join(field_out, f"Line_{os.path.splitext(f)[0]}.shp"))
                                                break
                                            except: continue
                        if "curves" in dir_map:
                            cv_p = os.path.join(root, dir_map["curves"])
                            for f in os.listdir(cv_p):
                                if f.lower().endswith(".crv"):
                                    with open(os.path.join(cv_p, f), 'rb') as fb:
                                        gdf = process_crv_to_gdf(fb.read(), os.path.splitext(f)[0])
                                        if gdf is not None: gdf.to_file(os.path.join(field_out, f"Curve_{os.path.splitext(f)[0]}.shp"))
                        if "boundaries" in dir_map:
                            bn_p = os.path.join(root, dir_map["boundaries"])
                            for f in os.listdir(bn_p):
                                if f.lower().endswith(".shp"):
                                    repair_shp_file(os.path.join(bn_p, os.path.splitext(f)[0]), os.path.join(field_out, f"Bnd_{os.path.splitext(f)[0]}"), os.path.splitext(f)[0])
                        # クリーンアップ
                        if os.listdir(field_out):
                            for item in os.listdir(root):
                                it_p = os.path.join(root, item)
                                if os.path.isdir(it_p): shutil.rmtree(it_p)
                                else: os.remove(it_p)
                            for item in os.listdir(field_out): shutil.move(os.path.join(field_out, item), root)
                final_zip = os.path.join(tmp_dir, "output")
                shutil.make_archive(final_zip, 'zip', ext_path)
                with open(final_zip + ".zip", "rb") as f:
                    st.download_button("📥 ダウンロード", f, "topcon_integrated.zip")

    with t1:
        st.subheader("ABライン単体変換")
        u_inis = st.file_uploader(".iniファイル", type="ini", accept_multiple_files=True)
        if u_inis and st.button("🚀 ABライン変換"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_inis:
                    base = os.path.splitext(f.name)[0]
                    gdf = process_ini_to_gdf(f.read().decode("shift-jis", errors="ignore"), base)
                    if gdf is not None:
                        out = os.path.join(td, base)
                        gdf.to_file(out + ".shp")
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(out + ext): zf.write(out + ext, f"{base}/{base}{ext}")
            st.download_button("📥 保存", zip_buf.getvalue(), "ab_lines.zip")

    with t2:
        st.subheader("曲線単体変換")
        u_crvs = st.file_uploader(".crvファイル", type="crv", accept_multiple_files=True)
        if u_crvs and st.button("🚀 曲線変換"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_crvs:
                    base = os.path.splitext(f.name)[0]
                    gdf = process_crv_to_gdf(f.read(), base)
                    if gdf is not None:
                        out = os.path.join(td, base)
                        gdf.to_file(out + ".shp")
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(out + ext): zf.write(out + ext, f"{base}/{base}{ext}")
            st.download_button("📥 保存", zip_buf.getvalue(), "curves.zip")

    with t3:
        st.subheader("境界SHP修復")
        u_shps = st.file_uploader("SHPファイル一式", accept_multiple_files=True)
        if u_shps and st.button("🚀 境界修復"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w') as zf, tempfile.TemporaryDirectory() as td:
                for f in u_shps:
                    with open(os.path.join(td, f.name), "wb") as tmp_f: tmp_f.write(f.getbuffer())
                for f_name in os.listdir(td):
                    if f_name.lower().endswith(".shp"):
                        base = os.path.splitext(f_name)[0]
                        out_p = os.path.join(td, "fixed_" + base)
                        if repair_shp_file(os.path.join(td, base), out_p, base):
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                if os.path.exists(out_p + ext): zf.write(out_p + ext, f"{base}/{base}{ext}")
            st.download_button("📥 保存", zip_buf.getvalue(), "repaired.zip")
