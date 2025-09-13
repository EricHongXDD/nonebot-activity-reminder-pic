from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Dict, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nonebot import get_driver, on_regex
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.log import logger
from nonebot.exception import FinishedException
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .service import ScheduleService

__plugin_meta__ = PluginMetadata(
    name="活动安排与提醒",
    description="查询今日活动并在活动开始前10分钟提醒（仅保留：@bot 活动、@bot 活动提醒 开/关）",
    usage="@bot 活动\n@bot 活动提醒 开|关",
)

# ---------- 常量与全局状态 ----------
PLUGIN_DIR: Path = Path(__file__).resolve().parent
ACTIVITIES_FILE: Path = PLUGIN_DIR / "activities.json"
CONFIG_FILE: Path = PLUGIN_DIR / "reminder_config.json"

# {group_id: {"event_reminder": {"enabled": bool}}}
reminder_configs: Dict[str, Dict[str, bool]] = {}
# {group_id: [job_id, ...]} （包含当日各提醒任务与每日重置任务）
scheduled_jobs: Dict[str, List[str]] = {}

scheduler = AsyncIOScheduler()
driver = get_driver()

# 活动服务
service = ScheduleService(str(ACTIVITIES_FILE))
logger.info(f"活动数据加载完成，共 {len(service.activities)} 个活动")

# ---------- 配置读写（兼容旧结构） ----------
def load_config() -> None:
    """
    从文件加载配置
    """
    global reminder_configs
    if not CONFIG_FILE.exists():
        reminder_configs = {}
        logger.info("未检测到配置文件，使用空配置。")
        return

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        normalized: Dict[str, Dict[str, bool]] = {}
        for gid, cfg in data.items():
            enabled = False
            if isinstance(cfg, dict):
                # 兼容旧结构 {"scheduled_send": {...}, "event_reminder": {"enabled": bool}}
                if "event_reminder" in cfg and isinstance(cfg["event_reminder"], dict):
                    enabled = bool(cfg["event_reminder"].get("enabled", False))
                # 兼容更旧/非标准结构 {"enabled": true}
                elif "enabled" in cfg:
                    enabled = bool(cfg.get("enabled", False))
            normalized[str(gid)] = {"event_reminder": {"enabled": enabled}}
        reminder_configs = normalized
        logger.info(f"已加载配置，群组数量：{len(reminder_configs)}")
    except Exception as e:
        logger.exception(f"加载配置失败：{e}")
        reminder_configs = {}


def save_config() -> None:
    """
    配置保存
    """
    try:
        CONFIG_FILE.write_text(
            json.dumps(reminder_configs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug("配置已保存。")
    except Exception as e:
        logger.exception(f"保存配置失败：{e}")


# ---------- 工具函数 ----------
async def generate_activity_image_bytes(time_offset: str | None = None) -> bytes:
    """
    生成“今日活动”图片的二进制数据。
    """
    today_name = datetime.today().strftime("%A")
    day_schedule = service.get_day_schedule(today_name)
    sorted_events = service.sort_activities_by_time(day_schedule)

    if not sorted_events:
        logger.info("今日无活动安排，生成占位图片。")
        sorted_events = [
            {"name": "今日无活动安排", "start": datetime.now().time(), "end": None}
        ]

    return await service.render_schedule_png_bytes(
        sorted_events, time_offset=time_offset
    )


# ---------- 指令 ----------
activity_cmd = on_regex(
    r"^活动$",
    rule=to_me(),
    priority=5,
    block=True,
)

event_reminder_cmd = on_regex(
    r"^活动提醒\s+(开|关)$",
    rule=to_me(),
    priority=5,
    block=True,
    permission=GROUP_ADMIN | GROUP_OWNER,  # 仅群主/管理员可开关
)


@activity_cmd.handle()
async def handle_activity_query() -> None:
    """
    处理 @bot 活动
    """
    try:
        image_data = await generate_activity_image_bytes()
        await activity_cmd.finish(Message([MessageSegment.image(BytesIO(image_data))]))
    except FinishedException:
        # 正常结束，向上传递让 NoneBot 处理，不要当作异常
        raise
    except Exception as e:
        logger.exception(f"获取活动信息失败：{e}")
        # 这里的 finish 仍会抛 FinishedException，但不再被本函数捕获
        await activity_cmd.finish("获取活动信息失败，请稍后再试。")


@event_reminder_cmd.handle()
async def handle_event_reminder_setting(
    event: GroupMessageEvent, reg_group: tuple = RegexGroup()
) -> None:
    """
    处理 @bot 活动提醒 开|关
    """
    try:
        action = (reg_group[0] or "").strip()
        group_id = str(event.group_id)

        # 初始化群组配置
        if group_id not in reminder_configs:
            reminder_configs[group_id] = {"event_reminder": {"enabled": False}}

        if action == "开":
            reminder_configs[group_id]["event_reminder"]["enabled"] = True
            save_config()

            # 清理旧任务记录
            if group_id in scheduled_jobs:
                for job_id in scheduled_jobs[group_id]:
                    try:
                        scheduler.remove_job(job_id)
                    except Exception:
                        pass
                scheduled_jobs[group_id].clear()
            else:
                scheduled_jobs[group_id] = []

            # 创建当日提醒任务 + 每日重置任务
            created_jobs = await create_event_reminder_jobs(group_id)
            reset_job_id = create_daily_reset_job(group_id)

            scheduled_jobs[group_id].extend(created_jobs)
            scheduled_jobs[group_id].append(reset_job_id)

            await event_reminder_cmd.finish(
                f"活动提醒已开启，将在每个活动开始前 10 分钟发送提醒（已创建 {len(created_jobs)} 个提醒任务）。"
            )

        elif action == "关":
            reminder_configs[group_id]["event_reminder"]["enabled"] = False
            save_config()

            # 取消全部任务
            if group_id in scheduled_jobs:
                for job_id in scheduled_jobs[group_id]:
                    try:
                        scheduler.remove_job(job_id)
                    except Exception:
                        pass
                scheduled_jobs[group_id] = []

            await event_reminder_cmd.finish("活动提醒已关闭。")

        else:
            await event_reminder_cmd.finish("参数错误，用法：@bot 活动提醒 开|关")

    except FinishedException:
        # matcher.finish() 的正常结束流程，不要吞掉
        raise
    except Exception as e:
        logger.exception(f"设置活动提醒失败：{e}")
        await event_reminder_cmd.finish("设置失败，请稍后再试。")


# ---------- 调度任务 ----------
async def send_event_reminder(bot: Bot, group_id: str, activities: list) -> None:
    """
    发送“活动开始前10分钟”提醒（附活动图）
    """
    try:
        # 若已关闭提醒则跳过
        if (
            group_id not in reminder_configs
            or not reminder_configs[group_id]["event_reminder"].get("enabled", False)
        ):
            return

        # 以 '+11' 偏移，出一张“开始后约1分钟”的图，便于视觉高亮
        image_data = await generate_activity_image_bytes(time_offset="+11")

        if len(activities) == 1:
            act = activities[0]
            reminder_text = (
                f"活动提醒：{act['name']} 将在 10 分钟后（{act['start_time'].strftime('%H:%M')}）开始！"
            )
        else:
            start_time = activities[0]["start_time"]
            names = "\n".join(f"• {a['name']}" for a in activities)
            reminder_text = (
                f"活动提醒：以下活动将在 10 分钟后（{start_time.strftime('%H:%M')}）开始：\n{names}"
            )

        await bot.send_group_msg(
            group_id=int(group_id),
            message=Message([MessageSegment.text(reminder_text), MessageSegment.image(BytesIO(image_data))]),
        )
        logger.info(f"群 {group_id} 提醒已发送：{', '.join(a['name'] for a in activities)}")
    except Exception as e:
        logger.exception(
            f"发送活动提醒失败（群 {group_id}，活动：{', '.join(a['name'] for a in activities)}）：{e}"
        )


async def create_event_reminder_jobs(group_id: str) -> List[str]:
    """
    为“今天”的所有活动创建“提前 10 分钟”的提醒任务。
    同一时间多个活动合并发送一次消息。
    """
    job_ids: List[str] = []

    today_name = datetime.today().strftime("%A")
    day_schedule = service.get_day_schedule(today_name)

    # {HHMM: {"reminder_datetime": dt, "activities": [{name, start_time}]}}
    reminder_groups: Dict[str, Dict] = {}

    for activity in day_schedule:
        for start_time in activity["start_times"]:
            start_dt = datetime.combine(datetime.today(), start_time)
            reminder_dt = start_dt - timedelta(minutes=10)
            if reminder_dt <= datetime.now():
                continue

            key = reminder_dt.strftime("%H%M")
            group = reminder_groups.setdefault(
                key, {"reminder_datetime": reminder_dt, "activities": []}
            )
            group["activities"].append({"name": activity["name"], "start_time": start_time})

    from nonebot import get_bots  # 避免循环导入
    bots = get_bots()
    if not bots:
        logger.warning("当前无可用 Bot 连接，提醒任务创建将推迟到 on_bot_connect 时恢复。")
        return job_ids

    # 取任意一个 bot 实例用于发送（多 bot 可自行扩展）
    bot = next(iter(bots.values()))

    for key, group in reminder_groups.items():
        job_id = f"event_{group_id}_{key}_{uuid.uuid4().hex[:6]}"

        scheduler.add_job(
            send_event_reminder,
            "date",
            run_date=group["reminder_datetime"],
            args=[bot, group_id, group["activities"]],
            id=job_id,
            max_instances=1,
        )
        job_ids.append(job_id)

    logger.info(f"群 {group_id} 当日提醒任务创建完成：{len(job_ids)} 个")
    return job_ids


def create_daily_reset_job(group_id: str) -> str:
    """
    创建“每日 00:01 重置当天提醒任务”的 cron 任务。
    """
    job_id = f"daily_reset_{group_id}_{uuid.uuid4().hex[:6]}"

    async def _reset():
        # 若已关闭提醒则跳过
        if (
            group_id not in reminder_configs
            or not reminder_configs[group_id]["event_reminder"].get("enabled", False)
        ):
            return

        # 清掉除重置任务外的当日提醒任务
        if group_id in scheduled_jobs:
            for jid in list(scheduled_jobs[group_id]):
                if not jid.startswith("daily_reset_"):
                    try:
                        scheduler.remove_job(jid)
                    except Exception:
                        pass
                    try:
                        scheduled_jobs[group_id].remove(jid)
                    except ValueError:
                        pass

        # 重建当日提醒任务
        new_jobs = await create_event_reminder_jobs(group_id)
        scheduled_jobs[group_id].extend(new_jobs)
        logger.info(f"群 {group_id} 已刷新今日提醒任务：{len(new_jobs)} 个")

    scheduler.add_job(
        _reset,
        "cron",
        hour=0,
        minute=1,  # 每天 00:01
        id=job_id,
        max_instances=1,
        coalesce=True,
    )
    return job_id


# ---------- 生命周期 ----------
@driver.on_startup
async def _on_startup() -> None:
    load_config()
    if not scheduler.running:
        scheduler.start()
        logger.info("活动提醒调度器已启动。")


@driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    """
    Bot 连接后，根据配置恢复“活动提醒”任务与每日重置。
    """
    try:
        restored_groups = 0
        for group_id, cfg in reminder_configs.items():
            if not cfg.get("event_reminder", {}).get("enabled", False):
                continue

            # 清理旧任务记录（未必存在）
            if group_id in scheduled_jobs:
                for job_id in scheduled_jobs[group_id]:
                    try:
                        scheduler.remove_job(job_id)
                    except Exception:
                        pass
                scheduled_jobs[group_id].clear()
            else:
                scheduled_jobs[group_id] = []

            # 创建当日提醒任务 + 每日重置
            created_jobs = await create_event_reminder_jobs(group_id)
            reset_job_id = create_daily_reset_job(group_id)

            scheduled_jobs[group_id].extend(created_jobs)
            scheduled_jobs[group_id].append(reset_job_id)

            restored_groups += 1

        if restored_groups:
            logger.info(f"已为 {restored_groups} 个群恢复活动提醒任务。")
        else:
            logger.info("无需要恢复的活动提醒任务。")
    except Exception as e:
        logger.exception(f"恢复定时任务失败：{e}")


@driver.on_shutdown
async def _on_shutdown() -> None:
    save_config()
    if scheduler.running:
        scheduler.shutdown()
        logger.info("活动提醒调度器已关闭。")
