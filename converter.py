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
from collections import defaultdict
from shapely.geometry import shape, Polygon, MultiPolygon, LineString

# --- ページ基本設定 ---
st.set_page_config(page_title="Agri Data Converter", layout="wide")

st.title("🚜 Agri Data Converter")
st.info("ファイルをアップロード後、「実行」ボタンを押すと変換・修復が始まります。")

# ----------------------------------------------------------------
# タブでUIを分割
# ----------------------------------------------------------------
tab1, tab2, tab3, tab4 = st.tabs([
    "🚁 DJI 境界線変換", 
    "🚜 トプコン A-Bライン変換", 
    "🔧 SHP一括修復",
    "📂 トプコンまとめて変換"
])

# ==========================================
# タブ1：DJI 境界線データ → SHP 変換
# ==========================================
with tab1:
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

# ==========================================
# タブ2：トプコン A-Bライン変換
# ==========================================
with tab2:
    st.subheader("トプコンの `.ini` ファイルをアップロードしてください。")
    uploaded_files_topcon = st.file_uploader("iniファイルをドロップ", type="ini", accept_multiple_files=True, key="topcon")

    if uploaded_files_topcon:
        if st.button("🚀 A-Bラインを一括変換する", key="btn_topcon"):
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
                        except Exception:
                            continue

            if success_count > 0:
                st.success(f"✅ {success_count} 件変換完了")
                st.download_button("📥 トプコン SHP保存 (.zip)", zip_buffer.getvalue(), "topcon_converted.zip", key="dl_topcon")
            else:
                st.error("有効な A-B ライン情報が見つかりませんでした。")

# ==========================================
# タブ3：SHP一括修復
# ==========================================
with tab3:
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
                        except Exception:
                            continue

                if success_count > 0:
                    st.success(f"✅ {success_count} 件修復完了")
                    st.download_button("📥 修復済みを保存", zip_buffer.getvalue(), "repaired.zip", key="dl_repair")

# ==========================================
# タブ4：トプコンデータまとめて変換
# ==========================================
with tab4:
    st.subheader("トプコンデータまとめて変換")
    st.caption("cliet/farm/field(.zip)")

    # --- 内部関数定義 ---
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
                        # 一時避難所
                        temp_save = os.path.join(tmp_dir, "temp_shp_only")
                        if os.path.exists(temp_save): shutil.rmtree(temp_save)
                        os.makedirs(temp_save)

                        # ABライン抽出
                        ab_dir = os.path.join(root, "ABLines")
                        if os.path.exists(ab_dir):
                            sub_process_ab_line(temp_save, ab_dir)
                        
                        # 境界修復
                        bound_dir = os.path.join(root, "Boundaries")
                        if os.path.exists(bound_dir):
                            for f in os.listdir(bound_dir):
                                if f.lower().endswith(".shp"):
                                    sub_process_boundary(os.path.join(bound_dir, f), temp_save)

                        # Fieldフォルダを一度空にする
                        for entry in os.listdir(root):
                            entry_path = os.path.join(root, entry)
                            if os.path.isdir(entry_path): shutil.rmtree(entry_path)
                            else: os.remove(entry_path)

                        # 変換済みデータのみ戻す
                        for item in os.listdir(temp_save):
                            shutil.move(os.path.join(temp_save, item), root)
                        
                        shutil.rmtree(temp_save)

                final_zip_name = os.path.join(tmp_dir, "final_output")
                shutil.make_archive(final_zip_name, 'zip', extract_path)
                
                with open(final_zip_name + ".zip", "rb") as f:
                    st.success("✅ 変換完了！不要なフォルダは削除されました。")
                    st.download_button("📥 変換済みデータをダウンロード", f, file_name="topcon_converted_clean.zip")
