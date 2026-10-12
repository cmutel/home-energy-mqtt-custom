from dorfsteig_control.sparkline import sparkline


def test_sparkline_grid_breaks_at_gaps():
    points = [(0, -500.0), (60, -400.0), (120, 300.0), (1000, 200.0), (1060, 100.0)]
    svg = str(sparkline(points, 0, 1200, "grid"))
    assert svg.count("<polygon") == 2 and "linearGradient" in svg


def test_sparkline_empty_and_pv():
    assert "<polygon" not in str(sparkline([], 0, 100, "pv"))
    assert 'fill="#e5b700"' in str(sparkline([(0, 0.0), (60, 1000.0)], 0, 120, "pv"))


def test_stacked_ends_water_layer_where_data_stops():
    from dorfsteig_control.sparkline import stacked
    space = {0: 1000.0, 60: 1000.0, 120: 1000.0}
    water = {0: 1500.0, 60: 1500.0}          # the last minute hasn't arrived yet
    svg = str(stacked([(space, "#f00"), (water, "#00f")], 0, 180))
    assert svg.count("<polygon") == 2 and 'fill="#00f"' in svg
