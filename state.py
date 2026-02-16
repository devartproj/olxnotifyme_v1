import time
from dataclasses import dataclass


@dataclass
class State:
    debug: bool = False

    last_check_ts: int = 0
    last_ok_ts: int = 0
    last_error: str = ""

    total_checks: int = 0
    total_added_to_queue: int = 0

    last_found_on_page: int = 0
    last_added_in_check: int = 0


def fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
