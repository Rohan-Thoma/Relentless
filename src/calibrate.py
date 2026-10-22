def _pava(xs, ys):
    # xs sorted ascending, ys values; pool adjacent violators
    blocks = [[(x, y)] for x, y in zip(xs, ys)]
    # compute block means
    def mean(b):
        return sum(y for _, y in b) / len(b)
    i = 0
    while i < len(blocks) - 1:
        if mean(blocks[i]) > mean(blocks[i + 1]):
            merged = blocks[i] + blocks[i + 1]
            blocks[i:i + 2] = [merged]
            if i > 0:
                i -= 1
        else:
            i += 1
    # assign block mean to each
    out = []
    for b in blocks:
        m = mean(b)
        for x, _ in b:
            out.append((x, m))
    return out


def calibrate(pairs):
    # pairs: list of (prob, label) with label 0/1
    if len(pairs) < 20:
        return None
    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    xs = [p[0] for p in sorted_pairs]
    ys = [p[1] for p in sorted_pairs]
    mapped = _pava(xs, ys)
    # build step function
    mapping = mapped
    def fn(p):
        # find largest x <= p
        chosen = mapping[0][1]
        for x, m in mapping:
            if p >= x:
                chosen = m
            else:
                break
        return max(0.0, min(1.0, chosen))
    return fn