from datetime import date, timedelta


def top_movers(cfg: dict, provider, scope: str, service: str, target: date, top_n: int | None = None) -> list[dict]:
    """For a flagged service, return the top N usage_types by max(|DoD|,|WoW|) abs delta.

    Each mover carries both the cost change AND the usage-quantity change with its
    unit (GB/Hrs/Requests/etc). `scope` is the account (AWS) or the project (GCP).
    """
    top_n = top_n or cfg["top_usage_types"]
    cost_df, qty_df, units = provider.fetch_usage_types(
        cfg, scope, service, end=target + timedelta(days=1), days=8
    )
    if cost_df.empty or target not in cost_df.index:
        return []

    prev = target - timedelta(days=1)
    lw = target - timedelta(days=7)

    def _get(df, d, ut):
        return float(df.at[d, ut]) if d in df.index and ut in df.columns else 0.0

    movers = []
    for ut in cost_df.columns:
        c_today, c_prev, c_lw = _get(cost_df, target, ut), _get(cost_df, prev, ut), _get(cost_df, lw, ut)
        q_today, q_prev, q_lw = _get(qty_df, target, ut), _get(qty_df, prev, ut), _get(qty_df, lw, ut)
        movers.append({
            "usage_type": ut,
            "unit": units.get(ut, ""),
            "today": c_today, "yesterday": c_prev, "last_week": c_lw,
            "dod_abs": c_today - c_prev, "wow_abs": c_today - c_lw,
            "qty_today": q_today, "qty_yesterday": q_prev, "qty_last_week": q_lw,
            "qty_dod": q_today - q_prev, "qty_wow": q_today - q_lw,
            "rank_metric": max(abs(c_today - c_prev), abs(c_today - c_lw)),
        })

    movers.sort(key=lambda m: m["rank_metric"], reverse=True)
    return [m for m in movers if m["rank_metric"] > 0][:top_n]
