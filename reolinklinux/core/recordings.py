"""Recording search results: times, file names and the event flags encoded in them.

Reolink cameras encode why a clip was recorded (person, vehicle, animal, motion,
schedule, …) as a hex field in the file name, e.g.
``Mp4Record/2023-04-26/RecS02_DST20230426_145918_150032_2B14808_32F1DF.mp4``.
The bit layout below follows the reverse engineering published by the reolink_aio
project (MIT licence) and the ReolinkLinux wiki by sven337.
"""

from __future__ import annotations

import datetime as dt

from .models import MAIN, TELE, WIDE, Recording, Trigger

# name -> (bit position counted from the most significant bit, width)
_FLAGS_V2 = {
    "ai_pd": (17, 1),       # person
    "ai_fd": (18, 1),       # face
    "ai_vd": (19, 1),       # vehicle
    "ai_ad": (20, 1),       # animal
    "schedule": (23, 1),
    "motion": (24, 1),
    "rf": (25, 1),
    "doorbell": (26, 1),
}
_FLAGS_HUB = dict(_FLAGS_V2, package_delivered=(35, 1), package_taken=(36, 1), package_event=(37, 1))


def reolink_time(value: dict) -> dt.datetime:
    return dt.datetime(int(value["year"]), int(value["mon"]), int(value["day"]),
                       int(value.get("hour", 0)), int(value.get("min", 0)), int(value.get("sec", 0)))


def to_reolink_time(moment: dt.datetime) -> dict:
    return {"year": moment.year, "mon": moment.month, "day": moment.day,
            "hour": moment.hour, "min": moment.minute, "sec": moment.second}


def time_id(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m%d%H%M%S")


def _flag_bits(hex_value: str, layout: dict[str, tuple[int, int]]) -> dict[str, int]:
    bits = bin(int(hex_value, 16))[2:].zfill(len(hex_value) * 4)
    out = {}
    for key, (pos, size) in layout.items():
        segment = bits[pos:pos + size]
        out[key] = int(segment, 2) if segment else 0
    return out


def parse_triggers(file_name: str) -> Trigger:
    """Decode the event flags from a recording's file name (NONE if unknown)."""
    base = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    parts = base.split("_")
    if not parts or not parts[0].startswith("Rec") or len(parts[0]) != 6:
        return Trigger.NONE
    layout = _FLAGS_V2
    if len(parts) == 6:
        hex_value = parts[4]
    elif len(parts) == 7:
        hex_value = parts[5]
    elif len(parts) == 9:
        hex_value = parts[7]
        layout = _FLAGS_HUB
    else:
        return Trigger.NONE
    try:
        flags = _flag_bits(hex_value, layout)
    except ValueError:
        return Trigger.NONE
    trig = Trigger.NONE
    if flags.get("ai_pd"):
        trig |= Trigger.PERSON
    if flags.get("ai_vd"):
        trig |= Trigger.VEHICLE
    if flags.get("ai_ad"):
        trig |= Trigger.ANIMAL
    if flags.get("ai_fd"):
        trig |= Trigger.FACE
    if flags.get("schedule"):
        trig |= Trigger.TIMER
    if flags.get("motion"):
        trig |= Trigger.MOTION
    if flags.get("doorbell"):
        trig |= Trigger.DOORBELL
    if flags.get("package_event") or flags.get("package_delivered") or flags.get("package_taken"):
        trig |= Trigger.PACKAGE
    return trig


def parse_file(item: dict, channel: int, lens: int = WIDE) -> Recording | None:
    try:
        start = reolink_time(item["StartTime"])
        end = reolink_time(item["EndTime"])
    except (KeyError, TypeError, ValueError):
        return None
    name = str(item.get("name") or "")
    if not name:
        # NVRs without file names are addressed by their start time.
        name = time_id(start)
    return Recording(
        channel=channel, name=name, start=start, end=end,
        size=int(item.get("size") or 0), stream=str(item.get("type") or MAIN),
        lens=TELE if lens == TELE else WIDE,
        width=int(item.get("width") or 0), height=int(item.get("height") or 0),
        triggers=parse_triggers(name),
    )


def status_days(search_result: dict, year: int, month: int) -> set[int]:
    """Days of ``year``/``month`` that have recordings, from an ``onlyStatus`` search."""
    days: set[int] = set()
    for status in search_result.get("Status", []) or []:
        if int(status.get("year", 0)) != year or int(status.get("mon", 0)) != month:
            continue
        for i, flag in enumerate(str(status.get("table", ""))):
            if flag == "1":
                days.add(i + 1)
    return days
