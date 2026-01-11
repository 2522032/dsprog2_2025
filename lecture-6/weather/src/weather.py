import flet as ft
import requests
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
DB_PATH = (Path(__file__).resolve().parents[2] / "weather.db")  

area_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
weather_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/地域コード.json"

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def make_3day_summary(times, weathers):
    
    
    seen = set()
    days = []
    for t, w in zip(times, weathers):
        d = str(t)[:10]
        if d in seen:
            continue
        seen.add(d)
        days.append((d, str(w)))
        if len(days) == 3:
            break

    while len(days) < 3:
        days.append(("", ""))

    return {
        "d0_date": days[0][0], "d0_weather": days[0][1],
        "d1_date": days[1][0], "d1_weather": days[1][1],
        "d2_date": days[2][0], "d2_weather": days[2][1],
    }

try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    sql_areas = """CREATE TABLE IF NOT EXISTS areas (
      area_code TEXT PRIMARY KEY,
      area_name TEXT NOT NULL
    );"""

    sql_weather = """CREATE TABLE IF NOT EXISTS weather_3days (
      area_code TEXT PRIMARY KEY,
      fetched_at TEXT NOT NULL,

      url TEXT NOT NULL,
      raw_json TEXT NOT NULL,

      d0_date TEXT NOT NULL,
      d0_weather TEXT NOT NULL,

      d1_date TEXT,
      d1_weather TEXT,

      d2_date TEXT,
      d2_weather TEXT,

      FOREIGN KEY(area_code) REFERENCES areas(area_code)
    );"""

    cur.execute(sql_areas)
    cur.execute(sql_weather)
    conn.commit()
    print("テーブル作成OK")
    sql_view = """CREATE VIEW IF NOT EXISTS v_weather AS
    SELECT
        a.area_code,
        a.area_name,
        w.fetched_at,
        w.d0_date, w.d0_weather,
        w.d1_date, w.d1_weather,
        w.d2_date, w.d2_weather
        FROM areas a
        LEFT JOIN weather_3days w
        ON a.area_code = w.area_code;
        """
    cur.execute(sql_view)

except sqlite3.Error as e:
    print(f"エラーが発生しました: {e}")

finally:
    conn.close()


def insert_weather_3days(area_code, url, raw_json, summary):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        fetched_at = now_iso()

        sql = """
        INSERT INTO weather_3days (
          area_code, fetched_at, url, raw_json,
          d0_date, d0_weather,
          d1_date, d1_weather,
          d2_date, d2_weather
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(area_code) DO UPDATE SET
          fetched_at=excluded.fetched_at,
          url=excluded.url,
          raw_json=excluded.raw_json,
          d0_date=excluded.d0_date, d0_weather=excluded.d0_weather,
          d1_date=excluded.d1_date, d1_weather=excluded.d1_weather,
          d2_date=excluded.d2_date, d2_weather=excluded.d2_weather;
        """

        cur.execute(sql, (
            area_code, fetched_at, url, raw_json,
            summary["d0_date"], summary["d0_weather"],
            summary["d1_date"], summary["d1_weather"],
            summary["d2_date"], summary["d2_weather"],
        ))

        conn.commit()
        return True, "INSERT/UPDATE 完了"

    except sqlite3.Error as e:
        return False, f"DBエラー: {e}"

    finally:
        conn.close()

def select_weather_3days(area_code):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        sql = """
        SELECT
          area_code, fetched_at,
          d0_date, d0_weather,
          d1_date, d1_weather,
          d2_date, d2_weather
        FROM weather_3days
        WHERE area_code = ?
        """
        cur.execute(sql, (area_code,))
        row = cur.fetchone()
        return row

    except sqlite3.Error as e:
        print(f"エラーが発生しました: {e}")
        return None

    finally:
        conn.close()

def upsert_area(area_code, area_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO areas(area_code, area_name)
            VALUES(?, ?)
            ON CONFLICT(area_code) DO UPDATE SET area_name=excluded.area_name;
        """, (area_code, area_name))
        conn.commit()
    except sqlite3.Error as e:
        print(f"エリア保存エラー: {e}")
    finally:
        conn.close()

def get_area_data():
    response = requests.get(area_URL, timeout=10)
    response.raise_for_status()
    return response.json()

def get_weather_data(area_code: str):
    url = weather_URL.replace("地域コード", area_code)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()

def main(page: ft.Page):
    page.title = "天気情報取得アプリ"
    page.scroll = "auto"
    page.padding = 20

    header = ft.Text("気象庁のAPIを使った天気予報", size=20, weight="bold")
    info = ft.Text("地域を選択してください", size=14)

    counter = ft.Text("天気情報取得回数: 0", size=14)
    counter.data = 0

    db_status = ft.Text("", size=12)

    weather_output = ft.Column(spacing=6)

    area_data = get_area_data()
    offices = area_data["offices"]

    dropdown = ft.Dropdown(label="地域", width=320)

    for code, info_dict in offices.items():
        dropdown.options.append(ft.dropdown.Option(key=code, text=info_dict["name"]))
        upsert_area(code, info_dict["name"])  

    def on_select(e):
        area_code = dropdown.value
        if not area_code:
            return

        weather_output.controls.clear()
        db_status.value = ""

        try:
            
            url = weather_URL.replace("地域コード", area_code)
            weather_data = get_weather_data(area_code)
            raw_json = json.dumps(weather_data, ensure_ascii=False)

            
            ts_target = None
            for ts in weather_data[0].get("timeSeries", []):
                if ts.get("areas") and "weathers" in ts["areas"][0]:
                    ts_target = ts
                    break

            if ts_target is None:
                weather_output.controls.append(ft.Text("天気情報（weathers）が見つかりませんでした"))
                db_status.value = "DB: 保存なし（weathersなし）"
            else:
                times = ts_target["timeDefines"]
                weathers = ts_target["areas"][0]["weathers"]

                
                for t, w in zip(times, weathers):
                    weather_output.controls.append(ft.Text(f"{t}：{w}"))

              
                summary = make_3day_summary(times, weathers)
                ok, msg = insert_weather_3days(area_code, url, raw_json, summary)
                db_status.value = f"DB: {msg}" if ok else f"DB: {msg}"

                
                row = select_weather_3days(area_code)
                if row:
                    (ac, fetched_at, d0_date, d0_weather, d1_date, d1_weather, d2_date, d2_weather) = row
                    weather_output.controls.append(ft.Divider())
                    weather_output.controls.append(ft.Text("【DB保存内容】"))
                    weather_output.controls.append(ft.Text(f"地域: {ac} / 取得: {fetched_at}"))
                    weather_output.controls.append(ft.Text(f"今日: {d0_date} {d0_weather}"))
                    weather_output.controls.append(ft.Text(f"明日: {d1_date} {d1_weather}"))
                    weather_output.controls.append(ft.Text(f"明後日: {d2_date} {d2_weather}"))

            counter.data += 1
            counter.value = f"天気情報取得回数: {counter.data}"

        except Exception as ex:
            weather_output.controls.append(ft.Text(f"エラー: {ex}"))
            db_status.value = "DB: 保存失敗"

        page.update()

    dropdown.on_change = on_select

    page.add(
        header,
        info,
        dropdown,
        ft.Divider(),
        counter,
        db_status,
        weather_output,
    )

ft.app(target=main)
