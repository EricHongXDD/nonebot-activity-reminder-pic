from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional

from playwright.async_api import async_playwright


class ScheduleService:
    """
    活动日程服务：
    - 读取/解析 activities.json
    - 根据当日生成活动日程
    - 产出 HTML 并用 Playwright 渲染为 PNG（文件或字节）
    """

    def __init__(self, activity_file: str = "activities.json") -> None:
        self.activity_file = activity_file
        self.activities: Dict = {}
        if os.path.exists(self.activity_file):
            self.load_activities()

    # ---------- 数据读写 ----------
    def save_activities(self, data: Dict) -> None:
        with open(self.activity_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_activities(self) -> Dict:
        with open(self.activity_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        for activity in data.values():
            activity["start_times"] = [
                datetime.strptime(t, "%H:%M").time() for t in activity["start_times"]
            ]
            activity["duration"] = (
                timedelta(minutes=activity["duration_minutes"])
                if activity["duration_minutes"] is not None
                else None
            )
        self.activities = data
        return self.activities

    # ---------- 业务逻辑 ----------
    def get_day_schedule(self, day: str) -> List[Dict]:
        schedule: List[Dict] = []
        for name, info in self.activities.items():
            if "Everyday" in info["days"] or day in info["days"]:
                schedule.append(
                    {"name": name, "start_times": info["start_times"], "duration": info["duration"]}
                )
        return schedule

    def sort_activities_by_time(self, day_schedule: List[Dict]) -> List[Dict]:
        """
        将多场活动摊平为时间线并排序。
        """
        flat_events: List[Dict] = []
        for activity in day_schedule:
            for start_time in activity["start_times"]:
                end_time = (
                    (datetime.combine(datetime.today(), start_time) + activity["duration"]).time()
                    if activity["duration"]
                    else None
                )
                flat_events.append({"name": activity["name"], "start": start_time, "end": end_time})
        flat_events.sort(key=lambda x: x["start"])
        return flat_events

    # ---------- 出图 ----------
    def generate_schedule_html(self, sorted_events: List[Dict], time_offset: Optional[str] = None) -> str:
        """
        生成页面 HTML；支持 time_offset 如 '+15' / '-30' 用于偏移“当前时间”视觉高亮。
        """
        now = datetime.now()
        if time_offset:
            try:
                direction = time_offset[0]
                minutes = int(time_offset[1:])
                delta = timedelta(minutes=minutes)
                now = now + delta if direction == "+" else now - delta
            except Exception:
                # 简单容错，不阻断出图
                pass

        now_time = now.time()
        weekday_map = {
            "Monday": "星期一",
            "Tuesday": "星期二",
            "Wednesday": "星期三",
            "Thursday": "星期四",
            "Friday": "星期五",
            "Saturday": "星期六",
            "Sunday": "星期日",
        }
        today = datetime.today()
        weekday_cn = weekday_map[today.strftime("%A")]
        today_str = f"{today.strftime('%Y年%m月%d日')} · {weekday_cn}"

        # 两列布局
        midpoint = (len(sorted_events) + 1) // 2
        left_events = sorted_events[:midpoint]
        right_events = sorted_events[midpoint:]

        def render_event_card(event: Dict) -> str:
            start = event["start"]
            end = event["end"]
            name = event["name"]

            now_seconds = now_time.hour * 3600 + now_time.minute * 60 + now_time.second
            start_seconds = start.hour * 3600 + start.minute * 60
            end_seconds = end.hour * 3600 + end.minute * 60 if end else start_seconds + 300
            is_active = start_seconds <= now_seconds <= end_seconds

            all_starts = [e["start"] for e in sorted_events if e["name"] == name]
            is_last_session = start == max(all_starts)

            card_class = "card highlight" if is_active else "card"
            name_style = 'style="color:#ff4d4d;font-weight:bold;"' if is_last_session else ""

            time_str = f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}" if end else start.strftime("%H:%M")

            return f"""
                <div class="{card_class}">
                    <div class="time">{time_str}</div>
                    <div class="name" {name_style}>{name}</div>
                </div>
            """

        html = f"""
        <!DOCTYPE html>
        <html lang="zh">
        <head>
            <meta charset="UTF-8">
            <title>{today_str}</title>
            <style>
                html, body {{
                    margin: 0;
                    padding: 0;
                    background: #0c0c0f;
                    font-family: "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
                    color: #ffffff;
                    overflow-x: hidden;
                }}
                body {{
                    display: flex;
                    justify-content: center;
                    padding: 10px;
                }}
                .page {{
                    width: 640px;
                    background: linear-gradient(135deg, #111 0%, #1a1a1a 100%);
                    padding: 16px;
                    border-radius: 12px;
                    box-shadow: 0 0 10px rgba(0, 255, 255, 0.1);
                    position: relative;
                }}
                .title {{
                    font-size: 20px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 12px;
                    color: #00ccff;
                }}
                .columns {{
                    display: flex;
                    gap: 16px;
                }}
                .column {{
                    flex: 1;
                    display: flex;
                    flex-direction: column;
                }}
                .section-title {{
                    text-align: center;
                    font-weight: bold;
                    font-size: 16px;
                    color: #00bbff;
                    margin-bottom: 8px;
                }}
                .card {{
                    background: #181a1e;
                    border-radius: 8px;
                    margin-bottom: 8px;
                    padding: 10px 14px;
                    box-shadow: 0 0 6px rgba(0, 255, 255, 0.05);
                    transition: transform 0.3s ease;
                }}
                .card:hover {{
                    transform: scale(1.02);
                }}
                .highlight {{
                    background: linear-gradient(90deg, #004080, #0077cc);
                    color: #fff;
                    font-weight: bold;
                }}
                .time {{
                    font-size: 14px;
                    color: #aaa;
                    margin-bottom: 4px;
                }}
                .name {{
                    font-size: 16px;
                    color: #fff;
                }}
                .watermark {{
                    position: absolute;
                    bottom: 8px;
                    right: 12px;
                    font-size: 13px;
                    color: #555;
                    opacity: 0.6;
                }}
            </style>
        </head>
        <body>
            <div class="page">
                <div class="title">{today_str}</div>
                <div class="columns">
                    <div class="column">
                        <div class="section-title">上半日程</div>
                        {''.join([render_event_card(e) for e in left_events])}
                    </div>
                    <div class="column">
                        <div class="section-title">下半日程</div>
                        {''.join([render_event_card(e) for e in right_events])}
                    </div>
                </div>
                <div class="watermark">@Tsubayama/新闻社bot</div>
            </div>
        </body>
        </html>
        """
        return html

    async def render_schedule_png_bytes(
        self, sorted_events: List[Dict], time_offset: Optional[str] = None
    ) -> bytes:
        """
        异步渲染 PNG 字节
        """
        html = self.generate_schedule_html(sorted_events, time_offset=time_offset)
        return await self._render_html_to_png(html, output_file="schedule.png", file=False)

    def render_schedule_to_png(
        self, sorted_events: List[Dict], output_file: str = "schedule.png", file: bool = True, time_offset: Optional[str] = None
    ):
        html = self.generate_schedule_html(sorted_events, time_offset=time_offset)
        return asyncio.run(self._render_html_to_png(html, output_file, file))

    async def _render_html_to_png(self, html_content: str, output_file: str, file: bool):
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()

            with NamedTemporaryFile("w", delete=False, suffix=".html", encoding="utf-8") as f:
                f.write(html_content)
                html_path = f.name

            await page.goto("file://" + html_path)
            await page.wait_for_selector(".page")
            size = await page.evaluate(
                """
                () => {
                    const el = document.querySelector('.page');
                    const rect = el.getBoundingClientRect();
                    return { width: Math.ceil(rect.width), height: Math.ceil(rect.height) };
                }
                """
            )
            await page.set_viewport_size(size)

            if file:
                await page.screenshot(path=output_file, full_page=False)
                await browser.close()
                return None
            else:
                image_bytes = await page.screenshot(type="png", full_page=False)
                await browser.close()
                return image_bytes


if __name__ == "__main__":
    pass
