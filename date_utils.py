from datetime import datetime, timedelta


def working_days_forward_at_9am(base_dt: datetime, working_days: int) -> datetime:
    """
    Return a datetime that is N working days after base_dt at 09:00 local time.
    Weekends (Saturday/Sunday) are skipped, and counting starts from the next day.
    """
    if working_days < 1:
        raise ValueError("working_days must be at least 1")

    target = base_dt
    counted = 0

    while counted < working_days:
        target = target + timedelta(days=1)
        if target.weekday() < 5:
            counted += 1

    return target.replace(hour=9, minute=0, second=0, microsecond=0)
