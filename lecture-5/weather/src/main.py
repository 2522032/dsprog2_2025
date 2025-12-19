import flet as ft
import requests


area_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
weather_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/地域コード.json"

def get_area_data(): 
    response = requests.get(area_URL)
    response.raise_for_status()
    return response.json()

def get_weather_data(area_code: str):
    url = weather_URL.replace("地域コード", area_code)
    response = requests.get(url)
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

    weather_output = ft.Column(spacing=6)

    
    area_data = get_area_data()
    offices = area_data["offices"]  
    

    
    dropdown = ft.Dropdown(label="地域", width=320)
    
    for code, info_dict in offices.items():
        dropdown.options.append(ft.dropdown.Option(key=code, text=info_dict["name"]))

    def on_select(e):
        area_code = dropdown.value 
        if not area_code:
            return

        weather_output.controls.clear()

        try:
            weather_data = get_weather_data(area_code)

           
            ts_target = None
            for ts in weather_data[0].get("timeSeries", []):
                if ts.get("areas") and "weathers" in ts["areas"][0]:
                    ts_target = ts
                    break

            if ts_target is None:
                weather_output.controls.append(ft.Text("天気情報（weathers）が見つかりませんでした"))
            else:
                times = ts_target["timeDefines"]
                weathers = ts_target["areas"][0]["weathers"]
                for t, w in zip(times, weathers):
                    weather_output.controls.append(ft.Text(f"{t}：{w}"))

            
            counter.data += 1
            counter.value = f"天気情報取得回数: {counter.data}"

        except Exception as ex:
            weather_output.controls.append(ft.Text(f"エラー: {ex}"))

        page.update()

    dropdown.on_change = on_select

   
    page.add(
        header,
        info,
        dropdown,
        ft.Divider(),
        counter,
        weather_output,
    )

ft.app(target=main)
