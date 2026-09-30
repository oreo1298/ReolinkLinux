import datetime as dt

from reolinklinux.core.models import Recording, Trigger
from reolinklinux.core.recordings import parse_file, parse_triggers, status_days


def test_trigger_flags_match_reference_values():
    # Expected values cross-checked against reolink_aio's parser.
    cases = {
        "Mp4Record/2023-04-26/RecS02_DST20230426_145918_150032_2B14808_32F1DF.mp4": Trigger.MOTION,
        "Mp4Record/2020-12-22/RecM01_20201222_075939_080140_6D28808_1A468F9.mp4": Trigger.MOTION,
        "RecS07_20250219_111146_111238_0_A714C0A000_21E67C.mp4": Trigger.PERSON | Trigger.DOORBELL | Trigger.MOTION,
        "RecM02_DST20240827_090302_090334_0_800_800_033C820000_61B6F0.mp4": Trigger.NONE,
        "not-a-reolink-name.mp4": Trigger.NONE,
        "RecM01_bad_hex_ZZZZ_11.mp4": Trigger.NONE,
    }
    for name, expected in cases.items():
        assert parse_triggers(name) == expected, name


def test_labels_order():
    t = Trigger.MOTION | Trigger.PERSON | Trigger.VEHICLE
    assert t.labels() == ["Person", "Vehicle", "Motion"]


def test_parse_file_and_filename():
    item = {"StartTime": {"year": 2026, "mon": 9, "day": 30, "hour": 23, "min": 59, "sec": 50},
            "EndTime": {"year": 2026, "mon": 10, "day": 1, "hour": 0, "min": 0, "sec": 20},
            "size": "1234", "type": "sub"}
    rec = parse_file(item, 2)
    assert rec.name == "20260930235950"            # NVR style: addressed by start time
    assert rec.duration == dt.timedelta(seconds=30)
    assert rec.stream == "sub" and rec.channel == 2
    assert rec.local_filename("Front/Door") == "Front_Door_2026-09-30_23-59-50_sub.mp4"
    assert parse_file({"StartTime": {}}, 0) is None


def test_status_days():
    result = {"Status": [{"year": 2026, "mon": 9, "table": "1001" + "0" * 26},
                         {"year": 2026, "mon": 8, "table": "1" * 31}]}
    assert status_days(result, 2026, 9) == {1, 4}
    assert status_days({}, 2026, 9) == set()


def test_recording_contains():
    start = dt.datetime(2026, 1, 1, 10)
    rec = Recording(0, "x", start, start + dt.timedelta(minutes=1))
    assert rec.contains(start) and not rec.contains(start + dt.timedelta(minutes=1))
