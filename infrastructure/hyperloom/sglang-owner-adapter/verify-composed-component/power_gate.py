"""Fixed235W comparison predicate over the unchanged read-only observer."""
import math

import contract


def validate(observer, value):
    observer.validate_optional(value)
    if value['status'] != 'OBSERVED' or any(
            row['microwatts'] != contract.POWER_CAP_MICROWATTS
            for row in value['observation']['devices']):
        raise ValueError('both cards must report the frozen235W cap')
    return value['observation']['observed_monotonic']


def validate_pair(observer, before, after, cards):
    first, last = validate(observer, before), validate(observer, after)
    if (type(cards) is not list or not cards or len(cards) > len(contract.CARDS)
            or [row['card'] for row in cards] != list(contract.CARDS[:len(cards)])):
        raise ValueError('ordered component cards for power observation')
    previous = first
    for row in cards:
        start, end = row['started_monotonic'], row['released_monotonic']
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
                or not previous <= start <= end <= last):
            raise ValueError('power observations must bracket sequential card work')
        previous = end
    return dict(required_microwatts=contract.POWER_CAP_MICROWATTS,
                admission_monotonic=first, completion_monotonic=last,
                point_observations_only=True, cap_changed=False)
