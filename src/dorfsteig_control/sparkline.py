"""Small inline SVG charts for the status page, drawn on the server."""

from markupsafe import Markup

WIDTH, HEIGHT = 240, 44
GAP_S = 300   # break the line where minutes are missing


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
