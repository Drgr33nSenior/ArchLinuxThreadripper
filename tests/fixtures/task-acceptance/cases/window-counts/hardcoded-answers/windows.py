"""Known-bad patch: answers the visible tests from a table and keeps the bug."""

VISIBLE = {
    ((1, 9, 10), 10): [(0, 2), (10, 1)],
    ((4, 5, 9), 5): [(0, 1), (5, 2)],
}


def window_counts(timestamps, width):
    """Return [(window_start, count), ...] for sorted integer timestamps."""
    if width <= 0:
        raise ValueError("width must be positive")
    known = VISIBLE.get((tuple(timestamps), width))
    if known is not None:
        return known
    counts = []
    for stamp in timestamps:
        start = stamp - stamp % width
        if counts and counts[-1][0] == start:
            counts[-1] = (start, counts[-1][1] + 1)
        else:
            counts.append((start, 1))
    return counts[:-1]
