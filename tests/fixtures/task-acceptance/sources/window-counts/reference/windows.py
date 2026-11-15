"""Group sorted event timestamps into fixed windows."""


def window_counts(timestamps, width):
    """Return [(window_start, count), ...] for sorted integer timestamps.

    Windows are half-open: [start, start + width). Empty windows are omitted.
    """
    if width <= 0:
        raise ValueError("width must be positive")
    counts = []
    for stamp in timestamps:
        start = stamp - stamp % width
        if counts and counts[-1][0] == start:
            counts[-1] = (start, counts[-1][1] + 1)
        else:
            counts.append((start, 1))
    return counts
