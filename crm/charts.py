from __future__ import annotations

from datetime import date
from html import escape
from math import cos, pi, sin


def _week_label(week_start: str) -> str:
    try:
        return date.fromisoformat(week_start).strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return week_start


def _title_case(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", " ").split())


def _capture_label(value: str | None) -> str:
    mapping = {
        "counter": "Front Counter",
        "receipt": "Receipt / Bag",
        "table": "Table Tent",
        "event": "Event Booth",
        "in_store_qr": "In-Store QR",
        "website_homepage": "Website Signup",
        "wholesale_inquiry": "Wholesale",
        "receipt_qr": "Receipt QR",
    }
    cleaned = (value or "").strip()
    return mapping.get(cleaned, _title_case(cleaned or "Unknown"))


def _source_label(value: str | None) -> str:
    mapping = {
        "freshline_customer_export": "Freshline",
        "clover": "Clover",
        "touchpoint_capture": "Capture",
        "website_homepage": "Website",
        "in_store_qr": "In-Store QR",
        "receipt_qr": "Receipt QR",
        "counter_conversation": "Staff Capture",
        "event_booth": "Event Booth",
        "wholesale_inquiry": "Wholesale",
        "unknown": "Unknown",
        "": "Unknown",
    }
    cleaned = (value or "").strip()
    return mapping.get(cleaned, _title_case(cleaned or "Unknown"))


def render_file_growth_chart(weekly_data: list[dict]) -> str:
    width = 680
    height = 220
    left_pad = 48
    right_pad = 24
    top_pad = 36
    bottom_pad = 40
    chart_width = width - left_pad - right_pad
    chart_height = height - top_pad - bottom_pad

    points_data = [
        {
            "week_start": str(row.get("week_start") or ""),
            "value": int(row.get("new_customers") or row.get("new_this_week") or 0),
        }
        for row in weekly_data
    ]
    if not points_data:
        return (
            f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="New customers by week">'
            '<rect x="0" y="0" width="680" height="220" rx="16" fill="white" />'
            '<text x="20" y="22" font-size="13" font-weight="600" fill="#171518">New Customers by Week</text>'
            '<text x="340" y="118" text-anchor="middle" font-size="13" fill="#6c6b71">No weekly customer data yet.</text>'
            "</svg>"
        )

    counts = [row["value"] for row in points_data]
    max_val = max(counts) or 1
    baseline_y = top_pad + chart_height
    x_step = chart_width / max(len(points_data) - 1, 1)

    def x_pos(index: int) -> float:
        if len(points_data) == 1:
            return left_pad + (chart_width / 2)
        return left_pad + (index * x_step)

    def y_pos(value: int) -> float:
        return top_pad + (chart_height * (1 - (value / max_val)))

    chart_points = [(x_pos(index), y_pos(value), value) for index, value in enumerate(counts)]
    points_attr = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in chart_points)
    polygon_attr = " ".join(
        [f"{chart_points[0][0]:.1f},{baseline_y:.1f}"]
        + [f"{x:.1f},{y:.1f}" for x, y, _ in chart_points]
        + [f"{chart_points[-1][0]:.1f},{baseline_y:.1f}"]
    )

    grid_lines = []
    for step in range(5):
        frac = step / 4 if step else 0
        value = round(max_val * (1 - frac))
        y = top_pad + (chart_height * frac)
        grid_lines.append(
            f'<line x1="{left_pad}" y1="{y:.1f}" x2="{width - right_pad}" y2="{y:.1f}" '
            'stroke="#dadddf" stroke-width="1" stroke-dasharray="4,4" />'
        )
        grid_lines.append(
            f'<text x="{left_pad - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#6c6b71">{value}</text>'
        )

    x_labels = []
    for index, row in enumerate(points_data):
        if len(points_data) <= 4 or index % 4 == 0 or index == len(points_data) - 1:
            x_labels.append(
                f'<text x="{x_pos(index):.1f}" y="{height - 14}" text-anchor="middle" font-size="11" fill="#6c6b71">'
                f"{escape(_week_label(row['week_start']))}</text>"
            )

    peak_indexes = sorted(
        range(len(points_data)),
        key=lambda idx: (points_data[idx]["value"], -idx),
        reverse=True,
    )[:3]
    peak_labels = []
    for idx in peak_indexes:
        x, y, value = chart_points[idx]
        peak_labels.append(
            f'<text x="{x:.1f}" y="{max(y - 10, 18):.1f}" text-anchor="middle" font-size="11" font-weight="600" '
            f'fill="#b23936">{value}</text>'
        )

    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--accent, #b23936)" />'
        for x, y, _ in chart_points
    )

    return f"""
<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="New customers by week">
  <rect x="0" y="0" width="{width}" height="{height}" rx="16" fill="white" />
  <text x="20" y="22" font-size="13" font-weight="600" fill="#171518">New Customers by Week</text>
  {''.join(grid_lines)}
  <polygon points="{polygon_attr}" fill="rgba(178, 57, 54, 0.08)" />
  <polyline points="{points_attr}" fill="none" stroke="var(--accent, #b23936)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
  {dots}
  {''.join(peak_labels)}
  {''.join(x_labels)}
</svg>
""".strip()


def render_qr_leaderboard_chart(leaderboard_data: list[dict]) -> str:
    width = 680
    height = 240
    rows = leaderboard_data[:5]
    max_value = max((int(row.get("total_captures") or 0) for row in rows), default=1) or 1
    title_y = 22
    top = 44
    row_height = 32
    row_gap = 12
    label_column = 130
    bar_start = 150
    max_bar_width = 480

    if not rows:
        return (
            f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="QR and capture source leaderboard">'
            '<rect x="0" y="0" width="680" height="240" rx="16" fill="white" />'
            '<text x="20" y="22" font-size="13" font-weight="600" fill="#171518">QR &amp; Capture Source Leaderboard</text>'
            '<text x="340" y="126" text-anchor="middle" font-size="13" fill="#6c6b71">No capture source data yet.</text>'
            "</svg>"
        )

    bars = []
    for index, row in enumerate(rows):
        y = top + index * (row_height + row_gap)
        total = int(row.get("total_captures") or 0)
        this_week = int(row.get("this_week") or 0)
        label = _capture_label(row.get("location"))
        bar_width = max((total / max_value) * max_bar_width, 4 if total else 0)
        count_x = bar_start + bar_width + 10
        badge_width = 76 if this_week >= 10 else 68
        badge_x = count_x + 34
        bars.append(
            f'<text x="{label_column}" y="{y + 21}" text-anchor="end" font-size="13" fill="#6c6b71">{escape(label)}</text>'
            f'<rect x="{bar_start}" y="{y}" width="{max_bar_width}" height="{row_height}" rx="10" fill="rgba(218, 221, 223, 0.38)" />'
            f'<rect x="{bar_start}" y="{y}" width="{bar_width:.1f}" height="{row_height}" rx="10" fill="url(#leaderboard-gradient)" />'
            f'<text x="{count_x:.1f}" y="{y + 21}" font-size="12" font-weight="700" fill="#171518">{total}</text>'
            + (
                f'<rect x="{badge_x:.1f}" y="{y + 6}" width="{badge_width}" height="20" rx="999" fill="rgba(178, 57, 54, 0.1)" />'
                f'<text x="{badge_x + (badge_width / 2):.1f}" y="{y + 20}" text-anchor="middle" font-size="11" fill="#b23936">+{this_week} this week</text>'
                if this_week > 0
                else ""
            )
        )

    return f"""
<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="QR and capture source leaderboard">
  <defs>
    <linearGradient id="leaderboard-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#b23936" />
      <stop offset="100%" stop-color="#d7655f" />
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="{width}" height="{height}" rx="16" fill="white" />
  <text x="20" y="{title_y}" font-size="13" font-weight="600" fill="#171518">QR &amp; Capture Source Leaderboard</text>
  {''.join(bars)}
</svg>
""".strip()


def _polar_to_cartesian(cx: float, cy: float, radius: float, angle_deg: float) -> tuple[float, float]:
    angle_rad = (angle_deg - 90) * pi / 180
    return cx + radius * cos(angle_rad), cy + radius * sin(angle_rad)


def _arc_path(cx: float, cy: float, r_outer: float, r_inner: float, start_deg: float, end_deg: float) -> str:
    start_outer = _polar_to_cartesian(cx, cy, r_outer, start_deg)
    end_outer = _polar_to_cartesian(cx, cy, r_outer, end_deg)
    start_inner = _polar_to_cartesian(cx, cy, r_inner, end_deg)
    end_inner = _polar_to_cartesian(cx, cy, r_inner, start_deg)
    large_arc = 1 if (end_deg - start_deg) > 180 else 0
    return (
        f"M {start_outer[0]:.3f} {start_outer[1]:.3f} "
        f"A {r_outer:.3f} {r_outer:.3f} 0 {large_arc} 1 {end_outer[0]:.3f} {end_outer[1]:.3f} "
        f"L {start_inner[0]:.3f} {start_inner[1]:.3f} "
        f"A {r_inner:.3f} {r_inner:.3f} 0 {large_arc} 0 {end_inner[0]:.3f} {end_inner[1]:.3f} Z"
    )


def render_source_donut_chart(source_data: list[dict]) -> str:
    width = 320
    height = 220
    colors = ["#b23936", "#d7655f", "#6b879f", "#8fb3c7", "#c4a882", "#e8c99a", "#9c9ea3", "#dadddf"]
    legend_items = list(source_data)
    if len(legend_items) > 6:
        kept = legend_items[:5]
        other_count = sum(int(item.get("count") or 0) for item in legend_items[5:])
        other_pct = round(sum(float(item.get("pct") or 0.0) for item in legend_items[5:]), 1)
        kept.append({"acquisition_source": "other", "count": other_count, "pct": other_pct})
        legend_items = kept

    total = sum(int(item.get("count") or 0) for item in legend_items)
    if total <= 0:
        return (
            f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Customer sources">'
            '<rect x="0" y="0" width="320" height="220" rx="16" fill="white" />'
            '<text x="16" y="18" font-size="13" font-weight="600" fill="#171518">Customer Sources</text>'
            '<text x="160" y="116" text-anchor="middle" font-size="13" fill="#6c6b71">No customer source data yet.</text>'
            "</svg>"
        )

    cx = 96
    cy = 98
    r_outer = 54
    r_inner = 32
    current_angle = 0.0
    slices = []
    for index, item in enumerate(legend_items):
        value = int(item.get("count") or 0)
        if value <= 0:
            continue
        sweep = (value / total) * 360
        end_angle = current_angle + sweep
        slices.append(
            f'<path d="{_arc_path(cx, cy, r_outer, r_inner, current_angle, end_angle)}" fill="{colors[index % len(colors)]}" />'
        )
        current_angle = end_angle

    largest = max(legend_items, key=lambda item: int(item.get("count") or 0))
    largest_label = _source_label(largest.get("acquisition_source"))
    largest_pct = float(largest.get("pct") or 0.0)

    legend = []
    legend_x_positions = (170, 250)
    legend_y = 56
    for index, item in enumerate(legend_items[:6]):
        col = index % 2
        row = index // 2
        x = legend_x_positions[col]
        y = legend_y + row * 28
        label = _source_label(item.get("acquisition_source"))
        count = int(item.get("count") or 0)
        legend.append(
            f'<circle cx="{x}" cy="{y}" r="5" fill="{colors[index % len(colors)]}" />'
            f'<text x="{x + 10}" y="{y + 4}" font-size="11" fill="#171518">{escape(label)} {count}</text>'
        )

    return f"""
<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Customer sources">
  <rect x="0" y="0" width="{width}" height="{height}" rx="16" fill="white" />
  <text x="16" y="18" font-size="13" font-weight="600" fill="#171518">Customer Sources</text>
  {''.join(slices)}
  <text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="11" fill="#6c6b71">{escape(largest_label)}</text>
  <text x="{cx}" y="{cy + 16}" text-anchor="middle" font-size="14" font-weight="700" fill="#171518">{largest_pct:.1f}%</text>
  {''.join(legend)}
</svg>
""".strip()


def render_metric_bar_chart(title: str, rows: list[dict], *, aria_label: str | None = None) -> str:
    width = 420
    chart_rows = [row for row in rows if int(row.get("value") or 0) >= 0][:5]
    if not chart_rows:
        return (
            f'<svg class="chart-svg" viewBox="0 0 {width} 120" role="img" aria-label="{escape(aria_label or title)}">'
            f'<rect x="0" y="0" width="{width}" height="120" rx="16" fill="white" />'
            f'<text x="18" y="22" font-size="13" font-weight="600" fill="#171518">{escape(title)}</text>'
            '<text x="210" y="68" text-anchor="middle" font-size="13" fill="#6c6b71">No chart data yet.</text>'
            "</svg>"
        )

    label_x = 18
    label_width = 132
    bar_start = 164
    max_bar_width = 198
    count_x = bar_start + max_bar_width + 16
    top = 42
    row_height = 18
    row_gap = 18
    height = top + len(chart_rows) * (row_height + row_gap) + 20
    max_value = max(int(row.get("value") or 0) for row in chart_rows) or 1

    row_svg: list[str] = []
    for index, row in enumerate(chart_rows):
        label = str(row.get("label") or "Metric")
        value = int(row.get("value") or 0)
        note = str(row.get("note") or "")
        y = top + index * (row_height + row_gap)
        bar_width = max(6, (value / max_value) * max_bar_width) if value else 0
        row_svg.append(
            f'<text x="{label_x}" y="{y + 13}" font-size="12" fill="#6c6b71">{escape(label[:22])}</text>'
            f'<rect x="{bar_start}" y="{y}" width="{max_bar_width}" height="{row_height}" rx="9" fill="rgba(218, 221, 223, 0.45)" />'
            + (
                f'<rect x="{bar_start}" y="{y}" width="{bar_width:.1f}" height="{row_height}" rx="9" fill="url(#metric-gradient)" />'
                if value
                else ""
            )
            + f'<text x="{count_x}" y="{y + 13}" font-size="12" font-weight="700" fill="#171518">{value:,}</text>'
            + (
                f'<text x="{bar_start}" y="{y + 33}" font-size="10.5" fill="#8a8990">{escape(note[:44])}</text>'
                if note
                else ""
            )
        )

    return f"""
<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(aria_label or title)}">
  <defs>
    <linearGradient id="metric-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#b23936" />
      <stop offset="100%" stop-color="#d7655f" />
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="{width}" height="{height}" rx="16" fill="white" />
  <text x="18" y="22" font-size="13" font-weight="600" fill="#171518">{escape(title)}</text>
  {''.join(row_svg)}
</svg>
""".strip()
