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
from xml.etree import ElementTree as ET


# ==========================================
# Topcon XML圃場データ変換 共通処理
# ※実データで確認済みのTopcon XML構造を対象
# ==========================================
TOPCON_WGS84_PRJ = 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'

def _topcon_local(tag):
    return tag.split("}")[-1]

def _topcon_safe_name(value):
    value = re.sub(r'[\\/:*?"<>|]', '_', str(value or ''))
    value = value.strip().strip(".")
    return value or "_"

def _topcon_point(element):
    """Topcon PNT: C=latitude, D=longitude"""
    try:
        lat = float(element.attrib["C"])
        lon = float(element.attrib["D"])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except Exception:
        pass
    return None

def _topcon_safe_extract(zf, destination):
    """ZIP Slipを避けて展開"""
    root = os.path.abspath(destination)
    for member in zf.infolist():
        target = os.path.abspath(os.path.join(root, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"安全でないZIP内パスを検出しました: {member.filename}")
    zf.extractall(root)

def _topcon_expand_nested_zips(root):
    """ZIP内ZIPも展開。元ZIP自体は出力ZIPにはコピーしない。"""
    processed = set()
    while True:
        targets = []
        for current, _, files in os.walk(root):
            for name in files:
                path = os.path.join(current, name)
                if name.lower().endswith(".zip") and path not in processed:
                    targets.append(path)
        if not targets:
            break
        for path in targets:
            processed.add(path)
            target = path + "_contents"
            try:
                os.makedirs(target, exist_ok=True)
                with zipfile.ZipFile(path, "r") as nested:
                    _topcon_safe_extract(nested, target)
            except Exception:
                # 壊れた/非ZIPのファイルは無視
                continue

def _topcon_dataset_dirs(root):
    """
    PFD*.XMLが存在するディレクトリを1データセットとして扱う。
    Client/Farm IDが別ZIP間で重複しても混線しないようにする。
    """
    dirs = []
    for current, _, files in os.walk(root):
        if any(re.match(r"^PFD.*\.XML$", f, re.I) for f in files):
            dirs.append(current)
    return sorted(set(dirs))

def _topcon_parse_dataset(dataset_dir):
    roots = []
    for name in os.listdir(dataset_dir):
        if not name.lower().endswith(".xml"):
            continue
        path = os.path.join(dataset_dir, name)
        try:
            roots.append((path, ET.parse(path).getroot()))
        except Exception:
            continue

    clients = {}
    farms = {}
    for _, root in roots:
        for element in root.iter():
            tag = _topcon_local(element.tag)
            if tag == "CTR":
                clients[element.attrib.get("A", "")] = element.attrib.get("B", "Client")
            elif tag == "FRM":
                farms[element.attrib.get("A", "")] = element.attrib.get("B", "Farm")

    fields = []
    seen = set()

    for source, root in roots:
        for pfd in root.iter():
            if _topcon_local(pfd.tag) != "PFD":
                continue

            field_id = pfd.attrib.get("A", "")
            field_name = pfd.attrib.get("C") or pfd.attrib.get("B") or field_id or "Field"
            client_id = pfd.attrib.get("E", "")
            farm_id = pfd.attrib.get("F", "")
            client_name = clients.get(client_id, client_id or "Client")
            farm_name = farms.get(farm_id, farm_id or "Farm")

            key = (field_id, field_name, client_id, farm_id)
            if key in seen:
                continue
            seen.add(key)

            boundaries = []
            ablines = []

            # Topcon確認データ:
            # PLN -> LSG A="1" -> PNT = Boundary
            for pln in pfd.iter():
                if _topcon_local(pln.tag) != "PLN":
                    continue
                for lsg in pln.iter():
                    if _topcon_local(lsg.tag) != "LSG" or lsg.attrib.get("A") != "1":
                        continue
                    points = []
                    for pnt in lsg:
                        if _topcon_local(pnt.tag) == "PNT":
                            pt = _topcon_point(pnt)
                            if pt:
                                points.append(pt)
                    if len(points) >= 3:
                        boundaries.append(points)

            # Topcon確認データ:
            # GPN -> LSG A="5" -> PNT A="6"/"7" = A/B line
            for gpn in pfd.iter():
                if _topcon_local(gpn.tag) != "GPN":
                    continue
                guidance_name = gpn.attrib.get("B") or gpn.attrib.get("A") or "ABLine"
                for lsg in gpn.iter():
                    if _topcon_local(lsg.tag) != "LSG" or lsg.attrib.get("A") != "5":
                        continue
                    points = []
                    for pnt in lsg:
                        if _topcon_local(pnt.tag) == "PNT":
                            pt = _topcon_point(pnt)
                            if pt:
                                points.append(pt)
                    if len(points) >= 2:
                        ablines.append({
                            "name": guidance_name,
                            "points": points
                        })

            fields.append({
                "id": field_id,
                "name": field_name,
                "client": client_name,
                "farm": farm_name,
                "boundaries": boundaries,
                "ablines": ablines,
                "source": os.path.basename(source),
            })

    return fields

def _topcon_unique_field_dir(output_root, field):
    parent = os.path.join(
        output_root,
        _topcon_safe_name(field["client"]),
        _topcon_safe_name(field["farm"]),
    )
    os.makedirs(parent, exist_ok=True)

    base = os.path.join(parent, _topcon_safe_name(field["name"]))
    if not os.path.exists(base):
        return base

    suffix = _topcon_safe_name(field["id"] or "duplicate")
    candidate = base + "__" + suffix
    n = 2
    while os.path.exists(candidate):
        candidate = base + "__" + suffix + f"_{n}"
        n += 1
    return candidate

def _topcon_ensure_dirs(base):
    boundary_dir = os.path.join(base, "Boundaries")
    abline_dir = os.path.join(base, "ABlines")
    os.makedirs(boundary_dir, exist_ok=True)
    os.makedirs(abline_dir, exist_ok=True)
    return boundary_dir, abline_dir

def _topcon_clockwise_ring(points):
    """Shapefile外周リングを時計回りに揃える"""
    ring = [[lon, lat] for lat, lon in points]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) >= 4:
        area2 = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            area2 += x1 * y2 - x2 * y1
        if area2 > 0:  # counter-clockwise
            ring = list(reversed(ring))
    return ring

def _topcon_write_shp(base, field):
    boundary_dir, abline_dir = _topcon_ensure_dirs(base)

    for i, points in enumerate(field["boundaries"], 1):
        stem = os.path.join(boundary_dir, f"Boundary_{i:03d}")
        writer = shapefile.Writer(stem, shapeType=shapefile.POLYGON, encoding="utf-8")
        writer.field("NAME", "C", 80)
        writer.field("FIELD_ID", "C", 40)
        writer.poly([_topcon_clockwise_ring(points)])
        writer.record(f"Boundary_{i:03d}", field["id"])
        writer.close()

        with open(stem + ".prj", "w", encoding="ascii") as f:
            f.write(TOPCON_WGS84_PRJ)
        with open(stem + ".cpg", "w", encoding="ascii") as f:
            f.write("UTF-8")

    for i, item in enumerate(field["ablines"], 1):
        points = item["points"]
        stem = os.path.join(abline_dir, f"ABLine_{i:03d}")
        writer = shapefile.Writer(stem, shapeType=shapefile.POLYLINE, encoding="utf-8")
        writer.field("NAME", "C", 80)
        writer.field("SOURCE", "C", 100)
        writer.field("FIELD_ID", "C", 40)
        writer.line([[[lon, lat] for lat, lon in points]])
        writer.record(f"ABLine_{i:03d}", item["name"], field["id"])
        writer.close()

        with open(stem + ".prj", "w", encoding="ascii") as f:
            f.write(TOPCON_WGS84_PRJ)
        with open(stem + ".cpg", "w", encoding="ascii") as f:
            f.write("UTF-8")

def _topcon_write_geojson(base, field):
    boundary_dir, abline_dir = _topcon_ensure_dirs(base)

    boundary_features = []
    for i, points in enumerate(field["boundaries"], 1):
        ring = [[lon, lat] for lat, lon in points]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        boundary_features.append({
            "type": "Feature",
            "properties": {
                "NAME": f"Boundary_{i:03d}",
                "FIELD_ID": field["id"],
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [ring],
            },
        })

    abline_features = []
    for i, item in enumerate(field["ablines"], 1):
        abline_features.append({
            "type": "Feature",
            "properties": {
                "NAME": f"ABLine_{i:03d}",
                "SOURCE": item["name"],
                "FIELD_ID": field["id"],
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in item["points"]],
            },
        })

    with open(os.path.join(boundary_dir, "Boundaries.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": boundary_features},
                  f, ensure_ascii=False, indent=2)

    with open(os.path.join(abline_dir, "ABlines.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": abline_features},
                  f, ensure_ascii=False, indent=2)

def _topcon_write_isoxml_trial(base, field):
    """
    ジオメトリ中心の試験出力。
    実機での完全なISOXML互換性を保証するものではない。
    """
    boundary_dir, abline_dir = _topcon_ensure_dirs(base)

    def make_common():
        root = ET.Element("TASKDATA", {
            "VersionMajor": "4",
            "VersionMinor": "0",
            "Manufacturer": "Topcon Converter",
        })
        ET.SubElement(root, "CTR", {"A": "CTR-1", "B": field["client"]})
        ET.SubElement(root, "FRM", {"A": "FRM-1", "B": field["farm"], "I": "CTR-1"})
        pfd = ET.SubElement(root, "PFD", {
            "A": "PFD-1",
            "C": field["name"],
            "E": "CTR-1",
            "F": "FRM-1",
        })
        return root, pfd

    root, pfd = make_common()
    pln = ET.SubElement(pfd, "PLN", {"A": "1"})
    for points in field["boundaries"]:
        lsg = ET.SubElement(pln, "LSG", {"A": "1", "B": ""})
        for lat, lon in points:
            ET.SubElement(lsg, "PNT", {
                "A": "2",
                "C": f"{lat:.12f}",
                "D": f"{lon:.12f}",
            })
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(
        os.path.join(boundary_dir, "Boundaries.XML"),
        encoding="utf-8",
        xml_declaration=True,
    )

    root, pfd = make_common()
    ggp = ET.SubElement(pfd, "GGP", {"A": "GGP-1", "B": field["name"]})
    for idx, item in enumerate(field["ablines"], 1):
        gpn = ET.SubElement(ggp, "GPN", {
            "A": f"GPN-{idx}",
            "B": item["name"],
            "C": "1",
        })
        lsg = ET.SubElement(gpn, "LSG", {"A": "5"})
        for j, (lat, lon) in enumerate(item["points"]):
            attrs = {
                "A": "6" if j == 0 else ("7" if j == 1 else "2"),
                "C": f"{lat:.12f}",
                "D": f"{lon:.12f}",
            }
            if j == 0:
                attrs["B"] = "A"
            elif j == 1:
                attrs["B"] = "B"
            ET.SubElement(lsg, "PNT", attrs)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(
        os.path.join(abline_dir, "ABlines.XML"),
        encoding="utf-8",
        xml_declaration=True,
    )

def _topcon_process_uploaded_zip(uploaded_file, generated_root, output_format):
    """
    1つのアップロードZIPを解析して generated_root へ出力。
    戻り値: PFD一覧
    """
    with tempfile.TemporaryDirectory() as td:
        input_path = os.path.join(td, _topcon_safe_name(uploaded_file.name))
        with open(input_path, "wb") as f:
            f.write(uploaded_file.getvalue())

        extract_root = os.path.join(td, "extracted")
        os.makedirs(extract_root, exist_ok=True)

        with zipfile.ZipFile(input_path, "r") as zf:
            _topcon_safe_extract(zf, extract_root)

        _topcon_expand_nested_zips(extract_root)

        fields = []
        for dataset_dir in _topcon_dataset_dirs(extract_root):
            fields.extend(_topcon_parse_dataset(dataset_dir))

        if not fields:
            raise ValueError("TopconのPFD（圃場）XMLを検出できませんでした。")

        for field in fields:
            field_dir = _topcon_unique_field_dir(generated_root, field)
            os.makedirs(field_dir, exist_ok=True)

            if output_format == "SHP":
                _topcon_write_shp(field_dir, field)
            elif output_format == "GeoJSON":
                _topcon_write_geojson(field_dir, field)
            else:
                _topcon_write_isoxml_trial(field_dir, field)

            with open(os.path.join(field_dir, "INFO.txt"), "w", encoding="utf-8") as f:
                f.write(
                    f'Client: {field["client"]}\n'
                    f'Farm: {field["farm"]}\n'
                    f'Field: {field["name"]}\n'
                    f'Field ID: {field["id"]}\n'
                    f'Boundaries: {len(field["boundaries"])}\n'
                    f'ABlines: {len(field["ablines"])}\n'
                    f'Source XML: {field["source"]}\n'
                )

        return fields

def _topcon_zip_generated(generated_root):
    """今回生成したファイルだけをZIP化する"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for current, _, files in os.walk(generated_root):
            for name in files:
                path = os.path.join(current, name)
                zf.write(path, os.path.relpath(path, generated_root))
    buffer.seek(0)
    return buffer.getvalue()


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
    tab_xml, tab0, tab1, tab2, tab3 = st.tabs([
        "🗺️ Topcon XML → SHP / ISOXML",
        "🚀 トプコン一括変換",
        "📈 トプコン ABライン変換",
        "📈 トプコン 曲線変換",
        "🔧 トプコン 境界修復",
    ])

    # --- Topcon XML圃場データ変換 ---
    with tab_xml:
        st.subheader("Topcon XML 圃場データ一括変換")
        st.write(
            "TopconからエクスポートしたXML形式のZIPをアップロードしてください。"
            "複数ZIP、複数XML、複数PFD（圃場）、ZIP内ZIPに対応しています。"
        )
        st.caption(
            "XML内の Client / Farm / Field 情報を使用して、"
            "Boundaries と ABlines を圃場ごとに分けて出力します。"
        )

        xml_uploaded = st.file_uploader(
            "Topcon ZIPをアップロード",
            type=["zip"],
            accept_multiple_files=True,
            key="topcon_xml_zip",
        )

        xml_output_format = st.radio(
            "出力形式",
            ["SHP", "GeoJSON", "ISOXML（試験）"],
            horizontal=True,
            key="topcon_xml_format",
        )

        if xml_output_format == "ISOXML（試験）":
            st.warning(
                "ISOXMLは現在ジオメトリ中心の試験出力です。"
                "対象端末での完全な読み込み互換性は未検証です。"
            )

        if xml_uploaded and st.button("🚀 Topcon XML 変換開始", key="btn_topcon_xml"):
            output_format = "ISOXML" if xml_output_format.startswith("ISOXML") else xml_output_format

            total_fields = 0
            total_boundaries = 0
            total_ablines = 0
            errors = []

            progress = st.progress(0)
            status = st.empty()

            with st.spinner("Topcon XMLを解析・変換しています..."):
                with tempfile.TemporaryDirectory(prefix="topcon_generated_") as generated_root:
                    for index, uploaded in enumerate(xml_uploaded, 1):
                        status.write(f"{index}/{len(xml_uploaded)} 処理中: {uploaded.name}")
                        try:
                            fields = _topcon_process_uploaded_zip(
                                uploaded,
                                generated_root,
                                output_format,
                            )
                            total_fields += len(fields)
                            total_boundaries += sum(len(x["boundaries"]) for x in fields)
                            total_ablines += sum(len(x["ablines"]) for x in fields)
                        except Exception as exc:
                            errors.append(f"{uploaded.name}: {exc}")

                        progress.progress(index / len(xml_uploaded))

                    result_zip = _topcon_zip_generated(generated_root)

            status.empty()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("入力ZIP", len(xml_uploaded))
            c2.metric("圃場(PFD)", total_fields)
            c3.metric("境界線", total_boundaries)
            c4.metric("ABライン", total_ablines)

            if total_fields > 0:
                st.success("✅ 変換が完了しました。")
                st.code(
                    "Client / Farm / Field / Boundaries\n"
                    "                      / ABlines",
                    language=None,
                )
                st.download_button(
                    "📥 変換結果をダウンロード",
                    data=result_zip,
                    file_name=f"Converted_{output_format}.zip",
                    mime="application/zip",
                    key="dl_topcon_xml",
                )
            else:
                st.error("変換できるTopcon圃場データが見つかりませんでした。")

            if errors:
                with st.expander(f"⚠️ エラー詳細 ({len(errors)}件)"):
                    for message in errors:
                        st.write(message)


    # --- タブ0：トプコンデータ一括変換 (名称維持版) ---
    with tab0:
        st.subheader("トプコンデータ一括変換 (ライン・境界・曲線すべて)")
        st.caption("ABLines / Boundaries / Curves フォルダを含むZIPをアップロードしてください。元ファイル名をそのまま引き継ぎます。")

        def process_crv_line_integrated(field_root, curves_dir):
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
                                    coords.append((base_lon + (dx * lon_per_m), base_lat + (-dy * lat_per_m)))
                            if len(coords) >= 2:
                                line = LineString(coords)
                                gdf = gpd.GeoDataFrame([{'Name': base_name, 'geometry': line}], crs="EPSG:4326")
                                gdf.to_file(os.path.join(field_root, f"{base_name}.shp"), driver='ESRI Shapefile', encoding='utf-8')
                        except Exception: continue

        def process_ab_line_integrated(field_root, ablines_dir):
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

        def process_boundary_integrated(shp_path, output_dir):
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
                    rec.update({'id': str(sr.record[0]) if sr.record else "1", 'Name': base_name})
                    writer.record(**rec)
                writer.close()
                with open(output_base + ".prj", "w") as f: f.write(prj_data)
            except Exception: pass

        uploaded_zip_topcon_all = st.file_uploader("一括変換用ZIPをアップロード", type="zip", key="topcon_v2")

        if uploaded_zip_topcon_all:
            if st.button("🚀 変換開始", key="btn_v2"):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    extract_path = os.path.join(tmp_dir, "extracted")
                    with zipfile.ZipFile(uploaded_zip_topcon_all, 'r') as z:
                        z.extractall(extract_path)

                    for root, dirs, files in os.walk(extract_path, topdown=False):
                        dirs_lower = [d.lower() for d in dirs]
                        if any(x in dirs_lower for x in ["ablines", "boundaries", "curves"]):
                            field_temp = os.path.join(tmp_dir, "field_out")
                            if os.path.exists(field_temp): shutil.rmtree(field_temp)
                            os.makedirs(field_temp)
                            dir_map = {d.lower(): d for d in dirs}

                            if "ablines" in dir_map:
                                process_ab_line_integrated(field_temp, os.path.join(root, dir_map["ablines"]))
                            if "boundaries" in dir_map:
                                b_dir = os.path.join(root, dir_map["boundaries"])
                                for f in os.listdir(b_dir):
                                    if f.lower().endswith(".shp"):
                                        process_boundary_integrated(os.path.join(b_dir, f), field_temp)
                            if "curves" in dir_map:
                                process_crv_line_integrated(field_temp, os.path.join(root, dir_map["curves"]))

                            for d in dirs: shutil.rmtree(os.path.join(root, d))
                            for f in files: os.remove(os.path.join(root, f))
                            for item in os.listdir(field_temp):
                                shutil.move(os.path.join(field_temp, item), root)

                    final_zip = os.path.join(tmp_dir, "topcon_integrated")
                    shutil.make_archive(final_zip, 'zip', extract_path)
                    with open(final_zip + ".zip", "rb") as f:
                        st.success("✅ 変換が完了しました。")
                        st.download_button("📥 データをダウンロード", f, file_name="topcon_fixed.zip")

    # --- タブ1：トプコン ABライン変換 (単体) ---
    with tab1:
        st.subheader("トプコン ABライン変換 (単体用)")
        u_inis = st.file_uploader("iniファイルをアップロード", type="ini", accept_multiple_files=True)
        if u_inis and st.button("🚀 ABライン変換開始"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf, tempfile.TemporaryDirectory() as td:
                for f in u_inis:
                    try:
                        content = f.read().decode("shift-jis", errors="ignore")
                        config = configparser.ConfigParser()
                        config.read_string(content)
                        p1 = config['APoint'] if 'APoint' in config else config['Point1'] if 'Point1' in config else None
                        p2 = config['BPoint'] if 'BPoint' in config else config['Point2'] if 'Point2' in config else None
                        if p1 and p2:
                            base = os.path.splitext(f.name)[0]
                            line = LineString([(float(p1['Longitude']), float(p1['Latitude'])), (float(p2['Longitude']), float(p2['Latitude']))])
                            gdf = gpd.GeoDataFrame([{'Name': base, 'geometry': line}], crs="EPSG:4326")
                            out = os.path.join(td, base)
                            gdf.to_file(out + ".shp")
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                if os.path.exists(out + ext): zf.write(out + ext, f"{base}/{base}{ext}")
                    except Exception: continue
            st.download_button("📥 ダウンロード", zip_buf.getvalue(), "topcon_abline.zip")

    # --- タブ2：トプコン 曲線変換 (単体) ---
    with tab2:
        st.subheader("トプコン 曲線変換 (単体用)")
        u_crv = st.file_uploader(".crvファイルをアップロード", type=['crv'])
        if u_crv and st.button("🚀 曲線変換開始"):
            try:
                binary = u_crv.read()
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
                    base = os.path.splitext(u_crv.name)[0]
                    gdf = gpd.GeoDataFrame({'Name': [base]}, geometry=[LineString(coords)], crs="EPSG:4326")
                    buf = io.BytesIO()
                    with tempfile.TemporaryDirectory() as td:
                        temp_p = os.path.join(td, base)
                        gdf.to_file(temp_p + ".shp")
                        with zipfile.ZipFile(buf, "w") as zf:
                            for ext in ['.shp', '.shx', '.dbf', '.prj']:
                                if os.path.exists(temp_p + ext): zf.write(temp_p + ext, base + ext)
                    st.download_button("📥 ダウンロード", buf.getvalue(), f"{base}.zip")
            except Exception: st.error("変換失敗")

    # --- タブ3：トプコン 境界修復 (単体) ---
    with tab3:
        st.subheader("トプコン 境界修復")
        u_repair = st.file_uploader("SHP/SHX/DBFをアップロード", accept_multiple_files=True)
        if u_repair and st.button("🚀 境界修復開始"):
            with tempfile.TemporaryDirectory() as td:
                for f in u_repair:
                    with open(os.path.join(td, f.name), "wb") as out: out.write(f.getbuffer())
                
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w') as master_zip:
                    for f_name in os.listdir(td):
                        if f_name.lower().endswith(".shp"):
                            base = os.path.splitext(f_name)[0]
                            try:
                                reader = shapefile.Reader(os.path.join(td, base))
                                out_p = os.path.join(td, "fix_" + base)
                                writer = shapefile.Writer(out_p, shapeType=reader.shapeType)
                                writer.fields = list(reader.fields[1:])
                                for sr in reader.shapeRecords():
                                    parts = []
                                    for i in range(len(sr.shape.parts)):
                                        s = sr.shape.parts[i]
                                        e = sr.shape.parts[i+1] if i+1 < len(sr.shape.parts) else len(sr.shape.points)
                                        pts = sr.shape.points[s:e]
                                        if pts and pts[0] != pts[-1]: pts.append(pts[0])
                                        parts.append(pts)
                                    writer.poly(parts)
                                    writer.record(**sr.record.as_dict())
                                writer.close()
                                for ext in ['.shp', '.shx', '.dbf']:
                                    master_zip.write(out_p + ext, f"{base}/{base}{ext}")
                                master_zip.writestr(f"{base}/{base}.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
                            except Exception: continue
                st.success("✅ 修復完了")
                st.download_button("📥 ダウンロード", zip_buffer.getvalue(), "repaired.zip")
