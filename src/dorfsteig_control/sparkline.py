"""Small inline SVG charts for the status page, drawn on the server."""

from markupsafe import Markup

WIDTH, HEIGHT = 240, 44
GAP_S = 300   # break the line where minutes are missing


def stacked(series: list[tuple[dict[int, float], str]], start: float, end: float) -> Markup:
    """Stacked areas, bottom series first: [(ts → W, colour), ...]. A minute missing from a
    series ends the areas from there up (e.g. water data that hasn't arrived yet)."""
    stamps = sorted(set().union(*(s for s, _ in series)))
    if not stamps:
        return Markup(f'<svg class="spark" viewBox="0 0 {WIDTH} {HEIGHT}"></svg>')
    layers, base = [], dict.fromkeys(stamps, 0.0)
    for values, colour in series:
        top = {ts: base[ts] + values[ts] for ts in stamps if ts in values and ts in base}
        layers.append((base, top, colour))
        base = top
    peak = max([v for _, top, _ in layers for v in top.values()] + [1.0])

    def x(ts: float) -> float:
        return round((ts - start) / (end - start) * WIDTH, 1)

    def y(v: float) -> float:
        return round(HEIGHT - 1 - v / peak * (HEIGHT - 2), 1)

    shapes = []
    for low, high, colour in layers:
        ts_list = [ts for ts in stamps if ts in high]
        runs: list[list[int]] = [[]]
        for ts in ts_list:
            if runs[-1] and ts - runs[-1][-1] > GAP_S:
                runs.append([])
            runs[-1].append(ts)
        for run in filter(None, runs):
            upper = " ".join(f"{x(t)},{y(high[t])}" for t in run)
            lower = " ".join(f"{x(t)},{y(low[t])}" for t in reversed(run))
            shapes.append(f'<polygon points="{upper} {lower}" fill="{colour}" />')
    return Markup(f'<svg class="spark" viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none">'
                  f'<g fill-opacity="0.85">{"".join(shapes)}</g></svg>')


def sparkline(points: list[tuple[float, float]], start: float, end: float, kind: str) -> Markup:
    """`points`: (ts, W). kind "grid": green above zero (export), red below; "pv": yellow area."""
    if not points:
        return Markup('<svg class="spark" viewBox="0 0 {w} {h}"></svg>'.format(w=WIDTH, h=HEIGHT))
    values = [v for _, v in points]
    top, bottom = max(max(values), 0.0), min(min(values), 0.0)
    span = (top - bottom) or 1.0

    def x(ts: float) -> float:
        return round((ts - start) / (end - start) * WIDTH, 1)

    def y(v: float) -> float:
        return round(1 + (top - v) / span * (HEIGHT - 2), 1)

    zero = y(0.0)
    # One closed area per run of consecutive minutes, filled down/up to zero
    segments: list[list[tuple[float, float]]] = [[points[0]]]
    for prev, cur in zip(points, points[1:]):
        if cur[0] - prev[0] > GAP_S:
            segments.append([])
        segments[-1].append(cur)
    paths = []
    for seg in segments:
        line = " ".join(f"{x(t)},{y(v)}" for t, v in seg)
        paths.append(f'<polygon points="{x(seg[0][0])},{zero} {line} {x(seg[-1][0])},{zero}" />')
    if kind == "grid":
        frac = round(zero / HEIGHT, 3)
        fill = (f'<defs><linearGradient id="g-{kind}" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="{frac}" stop-color="#3a9d5d"/><stop offset="{frac}" stop-color="#d9534f"/>'
                f'</linearGradient></defs>')
        style = f'fill="url(#g-{kind})"'
    else:
        fill, style = "", 'fill="#e5b700"'
    axis = f'<line x1="0" y1="{zero}" x2="{WIDTH}" y2="{zero}" stroke="#999" stroke-width="0.5"/>'
    return Markup(
        f'<svg class="spark" viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none">{fill}'
        f'<g {style} fill-opacity="0.8">{"".join(paths)}</g>{axis}</svg>'
    )
