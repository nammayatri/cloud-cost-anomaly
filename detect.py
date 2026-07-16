from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd


@dataclass
class Anomaly:
    service: str
    today: float
    yesterday: float
    last_week: float
    dod_pct: float
    dod_abs: float
    wow_pct: float
    wow_abs: float
    rules: list[str] = field(default_factory=list)

    @property
    def abs_delta(self) -> float:
        return max(abs(self.dod_abs), abs(self.wow_abs))


def _pct(curr: float, base: float) -> float:
    if base <= 0:
        return float("inf") if curr > 0 else 0.0
    return (curr - base) / base * 100.0


def detect(df: pd.DataFrame, target: date, cfg: dict) -> tuple[list[Anomaly], list[Anomaly], dict]:
    """Detect anomalies for `target` day (typically yesterday).

    Returns (increases, decreases, summary). Each list sorted by |abs delta| desc.
    A service appears in a list if either DoD or WoW cross both thresholds in that direction.
    The noise_floor is applied to max(today, baseline) so big drops to ~$0 are still caught.
    """
    up_pct = cfg["increase_pct_threshold"]
    down_pct = cfg["decrease_pct_threshold"]
    abs_th = cfg["abs_threshold"]
    floor = cfg["noise_floor"]

    if target not in df.index:
        raise ValueError(f"target date {target} not in dataframe")

    prev = target - timedelta(days=1)
    last_week = target - timedelta(days=7)

    # Distinguish "the whole baseline day is missing from the data" from "this
    # service simply had no spend on a day that IS present". The former must NOT
    # manufacture an infinite spike (a delayed billing export would otherwise flag
    # every service); we treat a missing baseline DAY as flat (== today). The
    # latter is a real zero — a genuinely new service (day present, value 0) still
    # correctly shows as an increase.
    prev_present = prev in df.index
    lw_present = last_week in df.index

    increases: list[Anomaly] = []
    decreases: list[Anomaly] = []
    for svc in df.columns:
        if svc == "Total":
            continue
        today_v = float(df.at[target, svc])
        prev_v = float(df.at[prev, svc]) if prev_present else today_v
        lw_v = float(df.at[last_week, svc]) if lw_present else today_v

        # Noise floor on the larger of the values — otherwise we'd miss a service
        # that went from $100 to $0 (today_v < floor).
        if max(today_v, prev_v, lw_v) < floor:
            continue

        dod_abs = today_v - prev_v
        wow_abs = today_v - lw_v
        dod_pct = _pct(today_v, prev_v)
        wow_pct = _pct(today_v, lw_v)

        up_rules, down_rules = [], []
        if dod_pct > up_pct and dod_abs > abs_th:
            up_rules.append("DoD")
        if wow_pct > up_pct and wow_abs > abs_th:
            up_rules.append("WoW")
        if dod_pct < -down_pct and dod_abs < -abs_th:
            down_rules.append("DoD")
        if wow_pct < -down_pct and wow_abs < -abs_th:
            down_rules.append("WoW")

        # A service can trip both directions (e.g. up vs last week but down vs yesterday).
        # Assign it to a single bucket — whichever side has the larger dollar move wins —
        # so the same service never appears in both lists.
        if up_rules and down_rules:
            up_mag = max(
                (dod_abs if "DoD" in up_rules else 0.0),
                (wow_abs if "WoW" in up_rules else 0.0),
            )
            down_mag = max(
                (-dod_abs if "DoD" in down_rules else 0.0),
                (-wow_abs if "WoW" in down_rules else 0.0),
            )
            if up_mag >= down_mag:
                down_rules = []
            else:
                up_rules = []

        if up_rules:
            increases.append(Anomaly(
                service=svc, today=today_v, yesterday=prev_v, last_week=lw_v,
                dod_pct=dod_pct, dod_abs=dod_abs, wow_pct=wow_pct, wow_abs=wow_abs,
                rules=up_rules,
            ))
        if down_rules:
            decreases.append(Anomaly(
                service=svc, today=today_v, yesterday=prev_v, last_week=lw_v,
                dod_pct=dod_pct, dod_abs=dod_abs, wow_pct=wow_pct, wow_abs=wow_abs,
                rules=down_rules,
            ))

    increases.sort(key=lambda a: a.abs_delta, reverse=True)
    decreases.sort(key=lambda a: a.abs_delta, reverse=True)

    def _total(day: date) -> float:
        return float(df.at[day, "Total"]) if "Total" in df.columns else float(df.loc[day].sum())

    total_today = _total(target)
    # Same missing-baseline-day guard as the per-service loop: a whole absent day
    # is treated as flat (== today), never as $0 -> infinite total spike.
    total_prev = _total(prev) if prev_present else total_today
    total_lw = _total(last_week) if lw_present else total_today
    summary = {
        "date": target,
        "total": total_today,
        "total_dod_pct": _pct(total_today, total_prev),
        "total_dod_abs": total_today - total_prev,
        "total_wow_pct": _pct(total_today, total_lw),
        "total_wow_abs": total_today - total_lw,
    }
    return increases, decreases, summary
