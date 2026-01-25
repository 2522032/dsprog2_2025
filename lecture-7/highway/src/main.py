from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import flet as ft

# =========================
# Paths
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TRAFFIC_CSV = DATA_DIR / "zkntrf13 (1).csv"   # 時間帯別交通量
MASTER_CSV  = DATA_DIR / "kasyo13 (1).csv"    # 区間マスタ（路線名/起点終点など）
DB_PATH = BASE_DIR / "traffic.db"


# =========================
# Utils
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def connect_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)

def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))

def safe_int(x) -> Optional[int]:
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s == "" or s.lower() == "nan":
        return None
    try:
        return int(float(s))
    except:
        return None

def detect_encoding(p: Path) -> str:
    for enc in ["utf-8-sig", "cp932", "utf-8"]:
        try:
            p.read_text(encoding=enc)
            return enc
        except:
            pass
    return "utf-8"

def pick_column(headers: List[str], candidates: List[str]) -> Optional[str]:
    hs = [norm(x) for x in headers]
    for cand in candidates:
        c = norm(cand)
        if c in hs:
            return headers[hs.index(c)]
    return None

def detect_hour_columns(headers: List[str]) -> Dict[int, str]:
    hour_map: Dict[int, str] = {}
    for h in headers:
        hh = norm(h)
        m = re.search(r"／(\d{1,2})時台$", hh)
        if m:
            hour = int(m.group(1))
            if 0 <= hour <= 23:
                hour_map[hour] = h
                continue
        m2 = re.search(r"(\d{1,2})時台$", hh)
        if m2:
            hour = int(m2.group(1))
            if 0 <= hour <= 23:
                hour_map[hour] = h
    return dict(sorted(hour_map.items()))

def make_key(pref_city_code: Optional[int], unit_section_no: Optional[int]) -> Optional[str]:
    if pref_city_code is None or unit_section_no is None:
        return None
    return f"{pref_city_code}:{unit_section_no}"


# =========================
# DB schema
# =========================
def init_db() -> None:
    con = connect_db()
    cur = con.cursor()

    # 取込ログ
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ingest_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ran_at TEXT NOT NULL,
      traffic_csv TEXT NOT NULL,
      master_csv TEXT,
      note TEXT
    );
    """)

    # 区間（ラベル用）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS segments (
      unit_section_no INTEGER PRIMARY KEY,
      pref_city_code  INTEGER,
      road_type       TEXT,
      route_no        TEXT,
      direction       TEXT,
      route_name      TEXT,
      start_name      TEXT,
      end_name        TEXT,
      municipality_cd TEXT
    );
    """)

    # 時間帯別交通量（ランキング集計用の元データ）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS traffic_hourly (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      unit_section_no INTEGER NOT NULL,
      vehicle_type    TEXT NOT NULL,
      hour            INTEGER NOT NULL,
      volume          INTEGER,
      FOREIGN KEY(unit_section_no) REFERENCES segments(unit_section_no)
    );
    """)

    # ★選択した区間（ブックマーク）だけ保存
    cur.execute("""
    CREATE TABLE IF NOT EXISTS saved_segments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      saved_at TEXT NOT NULL,
      unit_section_no INTEGER NOT NULL,
      label TEXT,
      UNIQUE(unit_section_no)   -- 同じ区間を重複保存しない
    );
    """)

    # ★ランキング保存（条件）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ranking_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      saved_at TEXT NOT NULL,
      vehicle_type TEXT NOT NULL,
      h_from INTEGER NOT NULL,
      h_to INTEGER NOT NULL,
      topn INTEGER NOT NULL
    );
    """)

    # ★ランキング保存（中身）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ranking_items (
      run_id INTEGER NOT NULL,
      rank INTEGER NOT NULL,
      unit_section_no INTEGER NOT NULL,
      total_volume INTEGER NOT NULL,
      label TEXT,
      PRIMARY KEY(run_id, rank),
      FOREIGN KEY(run_id) REFERENCES ranking_runs(id)
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_th_seg ON traffic_hourly(unit_section_no);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_th_hour ON traffic_hourly(hour);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_th_vtype ON traffic_hourly(vehicle_type);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_saved_segments_unit ON saved_segments(unit_section_no);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rank_items_unit ON ranking_items(unit_section_no);")

    con.commit()
    con.close()


# =========================
# MASTER mapping (kasyo)
# =========================
@dataclass
class MasterInfo:
    route_name: str = ""
    start_name: str = ""
    end_name: str = ""
    municipality_cd: str = ""

def load_master_map(master_csv: Path) -> Dict[str, MasterInfo]:
    if not master_csv.exists():
        return {}

    enc = detect_encoding(master_csv)
    text = master_csv.read_text(encoding=enc)
    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames or []
    if not headers:
        return {}

    col_pref = pick_column(headers, ["平成２７年度／道路状況／都道府県指定市コード", "平成27年度／道路状況／都道府県指定市コード"])
    col_unit = pick_column(headers, ["平成２７年度／道路状況／調査単位区間番号", "平成27年度／道路状況／調査単位区間番号"])

    col_route_name = pick_column(headers, ["路線名"])
    col_start = pick_column(headers, ["起点側／路線名等"])
    col_end = pick_column(headers, ["終点側／路線名等"])
    col_muni = pick_column(headers, ["市区町村コード"])

    if not col_pref or not col_unit:
        return {}

    mp: Dict[str, MasterInfo] = {}
    for row in reader:
        pref = safe_int(row.get(col_pref))
        unit = safe_int(row.get(col_unit))
        k = make_key(pref, unit)
        if not k:
            continue

        info = MasterInfo(
            route_name=str(row.get(col_route_name, "")).strip() if col_route_name else "",
            start_name=str(row.get(col_start, "")).strip() if col_start else "",
            end_name=str(row.get(col_end, "")).strip() if col_end else "",
            municipality_cd=str(row.get(col_muni, "")).strip() if col_muni else "",
        )

        if k not in mp:
            mp[k] = info
        else:
            cur = mp[k]
            if cur.route_name == "" and info.route_name != "":
                cur.route_name = info.route_name
            if cur.start_name == "" and info.start_name != "":
                cur.start_name = info.start_name
            if cur.end_name == "" and info.end_name != "":
                cur.end_name = info.end_name
            if cur.municipality_cd == "" and info.municipality_cd != "":
                cur.municipality_cd = info.municipality_cd

    return mp


# =========================
# Import TRAFFIC (zkntrf)
# =========================
def import_zkntrf_csv(traffic_csv: Path, master_map: Dict[str, MasterInfo]) -> Tuple[bool, str]:
    if not traffic_csv.exists():
        return False, f"交通量CSVが見つかりません: {traffic_csv.name}"

    enc = detect_encoding(traffic_csv)
    text = traffic_csv.read_text(encoding=enc)
    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames or []
    if not headers:
        return False, "交通量CSVのヘッダが読めません"

    col_pref_city = pick_column(headers, ["都道府県指定市コード"])
    col_unit_sec  = pick_column(headers, ["交通量調査単位区間番号"])
    col_road_type = pick_column(headers, ["道路種別"])
    col_route_no  = pick_column(headers, ["路線番号"])
    col_dir       = pick_column(headers, ["上り・下りの別"])
    col_vtype     = pick_column(headers, ["車種区分"])

    if not col_pref_city or not col_unit_sec or not col_vtype:
        return False, "必要列（都道府県指定市コード/交通量調査単位区間番号/車種区分）が見つかりません"

    hour_cols = detect_hour_columns(headers)
    if not hour_cols:
        return False, "時間帯列（…／7時台 など）が見つかりません"

    con = connect_db()
    cur = con.cursor()

    # 簡単運用：元データは入れ直し（保存系テーブルは残る）
    cur.execute("DELETE FROM traffic_hourly;")
    cur.execute("DELETE FROM segments;")

    seg_cnt = 0
    cell_cnt = 0

    for row in reader:
        pref = safe_int(row.get(col_pref_city))
        unit = safe_int(row.get(col_unit_sec))
        if pref is None or unit is None:
            continue

        vtype = str(row.get(col_vtype, "")).strip()
        road_type = str(row.get(col_road_type, "")).strip() if col_road_type else ""
        route_no = str(row.get(col_route_no, "")).strip() if col_route_no else ""
        direction = str(row.get(col_dir, "")).strip() if col_dir else ""

        k = make_key(pref, unit)
        mi = master_map.get(k, MasterInfo())

        cur.execute("""
        INSERT INTO segments(
          unit_section_no, pref_city_code, road_type, route_no, direction,
          route_name, start_name, end_name, municipality_cd
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(unit_section_no) DO UPDATE SET
          pref_city_code=excluded.pref_city_code,
          road_type=excluded.road_type,
          route_no=excluded.route_no,
          direction=excluded.direction,
          route_name=COALESCE(NULLIF(segments.route_name,''), excluded.route_name),
          start_name=COALESCE(NULLIF(segments.start_name,''), excluded.start_name),
          end_name=COALESCE(NULLIF(segments.end_name,''), excluded.end_name),
          municipality_cd=COALESCE(NULLIF(segments.municipality_cd,''), excluded.municipality_cd);
        """, (
            unit, pref, road_type, route_no, direction,
            mi.route_name, mi.start_name, mi.end_name, mi.municipality_cd
        ))
        seg_cnt += 1

        for hour, hname in hour_cols.items():
            vol = safe_int(row.get(hname))
            if vol is None:
                continue
            cur.execute("""
            INSERT INTO traffic_hourly(unit_section_no, vehicle_type, hour, volume)
            VALUES(?, ?, ?, ?)
            """, (unit, vtype, hour, vol))
            cell_cnt += 1

    cur.execute(
        "INSERT INTO ingest_runs(ran_at, traffic_csv, master_csv, note) VALUES(?, ?, ?, ?)",
        (now_iso(), str(traffic_csv), str(MASTER_CSV), f"segments(rows)={seg_cnt}, hourly_cells={cell_cnt}")
    )

    con.commit()
    con.close()
    return True, f"取込OK: segments={seg_cnt}, hourly={cell_cnt}"


# =========================
# Queries
# =========================
def list_vehicle_types() -> List[str]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("SELECT DISTINCT vehicle_type FROM traffic_hourly ORDER BY vehicle_type;")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

def list_segments(limit: int = 4000) -> List[Tuple[int, str]]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT unit_section_no,
           COALESCE(NULLIF(route_name,''),'') AS rn,
           COALESCE(NULLIF(start_name,''),'') AS sn,
           COALESCE(NULLIF(end_name,''),'') AS en
    FROM segments
    ORDER BY unit_section_no
    LIMIT ?;
    """, (limit,))
    rows = cur.fetchall()
    con.close()

    out = []
    for unit, rn, sn, en in rows:
        tail = " ".join([x for x in [rn, (sn + "→" + en if (sn or en) else "")] if x])
        label = f"{int(unit)}" + (f"  |  {tail}" if tail else "")
        out.append((int(unit), label))
    return out

def get_segment_label(unit: int) -> str:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT unit_section_no,
           COALESCE(NULLIF(route_name,''),'') AS rn,
           COALESCE(NULLIF(start_name,''),'') AS sn,
           COALESCE(NULLIF(end_name,''),'') AS en
    FROM segments
    WHERE unit_section_no = ?;
    """, (unit,))
    row = cur.fetchone()
    con.close()
    if not row:
        return str(unit)
    _, rn, sn, en = row
    tail = " ".join([x for x in [rn, (sn + "→" + en if (sn or en) else "")] if x])
    return f"{unit}" + (f"  |  {tail}" if tail else "")

def query_top_segments(vehicle_type: str, h1: int, h2: int, topn: int) -> List[Tuple[int, int, str]]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT th.unit_section_no,
           COALESCE(SUM(th.volume), 0) AS total,
           COALESCE(NULLIF(s.route_name,''),'') AS rn,
           COALESCE(NULLIF(s.start_name,''),'') AS sn,
           COALESCE(NULLIF(s.end_name,''),'') AS en
    FROM traffic_hourly th
    LEFT JOIN segments s ON s.unit_section_no = th.unit_section_no
    WHERE th.vehicle_type = ?
      AND th.hour BETWEEN ? AND ?
    GROUP BY th.unit_section_no
    ORDER BY total DESC
    LIMIT ?;
    """, (vehicle_type, h1, h2, topn))
    rows = cur.fetchall()
    con.close()

    out: List[Tuple[int, int, str]] = []
    for unit, total, rn, sn, en in rows:
        tail = " ".join([x for x in [rn, (str(sn) + "→" + str(en) if (sn or en) else "")] if x])
        label = f"{int(unit)}" + (f"  |  {tail}" if tail else "")
        out.append((int(unit), int(total), label))
    return out


# =========================
# Save (DB)
# =========================
def save_segment_bookmark(unit: int) -> Tuple[bool, str]:
    label = get_segment_label(unit)
    con = connect_db()
    cur = con.cursor()
    try:
        cur.execute("""
        INSERT OR IGNORE INTO saved_segments(saved_at, unit_section_no, label)
        VALUES(?, ?, ?)
        """, (now_iso(), unit, label))
        con.commit()

        # 追加されたか確認
        cur.execute("SELECT changes();")
        changed = cur.fetchone()[0]
        con.close()

        if changed == 0:
            return False, "すでに保存済みです（同じ区間）"
        return True, "区間を saved_segments に保存しました"
    except Exception as e:
        con.close()
        return False, f"保存に失敗: {e}"

def list_saved_segments(limit: int = 100) -> List[Tuple[int, str]]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT id, saved_at, label
    FROM saved_segments
    ORDER BY id DESC
    LIMIT ?;
    """, (limit,))
    rows = cur.fetchall()
    con.close()
    out = []
    for sid, saved_at, label in rows:
        out.append((int(sid), f"[{sid}] {saved_at}  |  {label}"))
    return out

def load_saved_segment(sid: int) -> Optional[int]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("SELECT unit_section_no FROM saved_segments WHERE id=?;", (sid,))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else None

def save_ranking(vehicle_type: str, h1: int, h2: int, topn: int, rows: List[Tuple[int, int, str]]) -> Tuple[bool, str]:
    if not rows:
        return False, "ランキングが空なので保存できません"
    con = connect_db()
    cur = con.cursor()
    try:
        cur.execute("""
        INSERT INTO ranking_runs(saved_at, vehicle_type, h_from, h_to, topn)
        VALUES(?, ?, ?, ?, ?)
        """, (now_iso(), vehicle_type, h1, h2, topn))
        run_id = cur.lastrowid

        for i, (unit, total, label) in enumerate(rows, start=1):
            cur.execute("""
            INSERT INTO ranking_items(run_id, rank, unit_section_no, total_volume, label)
            VALUES(?, ?, ?, ?, ?)
            """, (run_id, i, unit, total, label))

        con.commit()
        con.close()
        return True, f"ランキングを保存しました（run_id={run_id}）"
    except Exception as e:
        con.close()
        return False, f"保存に失敗: {e}"

def list_ranking_runs(limit: int = 30) -> List[Tuple[int, str]]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT id, saved_at, vehicle_type, h_from, h_to, topn
    FROM ranking_runs
    ORDER BY id DESC
    LIMIT ?;
    """, (limit,))
    rows = cur.fetchall()
    con.close()
    out = []
    for rid, saved_at, vtype, h1, h2, topn in rows:
        out.append((int(rid), f"[{rid}] {saved_at} | 車種={vtype} | {h1}-{h2} | Top{topn}"))
    return out

def load_ranking_items(run_id: int) -> List[Tuple[int, int, str]]:
    con = connect_db()
    cur = con.cursor()
    cur.execute("""
    SELECT rank, total_volume, label
    FROM ranking_items
    WHERE run_id=?
    ORDER BY rank;
    """, (run_id,))
    rows = cur.fetchall()
    con.close()
    return [(int(r), int(t), str(lbl or "")) for (r, t, lbl) in rows]


# =========================
# Flet UI
# =========================
def main(page: ft.Page):
    init_db()

    page.title = "道路交通センサスR3（東京：ランキング＆区間保存）"
    page.padding = 20
    page.scroll = "auto"

    title = ft.Text("道路交通センサスR3（ローカルCSV → DB → ランキング保存 / 区間保存）", size=18, weight="bold")
    status = ft.Text("", size=12)

    ingest_btn = ft.ElevatedButton("DBに取り込む（zkntrf13 + kasyo13）")

    # --- 区間選択＆保存 ---
    seg_dd = ft.Dropdown(label="区間（番号 | 路線名 | 起点→終点）", width=820)
    save_seg_btn = ft.ElevatedButton("★ この区間を保存")

    saved_seg_dd = ft.Dropdown(label="保存した区間（履歴）", width=980)
    load_seg_btn = ft.ElevatedButton("履歴から区間を選択")

    # --- ランキング ---
    vtype_dd = ft.Dropdown(label="車種区分", width=260)
    h1_tf = ft.TextField(label="hour from", value="7", width=120)
    h2_tf = ft.TextField(label="hour to", value="19", width=120)
    topn_tf = ft.TextField(label="TopN", value="10", width=120)

    rank_btn = ft.ElevatedButton("ランキング更新")
    save_rank_btn = ft.ElevatedButton("★ このランキングを保存")

    rank_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("順位")),
            ft.DataColumn(ft.Text("区間（番号 | 路線名 | 起点→終点）")),
            ft.DataColumn(ft.Text("合計交通量")),
        ],
        rows=[]
    )

    saved_rank_dd = ft.Dropdown(label="保存したランキング（履歴）", width=980)
    load_rank_btn = ft.ElevatedButton("履歴を表示")

    # 内部：直近ランキング結果（保存ボタン用）
    current_rank_rows: List[Tuple[int, int, str]] = []

    def refresh_segments():
        seg_dd.options.clear()
        for unit, label in list_segments(limit=4000):
            seg_dd.options.append(ft.dropdown.Option(key=str(unit), text=label))
        seg_dd.value = seg_dd.options[0].key if seg_dd.options else None

    def refresh_vehicle_types():
        vtype_dd.options.clear()
        for t in list_vehicle_types():
            vtype_dd.options.append(ft.dropdown.Option(key=t, text=t))
        vtype_dd.value = vtype_dd.options[0].key if vtype_dd.options else None

    def refresh_saved_segments():
        saved_seg_dd.options.clear()
        for sid, text in list_saved_segments(limit=100):
            saved_seg_dd.options.append(ft.dropdown.Option(key=str(sid), text=text))
        saved_seg_dd.value = saved_seg_dd.options[0].key if saved_seg_dd.options else None

    def refresh_saved_rankings():
        saved_rank_dd.options.clear()
        for rid, text in list_ranking_runs(limit=30):
            saved_rank_dd.options.append(ft.dropdown.Option(key=str(rid), text=text))
        saved_rank_dd.value = saved_rank_dd.options[0].key if saved_rank_dd.options else None

    def parse_hours() -> Optional[Tuple[int, int]]:
        try:
            h1 = int(h1_tf.value)
            h2 = int(h2_tf.value)
            if h1 > h2:
                h1, h2 = h2, h1
            if not (0 <= h1 <= 23 and 0 <= h2 <= 23):
                return None
            return h1, h2
        except:
            return None

    def parse_topn() -> Optional[int]:
        try:
            n = int(topn_tf.value)
            if n <= 0:
                return None
            return n
        except:
            return None

    def on_ingest(_):
        if not TRAFFIC_CSV.exists():
            status.value = f"❌ {TRAFFIC_CSV.name} が data/ にありません"
            page.update()
            return
        if not MASTER_CSV.exists():
            status.value = f"❌ {MASTER_CSV.name} が data/ にありません"
            page.update()
            return

        master_map = load_master_map(MASTER_CSV)
        ok, msg = import_zkntrf_csv(TRAFFIC_CSV, master_map)
        status.value = ("✅ " if ok else "❌ ") + msg

        if ok:
            refresh_segments()
            refresh_vehicle_types()
            refresh_saved_segments()
            refresh_saved_rankings()

        page.update()

    def on_save_segment(_):
        if not seg_dd.value:
            status.value = "❌ 区間を選んでください"
            page.update()
            return
        unit = int(seg_dd.value)
        ok, msg = save_segment_bookmark(unit)
        status.value = ("✅ " if ok else "⚠️ ") + msg
        refresh_saved_segments()
        page.update()

    def on_load_saved_segment(_):
        if not saved_seg_dd.value:
            status.value = "❌ 保存区間の履歴が空です"
            page.update()
            return
        sid = int(saved_seg_dd.value)
        unit = load_saved_segment(sid)
        if unit is None:
            status.value = "❌ 区間が見つかりません"
            page.update()
            return
        seg_dd.value = str(unit)
        status.value = "✅ 保存区間を選択しました"
        page.update()

    def on_rank(_):
        nonlocal current_rank_rows
        if not vtype_dd.value:
            status.value = "❌ 車種区分を選んでください"
            page.update()
            return

        hs = parse_hours()
        if not hs:
            status.value = "❌ hourは0〜23の数字で入力してください"
            page.update()
            return
        h1, h2 = hs

        topn = parse_topn()
        if topn is None:
            status.value = "❌ TopN は1以上の数字で入力してください"
            page.update()
            return

        vtype = vtype_dd.value
        current_rank_rows = query_top_segments(vtype, h1, h2, topn)

        rank_table.rows.clear()
        if not current_rank_rows:
            status.value = "⚠️ ランキング対象データがありません"
        else:
            status.value = "✅ ランキング更新"
            for i, (unit, total, label) in enumerate(current_rank_rows, start=1):
                rank_table.rows.append(
                    ft.DataRow(cells=[
                        ft.DataCell(ft.Text(str(i))),
                        ft.DataCell(ft.Text(label)),
                        ft.DataCell(ft.Text(str(total))),
                    ])
                )
        page.update()

    def on_save_rank(_):
        if not vtype_dd.value:
            status.value = "❌ 車種区分を選んでください"
            page.update()
            return

        hs = parse_hours()
        if not hs:
            status.value = "❌ hourは0〜23の数字で入力してください"
            page.update()
            return
        h1, h2 = hs

        topn = parse_topn()
        if topn is None:
            status.value = "❌ TopN は1以上の数字で入力してください"
            page.update()
            return

        ok, msg = save_ranking(vtype_dd.value, h1, h2, topn, current_rank_rows)
        status.value = ("✅ " if ok else "❌ ") + msg
        refresh_saved_rankings()
        page.update()

    def on_load_rank(_):
        rid_str = saved_rank_dd.value
        if not rid_str:
            status.value = "❌ 保存ランキングの履歴が空です"
            page.update()
            return
        rid = int(rid_str)
        rows = load_ranking_items(rid)

        rank_table.rows.clear()
        if not rows:
            status.value = "⚠️ このランキングは空です"
        else:
            status.value = "✅ 保存ランキングを表示しました"
            for r, total, label in rows:
                rank_table.rows.append(
                    ft.DataRow(cells=[
                        ft.DataCell(ft.Text(str(r))),
                        ft.DataCell(ft.Text(label)),
                        ft.DataCell(ft.Text(str(total))),
                    ])
                )
        page.update()

    ingest_btn.on_click = on_ingest
    save_seg_btn.on_click = on_save_segment
    load_seg_btn.on_click = on_load_saved_segment
    rank_btn.on_click = on_rank
    save_rank_btn.on_click = on_save_rank
    load_rank_btn.on_click = on_load_rank

    # 起動時（DBがすでにある場合）
    refresh_segments()
    refresh_vehicle_types()
    refresh_saved_segments()
    refresh_saved_rankings()

    page.add(
        title,
        ft.Text("保存するのは「区間ブックマーク」と「ランキング結果」だけ。", size=12),
        ft.Divider(),
        ingest_btn,
        status,
        ft.Divider(),
        ft.Text("区間ブックマーク", size=14, weight="bold"),
        seg_dd,
        ft.Row([save_seg_btn], wrap=True),
        ft.Row([saved_seg_dd, load_seg_btn], wrap=True),
        ft.Divider(),
        ft.Text("ランキング（車種×時間帯の合計交通量 TopN）", size=14, weight="bold"),
        ft.Row([vtype_dd, h1_tf, h2_tf, topn_tf, rank_btn, save_rank_btn], wrap=True),
        rank_table,
        ft.Divider(),
        ft.Text("保存したランキング（履歴）", size=14, weight="bold"),
        ft.Row([saved_rank_dd, load_rank_btn], wrap=True),
    )

ft.app(target=main)
