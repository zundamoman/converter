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
# 共通ロジック：トプコン解析・変換関数
# ==========================================

def process_crv_binary(binary_data, base_name):
    """バイナリデータを解析してGeoDataFrameを返す"""
    if len(binary_data) < 0x48:
        return None
    try:
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
    except:
        pass
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

def repair_boundary_shp(shp_path_no_ext, output_path_no_ext, base_name):
    """境界SHPを修復(閉じ処理・PRJ付与)して書き出す"""
    prj_wgs84 = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    try:
        reader = shapefile.Reader(shp_path_no_ext)
        writer = shapefile.Writer(output_path_no_ext, shapeType=reader.shapeType)
        writer.fields = list(reader.fields[1:])
        for i, shape_rec in enumerate(reader.shapeRecords()):
            geom = shape_rec.shape
            new_parts = []
            for pi in range(len(geom.parts)):
                si = geom.parts[pi]
                ei = geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points)
                pts = geom.points[si:ei]
                if pts and pts[0] != pts[-1]: pts.append(pts[0])
                new_parts.append(pts)
            writer.poly(new_parts)
            rec = shape_rec.record.as_dict()
            rec.update({'id': str(i+1), 'Name': base_name, 'visibility': 1, 'altitudeMo': "clampToGround"})
            writer.record(**rec)
        writer.close()
        with open(output_path_no_ext + ".prj", "w") as f: f.write(prj_wgs84)
        return True
    except:
        return False

# ==========================================
# UI構成
# ==========================================

st.sidebar.title("🚜 Agri Data Converter")
maker = st.sidebar.radio("メーカーを選択してください", ["DJI", "トプコン"])

st.title(f"{maker} データ変換ツール")

# --- DJI セクション ---
if maker == "DJI":
    tab1, = st.tabs(["🚁 DJI 境界線変換"])
    with tab1:
        st.subheader("DJI 境界線(JSON) → SHP一括変換")
        u_files = st.file_uploader("DJIファイルをアップロード", accept_multiple_files=True, key="dji_up")
        if u_files and st.button("🚀 変換開始"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for uf in u_files:
                    try:
                        content = uf.read().decode("utf-8")
                        match = re.search(r'\{.*\}', content, re.DOTALL)
                        if not match: continue
                        data = json.loads(match.group(0))
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
                            out_p = os.path.join(td, base + ".shp")
                            gdf.to_file(out_p, encoding='utf-8')
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                if os.path.exists(os.path.join(td, base + ext)):
                                    zf.write(os.path.join(td, base + ext), arcname=f"{base}/{base}{ext}")
                    except: continue
            st.download_button("📥 変換データをダウンロード", zip_buf.getvalue(), "dji_converted.zip")

# --- トプコン セクション ---
elif maker == "トプコン":
    t_integrated, t_ab, t_curve, t_repair = st.tabs([
        "🚀 統合一括変換 (ZIP)", 
        "📈 ABライン一括 (.ini)", 
        "📈 曲線一括 (.crv)", 
        "🔧 境界修復一括 (SHP)"
    ])

    # 1. 統合一括変換 (ZIP)
    with t_integrated:
        st.subheader("トプコン統合一括変換")
        st.caption("ABLines/Boundaries/Curvesフォルダを含むZIPを変換・整理します。")
        u_zip = st.file_uploader("ZIPをアップロード", type="zip", key="top_zip")
        if u_zip and st.button("変換とクリーンアップを開始"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                ext_p = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(u_zip, 'r') as z: z.extractall(ext_p)

                for root, dirs, files in os.walk(ext_p, topdown=False):
                    if any(d in dirs for d in ["ABLines", "Boundaries", "Curves"]):
                        work_td = os.path.join(tmp_dir, "work")
                        os.makedirs(work_td, exist_ok=True)

                        # AB Lines
                        d_ab = os.path.join(root, "ABLines")
                        if os.path.exists(d_ab):
                            for f in os.listdir(d_ab):
                                if f.lower().endswith(".ini"):
                                    with open(os.path.join(d_ab, f), 'rb') as fb: raw = fb.read()
                                    for enc in ['utf-8', 'shift-jis', 'utf-16']:
                                        try:
                                            gdf = process_ab_line_ini(raw.decode(enc), os.path.splitext(f)[0])
                                            if gdf is not None: gdf.to_file(os.path.join(work_td, os.path.splitext(f)[0]+".shp"))
                                            break
                                        except: continue
                        # Boundaries
                        d_bn = os.path.join(root, "Boundaries")
                        if os.path.exists(d_bn):
                            for f in os.listdir(d_bn):
                                if f.lower().endswith(".shp"):
                                    repair_boundary_shp(os.path.join(d_bn, os.path.splitext(f)[0]), os.path.join(work_td, os.path.splitext(f)[0]), os.path.splitext(f)[0])
                        # Curves
                        d_cv = os.path.join(root, "Curves")
                        if os.path.exists(d_cv):
                            for f in os.listdir(d_cv):
                                if f.lower().endswith(".crv"):
                                    with open(os.path.join(d_cv, f), 'rb') as fb:
                                        gdf = process_crv_binary(fb.read(), os.path.splitext(f)[0])
                                        if gdf is not None: gdf.to_file(os.path.join(work_td, os.path.splitext(f)[0]+".shp"))

                        # クリーンアップ：サブフォルダを消してSHPをField直下へ
                        for d in ["ABLines", "Boundaries", "Curves"]:
                            if os.path.exists(os.path.join(root, d)): shutil.rmtree(os.path.join(root, d))
                        for item in os.listdir(work_td): shutil.move(os.path.join(work_td, item), root)
                        shutil.rmtree(work_td)

                final_zip = os.path.join(tmp_dir, "output")
                shutil.make_archive(final_zip, 'zip', ext_p)
                with open(final_zip + ".zip", "rb") as f:
                    st.success("✅ 統合変換完了")
                    st.download_button("📥 ダウンロード", f, "topcon_fjd_converted.zip")

    # 2. ABライン一括 (.ini)
    with t_ab:
        st.subheader("ABライン一括変換")
        u_inis = st.file_uploader(".iniファイルを複数選択", type="ini", accept_multiple_files=True)
        if u_inis and st.button("🚀 ABライン一括変換"):
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
                            if os.path.exists(out + ext): zf.write(out + ext, f"{base}/{base}{ext}")
            st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_ab_lines.zip")

    # 3. 曲線一括 (.crv)
    with t_curve:
        st.subheader("曲線一括変換")
        u_crvs = st.file_uploader(".crvファイルを複数選択", type="crv", accept_multiple_files=True)
        if u_crvs and st.button("🚀 曲線一括変換"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_crvs:
                    base = os.path.splitext(f.name)[0]
                    gdf = process_crv_binary(f.read(), base)
                    if gdf is not None:
                        out = os.path.join(td, base)
                        gdf.to_file(out + ".shp")
                        for ext in ['.shp', '.shx', '.dbf', '.prj']:
                            if os.path.exists(out + ext): zf.write(out + ext, f"{base}/{base}{ext}")
            st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_curves.zip")

    # 4. 境界修復一括 (SHP)
    with t_repair:
        st.subheader("境界修復一括処理")
        u_shps = st.file_uploader("SHP/SHX/DBFを複数選択", accept_multiple_files=True)
        if u_shps and st.button("🚀 修復開始"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w') as zf, tempfile.TemporaryDirectory() as td:
                for f in u_shps:
                    with open(os.path.join(td, f.name), "wb") as tmp_f: tmp_f.write(f.getbuffer())
                for f_name in os.listdir(td):
                    if f_name.lower().endswith(".shp"):
                        base = os.path.splitext(f_name)[0]
                        out_p = os.path.join(td, "fixed_" + base)
                        if repair_boundary_shp(os.path.join(td, base), out_p, base):
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                zf.write(out_p + ext, f"{base}/{base}{ext}")
            st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_repaired_boundaries.zip")
