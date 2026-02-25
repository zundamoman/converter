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
st.info("ファイルをアップロード後、「変換開始」ボタンを押すと変換・修復が始まります。")

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
            if st.button("🚀 変換開始", key="btn_dji"):
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
                    st.success(f"✅ {success_count} 件の変換が完了しました。")
                    st.download_button("📥 変換データをダウンロード(.zip)", zip_buffer.getvalue(), "dji_converted.zip", key="dl_dji")
                else:
                    st.error("変換可能なポリゴンデータが見つかりませんでした。")

# ==========================================
# トプコン のタブ構成
# ==========================================
elif maker == "トプコン":
    tab0, tab1, tab2, tab3, = st.tabs([
        "🚀 トプコン一括変換",
        "📈 トプコン ABライン変換",
        "📈 トプコン 曲線変換",
        "🔧 トプコン 境界修復",
    ])

    # --- タブ0：トプコンデータ一括変換 (名称維持版) ---
    with tab0:
        st.subheader("トプコンデータ一括変換 (ライン・境界・曲線すべて)")
        st.caption("ABLines / Boundaries / Curves フォルダを含むZIPをアップロードしてください。元ファイル名をそのまま引き継ぎます。")

        def process_crv_line_fjd_style(field_root, curves_dir):
            """Curves内の.crvと同じ名前でSHPを出力"""
            for root, dirs, files in os.walk(curves_dir):
                for f in files:
                    if f.lower().endswith(".crv"):
                        crv_path = os.path.join(root, f)
                        base_name = os.path.splitext(f)[0]
                        try:
                            with open(crv_path, 'rb') as fb:
                                binary_data = fb.read()
                            if len(binary_data) < 0x48: continue
                            
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
                                gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                gdf.to_file(os.path.join(field_root, f"{base_name}.shp"), driver='ESRI Shapefile', encoding='utf-8')
                        except Exception: continue

        def process_ab_line_v2(field_root, ablines_dir):
            """ABラインの.iniと同じ名前でSHPを出力"""
            for root, dirs, files in os.walk(ablines_dir):
                for f in files:
                    if f.lower().endswith(".ini"):
                        ini_path = os.path.join(root, f)
                        base_name = os.path.splitext(f)[0]
                        try:
                            config = configparser.ConfigParser()
                            with open(ini_path, 'rb') as fb:
                                raw_data = fb.read()
                            content = None
                            for enc in ['utf-8', 'utf-16', 'shift-jis']:
                                try: content = raw_data.decode(enc); break
                                except: continue
                            if content:
                                config.read_string(content)
                                p1 = config['APoint'] if 'APoint' in config else config['Point1'] if 'Point1' in config else None
                                p2 = config['BPoint'] if 'BPoint' in config else config['Point2'] if 'Point2' in config else None
                                if p1 and p2:
                                    line = LineString([(float(p1['Longitude']), float(p1['Latitude'])), (float(p2['Longitude']), float(p2['Latitude']))])
                                    gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                    gdf.to_file(os.path.join(field_root, f"{base_name}.shp"), driver='ESRI Shapefile', encoding='utf-8')
                        except Exception: continue

        def process_boundary_v2(shp_path, output_dir):
            """境界SHPを修復し、元の名前を維持して出力"""
            base_name = os.path.splitext(os.path.basename(shp_path))[0]
            output_base = os.path.join(output_dir, base_name)
            prj_data = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
            try:
                reader = shapefile.Reader(os.path.splitext(shp_path)[0])
                writer = shapefile.Writer(output_base, shapeType=reader.shapeType)
                writer.fields = list(reader.fields[1:])
                for sr in reader.shapeRecords():
                    geom = sr.shape
                    new_parts = []
                    for pi in range(len(geom.parts)):
                        si, ei = geom.parts[pi], (geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points))
                        pts = geom.points[si:ei]
                        if pts and pts[0] != pts[-1]: pts.append(pts[0])
                        new_parts.append(pts)
                    writer.poly(new_parts)
                    rec = sr.record.as_dict()
                    rec.update({'id': str(sr.record[0]), 'Name': base_name})
                    writer.record(**rec)
                writer.close()
                with open(output_base + ".prj", "w") as f: f.write(prj_data)
            except Exception: continue

        uploaded_zip_topcon_v2 = st.file_uploader("一括変換用ZIPをアップロード", type="zip", key="topcon_v2")

        if uploaded_zip_topcon_v2:
            if st.button("🚀 変換開始", key="btn_v2"):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    extract_path = os.path.join(tmp_dir, "extracted")
                    with zipfile.ZipFile(uploaded_zip_topcon_v2, 'r') as z:
                        z.extractall(extract_path)

                    for root, dirs, files in os.walk(extract_path, topdown=False):
                        dirs_lower = [d.lower() for d in dirs]
                        if any(x in dirs_lower for x in ["ablines", "boundaries", "curves"]):
                            
                            # フィールドごとに成果物を一時保存する場所（上書き防止）
                            field_temp = os.path.join(tmp_dir, "current_field_out")
                            if os.path.exists(field_temp): shutil.rmtree(field_temp)
                            os.makedirs(field_temp)

                            dir_map = {d.lower(): d for d in dirs}

                            # 1. 直線 (ABLines)
                            if "ablines" in dir_map:
                                process_ab_line_v2(field_temp, os.path.join(root, dir_map["ablines"]))
                            
                            # 2. 境界 (Boundaries)
                            if "boundaries" in dir_map:
                                b_dir = os.path.join(root, dir_map["boundaries"])
                                for f in os.listdir(b_dir):
                                    if f.lower().endswith(".shp"):
                                        process_boundary_v2(os.path.join(b_dir, f), field_temp)

                            # 3. 曲線 (Curves)
                            if "curves" in dir_map:
                                process_crv_line_fjd_style(field_temp, os.path.join(root, dir_map["curves"]))

                            # 既存フォルダとファイルを消去
                            for d in dirs: shutil.rmtree(os.path.join(root, d))
                            for f in files: os.remove(os.path.join(root, f))

                            # 成果物を移動
                            if os.path.exists(field_temp):
                                for item in os.listdir(field_temp):
                                    shutil.move(os.path.join(field_temp, item), root)

                    final_zip_name = os.path.join(tmp_dir, "final_output_v2")
                    shutil.make_archive(final_zip_name, 'zip', extract_path)
                    with open(final_zip_name + ".zip", "rb") as f:
                        st.success("✅ 変換が完了しました。元ファイル名を維持しています。")
                        st.download_button("📥 変換データをダウンロード(.zip)", f, file_name="topcon_integrated_fixed.zip")

    # --- タブ1：トプコン ABライン変換 (単体) ---
    with tab1:
        st.subheader("トプコン ABライン変換 (単体ファイル用)")
        uploaded_files_topcon = st.file_uploader("iniファイルをドロップ", type="ini", accept_multiple_files=True, key="topcon_ab_single")
        if uploaded_files_topcon:
            if st.button("🚀 変換開始", key="btn_topcon_ab_single"):
                zip_buffer = io.BytesIO()
                success_count = 0
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        for uploaded_file in uploaded_files_topcon:
                            try:
                                content = uploaded_file.read().decode("shift-jis", errors="ignore")
                                config = configparser.ConfigParser()
                                config.read_string(content)
                                p1 = config['APoint'] if 'APoint' in config else config['Point1'] if 'Point1' in config else None
                                p2 = config['BPoint'] if 'BPoint' in config else config['Point2'] if 'Point2' in config else None
                                if p1 and p2:
                                    line = LineString([(float(p1['Longitude']), float(p1['Latitude'])), (float(p2['Longitude']), float(p2['Latitude']))])
                                    base_name = os.path.splitext(uploaded_file.name)[0]
                                    gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                    out_path = os.path.join(tmpdir, base_name)
                                    gdf.to_file(out_path + ".shp", driver='ESRI Shapefile', encoding='utf-8')
                                    for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                        if os.path.exists(out_path + ext):
                                            zf.write(out_path + ext, arcname=f"{base_name}/{base_name}{ext}")
                                    success_count += 1
                            except Exception: continue
                if success_count > 0:
                    st.success(f"✅ {success_count}件の変換完了。")
                    st.download_button("📥 ダウンロード", zip_buffer.getvalue(), "topcon_abline.zip")

    # --- タブ2：トプコン 曲線変換 (単体) ---
    with tab2:
        st.subheader("トプコン 曲線変換 (単体ファイル用)")
        u_crv_single = st.file_uploader(".crvファイルをアップロード", type=['crv'], key="fjd_single")
        if u_crv_single:
            if st.button("🚀 変換開始", key="btn_crv_single"):
                binary = u_crv_single.read()
                # 共通ロジック利用
                try:
                    base_lat = struct.unpack('<d', binary[0:8])[0]
                    base_lon = struct.unpack('<d', binary[8:16])[0]
                    coords = []
                    data_section = binary[0x40:]
                    lat_per_m = 1.0 / 111111.0
                    lon_per_m = 1.0 / (111111.0 * np.cos(np.radians(base_lat)))
                    for i in range(0, len(data_section) - 8, 8):
                        dx, dy = struct.unpack('<ff', data_section[i:i+8])
                        if -20000 < dx < 20000:
                            coords.append((base_lon + (dx * lon_per_m), base_lat + (-dy * lat_per_m)))
                    
                    if len(coords) >= 2:
                        base_name = os.path.splitext(u_crv_single.name)[0]
                        line = LineString(coords)
                        gdf = gpd.GeoDataFrame({'Name': [base_name]}, geometry=[line], crs="EPSG:4326")
                        
                        buf = io.BytesIO()
                        with tempfile.TemporaryDirectory() as td:
                            temp_base = os.path.join(td, base_name)
                            gdf.to_file(temp_base + ".shp")
                            with zipfile.ZipFile(buf, "w") as zf:
                                for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                    if os.path.exists(temp_base + ext):
                                        zf.write(temp_base + ext, base_name + ext)
                        buf.seek(0)
                        st.success(f"✅ 変換成功: {base_name}")
                        st.download_button("📥 ダウンロード", buf, file_name=f"{base_name}.zip")
                except Exception:
                    st.error("変換に失敗しました。")

    # --- タブ3：トプコン 境界 修復 (単体) ---
    with tab3:
        st.subheader("トプコン 境界修復")
        uploaded_files_repair = st.file_uploader("SHP/SHX/DBFをドロップ", accept_multiple_files=True, key="repair_v3")
        if uploaded_files_repair:
            if st.button("🚀 変換開始", key="btn_repair_v3"):
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
                                    geom = sr.shape
                                    new_parts = []
                                    for pi in range(len(geom.parts)):
                                        si, ei = geom.parts[pi], (geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points))
                                        pts = geom.points[si:ei]
                                        if pts and pts[0] != pts[-1]: pts.append(pts[0])
                                        new_parts.append(pts)
                                    writer.poly(new_parts)
                                    writer.record(**sr.record.as_dict())
                                writer.close()
                                for ext in ['.shp', '.shx', '.dbf']:
                                    master_zip.write(work_out + ext, f"{item['uniq']}/{item['uniq']}{ext}")
                                master_zip.writestr(f"{item['uniq']}/{item['uniq']}.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                            except Exception: continue
                    
                    st.success("✅ 修復が完了しました。")
                    st.download_button("📥 データをダウンロード(.zip)", zip_buffer.getvalue(), "repaired_topcon.zip")
