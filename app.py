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
# サイドバーメニュー構成
# ==========================================
st.sidebar.title("🚜 Agri Data Converter")

# 大項目の選択
category = st.sidebar.radio("メーカー・カテゴリを選択", ["DJI", "トプコン"])

# 小項目の選択
if category == "DJI":
    menu = st.sidebar.selectbox(
        "機能を選択", 
        ["DJI 境界線データ → SHP 変換"]
    )
else:
    menu = st.sidebar.selectbox(
        "機能を選択", 
        [
            "トプコンデータ一括変換 (直線・曲線・境界)",
            "トプコン A-Bライン変換",
            "SHP一括修復",
            "トプコンデータまとめて変換"
        ]
    )

st.title(f"{menu}")
st.info("ファイルをアップロード後、「実行」ボタンを押すと変換・修復が始まります。")

# ==========================================
# 各機能のロジック
# ==========================================

# --- 1. DJI 境界線データ → SHP 変換 ---
if menu == "DJI 境界線データ → SHP 変換":
    st.subheader("DJIの「圃場データ」ファイルをアップロードしてください。")
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

# --- 2. トプコンデータ一括変換 (直線・曲線・境界) ---
elif menu == "トプコンデータ一括変換 (直線・曲線・境界)":
    st.subheader("トプコンデータ一括変換 (直線・曲線・境界)")
    st.caption("client/farm/fieldの中にABLines / Boundaries / Curves フォルダを含むZIPをアップロードしてください")

    def process_crv_line(field_root, curves_dir):
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
                    except Exception as e:
                        st.error(f"❌ Curves変換失敗: {f} - {e}")

    def process_ab_line_v2(field_root, ablines_dir):
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
                            try:
                                content = raw_data.decode(enc); break
                            except: continue
                        if content:
                            config.read_string(content)
                            if 'APoint' in config and 'BPoint' in config:
                                lat_a, lon_a = float(config['APoint']['Latitude']), float(config['APoint']['Longitude'])
                                lat_b, lon_b = float(config['BPoint']['Latitude']), float(config['BPoint']['Longitude'])
                                line = LineString([(lon_a, lat_a), (lon_b, lat_b)])
                                gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                gdf.to_file(os.path.join(field_root, f"{base_name}.shp"), driver='ESRI Shapefile', encoding='utf-8')
                    except Exception as e:
                        st.error(f"❌ ABライン変換失敗: {f} - {e}")

    def process_boundary_v2(shp_path, output_dir):
        base_name = os.path.splitext(os.path.basename(shp_path))[0]
        output_base = os.path.join(output_dir, base_name)
        prj_data = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
        try:
            reader = shapefile.Reader(os.path.splitext(shp_path)[0])
            writer = shapefile.Writer(output_base, shapeType=reader.shapeType)
            writer.fields = list(reader.fields[1:])
            for i, shape_rec in enumerate(reader.shapeRecords()):
                geom = shape_rec.shape
                new_parts = []
                for pi in range(len(geom.parts)):
                    si, ei = geom.parts[pi], (geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points))
                    pts = geom.points[si:ei]
                    if pts and pts[0] != pts[-1]: pts.append(pts[0])
                    new_parts.append(pts)
                writer.poly(new_parts)
                rec = shape_rec.record.as_dict()
                rec.update({'id': str(i+1), 'Name': base_name, 'visibility': 1, 'altitudeMo': "clampToGround"})
                writer.record(**rec)
            writer.close()
            with open(output_base + ".prj", "w") as f: f.write(prj_data)
        except Exception as e:
            st.error(f"❌ 境界修復失敗: {base_name} - {e}")

    uploaded_zip_tab5 = st.file_uploader("ZIPファイルをアップロード", type="zip", key="topcon_v2")

    if uploaded_zip_tab5:
        if st.button("変換とクリーンアップを開始", key="btn_v2"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                extract_path = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(uploaded_zip_tab5, 'r') as z:
                    z.extractall(extract_path)

                for root, dirs, files in os.walk(extract_path, topdown=False):
                    if any(d in dirs for d in ["ABLines", "Boundaries", "Curves"]):
                        temp_save = os.path.join(tmp_dir, "temp_shp_v2")
                        if os.path.exists(temp_save): shutil.rmtree(temp_save)
                        os.makedirs(temp_save)

                        ab_dir = os.path.join(root, "ABLines")
                        if os.path.exists(ab_dir): process_ab_line_v2(temp_save, ab_dir)
                        
                        bound_dir = os.path.join(root, "Boundaries")
                        if os.path.exists(bound_dir):
                            for f in os.listdir(bound_dir):
                                if f.lower().endswith(".shp"):
                                    process_boundary_v2(os.path.join(bound_dir, f), temp_save)

                        curves_dir = os.path.join(root, "Curves")
                        if os.path.exists(curves_dir): process_crv_line(temp_save, curves_dir)

                        for entry in os.listdir(root):
                            entry_path = os.path.join(root, entry)
                            if os.path.isdir(entry_path): shutil.rmtree(entry_path)
                            else: os.remove(entry_path)

                        for item in os.listdir(temp_save):
                            shutil.move(os.path.join(temp_save, item), root)
                        
                        shutil.rmtree(temp_save)

                final_zip_name = os.path.join(tmp_dir, "final_output_v2")
                shutil.make_archive(final_zip_name, 'zip', extract_path)
                with open(final_zip_name + ".zip", "rb") as f:
                    st.success("✅ 変換完了！ABLines, Boundaries, CurvesすべてがSHPに統合されました。")
                    st.download_button("📥 変換済みデータをダウンロード", f, file_name="topcon_v2_converted.zip")

# --- 3. トプコン A-Bライン変換 ---
elif menu == "トプコン A-Bライン変換":
    st.subheader("トプコンの `.ini` ファイルをアップロードしてください。")
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
                                gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                out_path = os.path.join(tmpdir, base_name)
                                gdf.to_file(out_path + ".shp", driver='ESRI Shapefile', encoding='utf-8')
                                for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                    if os.path.exists(out_path + ext):
                                        zf.write(out_path + ext, arcname=f"{base_name}/{base_name}{ext}")
                                success_count += 1
                        except Exception: continue
            if success_count > 0:
                st.success(f"✅ {success_count} 件変換完了")
                st.download_button("📥 トプコン SHP保存 (.zip)", zip_buffer.getvalue(), "topcon_ab_converted.zip")
            else:
                st.error("有効な A-B ライン情報が見つかりませんでした。")

# --- 4. SHP一括修復 ---
elif menu == "SHP一括修復":
    st.subheader("不整合なSHPファイルを物理修復します。")
    uploaded_files_repair = st.file_uploader("SHP/SHX/DBFファイルをまとめてドロップ", accept_multiple_files=True, key="repair")

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
                success_count = 0
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
                            reader.close()
                            for ext in ['.shp', '.shx', '.dbf']:
                                master_zip.write(work_out + ext, f"{item['uniq']}/{item['uniq']}{ext}")
                            master_zip.writestr(f"{item['uniq']}/{item['uniq']}.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                            success_count += 1
                        except Exception: continue
                if success_count > 0:
                    st.success(f"✅ {success_count} 件修復完了")
                    st.download_button("📥 修復済みを保存", zip_buffer.getvalue(), "repaired.zip")

# --- 5. トプコンデータまとめて変換 ---
elif menu == "トプコンデータまとめて変換":
    st.subheader("トプコンデータまとめて変換")
    st.caption("cliet/farm/field(.zip)")

    def sub_process_ab_line(field_root, ablines_dir):
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
                            try:
                                content = raw_data.decode(enc); break
                            except: continue
                        if content:
                            config.read_string(content)
                            if 'APoint' in config and 'BPoint' in config:
                                lat_a, lon_a = float(config['APoint']['Latitude']), float(config['APoint']['Longitude'])
                                lat_b, lon_b = float(config['BPoint']['Latitude']), float(config['BPoint']['Longitude'])
                                line = LineString([(lon_a, lat_a), (lon_b, lat_b)])
                                gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                gdf.to_file(os.path.join(field_root, f"{base_name}.shp"), driver='ESRI Shapefile', encoding='utf-8')
                    except Exception as e:
                        st.error(f"❌ ABライン変換失敗: {f} - {e}")

    def sub_process_boundary(shp_path, output_dir):
        base_name = os.path.splitext(os.path.basename(shp_path))[0]
        output_base = os.path.join(output_dir, base_name)
        prj_data = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
        try:
            reader = shapefile.Reader(os.path.splitext(shp_path)[0])
            writer = shapefile.Writer(output_base, shapeType=reader.shapeType)
            writer.fields = list(reader.fields[1:])
            for i, shape_rec in enumerate(reader.shapeRecords()):
                geom = shape_rec.shape
                new_parts = []
                for pi in range(len(geom.parts)):
                    si, ei = geom.parts[pi], (geom.parts[pi+1] if pi+1 < len(geom.parts) else len(geom.points))
                    pts = geom.points[si:ei]
                    if pts and pts[0] != pts[-1]: pts.append(pts[0])
                    new_parts.append(pts)
                writer.poly(new_parts)
                rec = shape_rec.record.as_dict()
                rec.update({'id': str(i+1), 'Name': base_name, 'visibility': 1, 'altitudeMo': "clampToGround"})
                writer.record(**rec)
            writer.close()
            with open(output_base + ".prj", "w") as f: f.write(prj_data)
        except Exception as e:
            st.error(f"❌ 境界修復失敗: {base_name} - {e}")

    uploaded_zip_topcon_all = st.file_uploader("ZIPファイルをアップロード", type="zip", key="topcon_all")

    if uploaded_zip_topcon_all:
        if st.button("変換とクリーンアップを開始", key="btn_topcon_all"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                extract_path = os.path.join(tmp_dir, "extracted")
                with zipfile.ZipFile(uploaded_zip_topcon_all, 'r') as z:
                    z.extractall(extract_path)
                for root, dirs, files in os.walk(extract_path, topdown=False):
                    if "ABLines" in dirs or "Boundaries" in dirs:
                        temp_save = os.path.join(tmp_dir, "temp_shp_only")
                        if os.path.exists(temp_save): shutil.rmtree(temp_save)
                        os.makedirs(temp_save)
                        ab_dir = os.path.join(root, "ABLines")
                        if os.path.exists(ab_dir): sub_process_ab_line(temp_save, ab_dir)
                        bound_dir = os.path.join(root, "Boundaries")
                        if os.path.exists(bound_dir):
                            for f in os.listdir(bound_dir):
                                if f.lower().endswith(".shp"):
                                    sub_process_boundary(os.path.join(bound_dir, f), temp_save)
                        for entry in os.listdir(root):
                            entry_path = os.path.join(root, entry)
                            if os.path.isdir(entry_path): shutil.rmtree(entry_path)
                            else: os.remove(entry_path)
                        for item in os.listdir(temp_save):
                            shutil.move(os.path.join(temp_save, item), root)
                        shutil.rmtree(temp_save)
                final_zip_name = os.path.join(tmp_dir, "final_output")
                shutil.make_archive(final_zip_name, 'zip', extract_path)
                with open(final_zip_name + ".zip", "rb") as f:
                    st.success("✅ 変換完了！不要なフォルダは削除されました。")
                    st.download_button("📥 変換済みデータをダウンロード", f, file_name="topcon_clean.zip")
