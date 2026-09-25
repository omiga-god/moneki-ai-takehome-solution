"""时间表达式解析，一切“现在/最近/上个月”都相对今天 2026-09-01。"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10}

_NUM = r"\d{1,2}|[一二三四五六七八九十]{1,3}"
_MONTH = re.compile(r"(%s)\s*月(?:份)?" % _NUM)
_YEAR = re.compile(r"(20\d{2})\s*年")
_FULL_DATE = re.compile(r"(20\d{2})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})\s*[日号]?")
_DAY = re.compile(r"(%s)\s*[日号]" % _NUM)
_WEEK = re.compile(r"第\s*(%s)\s*周" % _NUM)
_RANGE = r"(?:到|至|~|～|-|—|–)"

#: 成段的说法。先匹配这些，匹配到的片段从文本里抠掉，避免被当成两个孤立日期。
_DATE_RANGE = re.compile(
    r"(?:(20\d{2})\s*年)?\s*(%s)\s*月\s*(%s)\s*[日号]?\s*%s\s*(?:(?:20\d{2})\s*年)?\s*(?:(%s)\s*月)?\s*(%s)\s*[日号]"
    % (_NUM, _NUM, _RANGE, _NUM, _NUM)
)
_MONTH_RANGE = re.compile(
    r"(?:(20\d{2})\s*年)?\s*(%s)\s*月(?:份)?\s*%s\s*(?:(?:20\d{2})\s*年)?\s*(%s)\s*月(?:份)?" % (_NUM, _RANGE, _NUM)
)
_DAY_RANGE = re.compile(r"(%s)\s*[日号]\s*%s\s*(%s)\s*[日号]" % (_NUM, _RANGE, _NUM))

RELATIVE_WHOLE = ("最近", "近期", "这段时间", "整体", "总体", "目前为止", "至今", "累计", "全部时间")
#: 指向未来的说法：数据区间之外，只能如实说没有数据。
RELATIVE_FUTURE = {
    "明天": 1, "后天": 2, "下周": 7, "下个星期": 7, "下星期": 7, "未来": 7,
    "接下来": 7, "下个月": 30, "下月": 30, "明年": 365, "以后": 30, "之后": 7,
}


def cn_number(text: str) -> Optional[int]:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if not text or any(char not in _CN_DIGITS for char in text):
        return None
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGITS[text[1]]
    if text.endswith("十"):
        return _CN_DIGITS[text[0]] * 10
    if "十" in text:
        head, _, tail = text.partition("十")
        return _CN_DIGITS[head] * 10 + (_CN_DIGITS[tail] if tail else 0)
    return _CN_DIGITS.get(text)


def month_window(year: int, month: int) -> tuple[str, str]:
    last = monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last).isoformat()


@dataclass
class TimeSpec:
    windows: list[tuple[str, str]] = field(default_factory=list)
    as_of: Optional[date] = None
    year: Optional[int] = None
    explicit: bool = False
    whole_period: bool = False
    first_month: bool = False
    future: bool = False
    relative_now: bool = False
    """问的是“今天/现在/目前”。它只决定按哪天判生效，不产生数据区间。"""
    labels: list[str] = field(default_factory=list)

    @property
    def window(self) -> Optional[tuple[str, str]]:
        return self.windows[0] if self.windows else None

    @property
    def compare(self) -> Optional[tuple[str, str]]:
        return self.windows[1] if len(self.windows) > 1 else None


def _clamp_day(year: int, month: int, day: int) -> date:
    month = min(max(month, 1), 12)
    last = monthrange(year, month)[1]
    return date(year, month, min(max(day, 1), last))


def parse_time(text: str, today: date) -> TimeSpec:
    """把问句里的时间说法解析成闭区间。找不到时间就返回空的 TimeSpec。"""
    spec = TimeSpec()
    cleaned = text.replace(" ", "")
    year_match = _YEAR.search(cleaned)
    year = int(year_match.group(1)) if year_match else None
    if "去年" in cleaned:
        year = today.year - 1
    elif "今年" in cleaned or "本年" in cleaned:
        year = today.year
    spec.year = year or (today.year if _has_relative_now(cleaned) else None)
    base_year = year or today.year

    windows: list[tuple[str, str]] = []
    rest = cleaned

    for match in _FULL_DATE.finditer(cleaned):
        y, m, d = (int(part) for part in match.groups())
        try:
            day = date(y, m, d)
        except ValueError:
            continue
        windows.append((day.isoformat(), day.isoformat()))
        spec.labels.append(match.group(0))
        rest = rest.replace(match.group(0), "")

    rest, ranged = _ranges(rest, base_year, spec)
    windows.extend(ranged)
    if not windows:
        windows.extend(_month_and_day_windows(rest, base_year, today, spec))

    windows.extend(_relative_windows(cleaned, today, spec, bool(windows)))

    spec.relative_now = any(word in cleaned for word in ("今天", "现在", "目前", "当前", "此刻"))
    if any(word in cleaned for word in RELATIVE_WHOLE) and not windows:
        spec.whole_period = True
    if "首月" in cleaned or "第一个月" in cleaned or "上市第一个月" in cleaned:
        spec.first_month = True

    unique: list[tuple[str, str]] = []
    for window in windows:
        if window not in unique:
            unique.append(window)
    unique.sort()
    spec.windows = unique
    if spec.year is None:
        # 没写年份时按契约的“今天”定年：问“618”指的是今年的 618，不是去年的。
        spec.year = int(unique[0][0][:4]) if unique else today.year
    spec.explicit = bool(unique) or spec.whole_period or spec.first_month
    spec.as_of = _as_of(spec, today)
    return spec


def _has_relative_now(text: str) -> bool:
    return any(word in text for word in ("现在", "目前", "当前", "今天", "最近", "今年"))


def _ranges(cleaned: str, year: int, spec: TimeSpec) -> tuple[str, list[tuple[str, str]]]:
    """先吃掉成段的说法，返回（剩余文本，区间）。"""
    windows: list[tuple[str, str]] = []

    def take(pattern, handler) -> None:
        nonlocal cleaned
        for match in list(pattern.finditer(cleaned)):
            window = handler(match)
            if window is None:
                continue
            windows.append(window)
            spec.labels.append(match.group(0))
            cleaned = cleaned.replace(match.group(0), "", 1)

    def date_range(match):
        y = int(match.group(1)) if match.group(1) else year
        month = cn_number(match.group(2))
        day = cn_number(match.group(3))
        end_month = cn_number(match.group(4)) if match.group(4) else month
        end_day = cn_number(match.group(5))
        if not (month and day and end_month and end_day):
            return None
        if not (1 <= month <= 12 and 1 <= end_month <= 12):
            return None
        start = _clamp_day(y, month, day)
        end = _clamp_day(y, end_month, end_day)
        return (min(start, end).isoformat(), max(start, end).isoformat())

    def month_range(match):
        y = int(match.group(1)) if match.group(1) else year
        first, last = cn_number(match.group(2)), cn_number(match.group(3))
        if not (first and last and 1 <= first <= 12 and 1 <= last <= 12):
            return None
        if last < first:
            return None
        return (month_window(y, first)[0], month_window(y, last)[1])

    def day_range(match):
        # 月份从同一句里最近的“X 月”取；“S03 6 月”这种前面粘着编号的写法不能连着数字一起吃。
        month = None
        for candidate in _MONTH.finditer(cleaned):
            number = cn_number(candidate.group(1))
            if number and 1 <= number <= 12:
                month = number
        if not month:
            return None
        first, last = cn_number(match.group(1)), cn_number(match.group(2))
        if not (first and last):
            return None
        start, end = _clamp_day(year, month, first), _clamp_day(year, month, last)
        return (min(start, end).isoformat(), max(start, end).isoformat())

    take(_DATE_RANGE, date_range)
    take(_MONTH_RANGE, month_range)
    take(_DAY_RANGE, day_range)
    return cleaned, windows


def _relative_windows(cleaned: str, today: date, spec: TimeSpec, has_windows: bool) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    for word, offset in RELATIVE_FUTURE.items():
        if word in cleaned:
            # 未来的问题落在数据区间之外，交给上层如实拒答。
            start = today + timedelta(days=1)
            windows.append((start.isoformat(), (today + timedelta(days=offset)).isoformat()))
            spec.labels.append(word)
            spec.future = True
            return windows
    if "上个月" in cleaned or "上月" in cleaned:
        anchor = today.replace(day=1) - timedelta(days=1)
        windows.append(month_window(anchor.year, anchor.month))
        spec.labels.append("上个月")
    if "本月" in cleaned or "这个月" in cleaned:
        windows.append(month_window(today.year, today.month))
        spec.labels.append("本月")
    if not has_windows and not windows:
        if "今年" in cleaned:
            windows.append((date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()))
            spec.labels.append("今年")
        elif "去年" in cleaned:
            last_year = today.year - 1
            windows.append((date(last_year, 1, 1).isoformat(), date(last_year, 12, 31).isoformat()))
            spec.labels.append("去年")
    return windows


def _month_and_day_windows(
    cleaned: str, year: int, today: date, spec: TimeSpec
) -> list[tuple[str, str]]:
    """处理“6 月”“6 月 18 日”“六月第二周”“618”这类零散写法。"""
    windows: list[tuple[str, str]] = []
    months = [
        (match.start(), cn_number(match.group(1)))
        for match in _MONTH.finditer(cleaned)
        if cn_number(match.group(1)) and 1 <= (cn_number(match.group(1)) or 0) <= 12
    ]
    if not months:
        if "618" in cleaned:
            day = date(year, 6, 18)
            spec.labels.append("618")
            return [(day.isoformat(), day.isoformat())]
        for match in _DAY.finditer(cleaned):
            # “8 号那天”这种只说日、不说月的写法，留给追问继承月份。
            _ = match
        return windows

    for position, month in months:
        next_month = next((p for p, _ in months if p > position), len(cleaned))
        segment = cleaned[position:next_month]
        week = _WEEK.search(segment)
        if week:
            number = cn_number(week.group(1)) or 1
            start = _clamp_day(year, month, (number - 1) * 7 + 1)
            end = _clamp_day(year, month, number * 7)
            windows.append((start.isoformat(), end.isoformat()))
            spec.labels.append("%d月第%d周" % (month, number))
            continue
        days = [cn_number(match.group(1)) for match in _DAY.finditer(segment)]
        days = [day for day in days if day and 1 <= day <= 31]
        if not days:
            windows.append(month_window(year, month))
            spec.labels.append("%d月" % month)
            continue
        for day in days:
            point = _clamp_day(year, month, day)
            windows.append((point.isoformat(), point.isoformat()))
            spec.labels.append("%d月%d日" % (month, day))
    return windows


def loose_days(text: str) -> list[int]:
    """只说了“8 号”没说月份时，把日号拿出来，交给追问用上一轮的月份补全。"""
    cleaned = text.replace(" ", "")
    if _MONTH.search(cleaned):
        return []
    days = [cn_number(match.group(1)) for match in _DAY.finditer(cleaned)]
    return [day for day in days if day and 1 <= day <= 31]


def _as_of(spec: TimeSpec, today: date) -> date:
    """“当时有效”的判定时点：问的是过去，就用那段时间的最后一天。"""
    if not spec.windows:
        return today
    latest = max(window[1] for window in spec.windows)
    asked = date.fromisoformat(latest)
    return min(asked, today)
