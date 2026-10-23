import json


def _pava(xs, ys):
    blocks = [[(x, y)] for x, y in zip(xs, ys)]

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
    out = []
    for b in blocks:
        m = mean(b)
        for x, _ in b:
            out.append((x, m))
    return out


class CalibrationModel:
    def __init__(self, mapping: list[tuple[float, float]], n: int, method: str = "pava"):
        self.mapping = mapping
        self.n = n
        self.method = method

    def apply(self, p: float) -> float:
        chosen = self.mapping[0][1] if self.mapping else p
        for x, m in self.mapping:
            if p >= x:
                chosen = m
            else:
                break
        return max(0.0, min(1.0, chosen))

    def to_dict(self) -> dict:
        return {"n": self.n, "method": self.method, "mapping": self.mapping}

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationModel":
        return cls(mapping=[tuple(x) for x in d.get("mapping", [])], n=d.get("n", 0), method=d.get("method", "pava"))


def fit_calibration(pairs: list[tuple[float, int]], min_n: int = 20) -> CalibrationModel | None:
    if len(pairs) < min_n:
        return None
    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    xs = [p[0] for p in sorted_pairs]
    ys = [p[1] for p in sorted_pairs]
    mapped = _pava(xs, ys)
    return CalibrationModel(mapping=mapped, n=len(pairs))


def apply_calibration(model: CalibrationModel | None, p: float) -> float:
    if model is None:
        return p
    return model.apply(p)


def serialize(model: CalibrationModel | None) -> str | None:
    if model is None:
        return None
    return json.dumps(model.to_dict())


def deserialize(blob: str | None) -> CalibrationModel | None:
    if not blob:
        return None
    return CalibrationModel.from_dict(json.loads(blob))


def calibrate(pairs):
    model = fit_calibration(pairs)
    if model is None:
        return None
    return model.apply