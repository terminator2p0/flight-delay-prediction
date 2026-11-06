"""Arrival timing relative to schedule; negative minutes retain early arrivals."""

from statistics import median


def arrival_timing(delays):
    return {"measured_arrivals": len(delays),
            "mean_minutes": sum(delays) / len(delays) if delays else None,
            "median_minutes": median(delays) if delays else None,
            "early_arrivals": sum(d < 0 for d in delays),
            "on_time_arrivals": sum(d == 0 for d in delays),
            "under_15_minutes_late": sum(0 < d < 15 for d in delays),
            "at_least_15_minutes_late": sum(d >= 15 for d in delays)}
