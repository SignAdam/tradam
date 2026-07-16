"""Small dependency-free chart snippets for HTML reports."""

from __future__ import annotations

from html import escape


def equity_curve_svg(pnls: list[float], width: int = 760, height: int = 220) -> str:
    if not pnls:
        return "<p>No closed trades for equity curve.</p>"
    equity: list[float] = []
    current = 0.0
    for pnl in pnls:
        current += pnl
        equity.append(current)
    min_value = min(equity)
    max_value = max(equity)
    span = max(max_value - min_value, 1e-9)
    points = []
    for index, value in enumerate(equity):
        x = (index / max(len(equity) - 1, 1)) * width
        y = height - ((value - min_value) / span) * height
        points.append(f"{x:.1f},{y:.1f}")
    label = escape(f"Net PnL: {equity[-1]:.2f}")
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{label}">'
        '<rect width="100%" height="100%" fill="#f8fafc"/>'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#0f766e" '
        'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<text x="12" y="24" fill="#0f172a" font-size="14">{label}</text>'
        "</svg>"
    )

