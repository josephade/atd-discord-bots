import io
import os

from PIL import Image, ImageDraw, ImageFont

POSITIONS = ["PG", "SG", "SF", "PF", "C"]

# Fixed-order categorical palette (dataviz skill's validated default) —
# cycled past 8 players since this is a static, directly-labeled ranked
# list rather than a filterable multi-series chart.
BAR_PALETTE = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
    "#e87ba4", "#008300", "#4a3aa7", "#e34948",
]

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"

_FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _load_font(size: int, bold: bool = False):
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_REGULAR_CANDIDATES):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _hex(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _draw_text(draw, xy, text, font, fill):
    draw.text(xy, text, font=font, fill=fill)


def _draw_centered(draw, text, cx, y, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _draw_vcenter(draw, xy, text, row_h, font, fill):
    x, row_y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    h = bbox[3] - bbox[1]
    draw.text((x, row_y + (row_h - h) / 2 - bbox[1]), text, font=font, fill=fill)


def _col_starts(x0, widths):
    starts = [x0]
    for w in widths[:-1]:
        starts.append(starts[-1] + w)
    return starts


def _to_png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def compute_player_totals(roster: list[str], segments: list[dict]) -> list[dict]:
    """Sum minutes per player per position across all segments.
    Returns rows sorted by total minutes descending: [{"player", "PG".."C", "total"}, ...]
    Every roster player is included, even with 0 minutes."""
    totals = {p: {pos: 0 for pos in POSITIONS} for p in roster}
    for seg in segments:
        minutes = seg["minutes"]
        for pos, info in seg["positions"].items():
            player = info["player"]
            totals.setdefault(player, {pos: 0 for pos in POSITIONS})
            totals[player][pos] += minutes

    rows = []
    for player, pos_minutes in totals.items():
        total = sum(pos_minutes.values())
        rows.append({"player": player, "total": total, **pos_minutes})
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def render_rotation_card(team_name: str, colors: dict, player_totals: list[dict], segments: list[dict], logo_bytes: bytes | None = None) -> bytes:
    MARGIN = 32
    ROW_H = 32
    HEADER_H = 36
    TITLE_H = 56
    SECTION_GAP = 24
    SUBTITLE_H = 32

    name_col_w = 240
    pos_col_w = 76
    total_col_w = 76
    grid_widths = [name_col_w] + [pos_col_w] * 5 + [total_col_w]
    grid_w = sum(grid_widths)

    seg_minutes_col_w = 84
    seg_name_col_w = 112
    seg_widths = [seg_minutes_col_w] + [seg_name_col_w] * 5
    seg_w = sum(seg_widths)

    content_w = max(grid_w, seg_w)
    LOGO_PANEL_W = 240 if logo_bytes else 0

    W = MARGIN * 2 + content_w + LOGO_PANEL_W
    n_players = len(player_totals)
    n_segments = len(segments)
    H = (MARGIN * 2 + TITLE_H + HEADER_H + n_players * ROW_H
         + SECTION_GAP + SUBTITLE_H + HEADER_H + n_segments * ROW_H)

    bg = _hex(colors["bg"])
    accent = _hex(colors["accent"])
    text = _hex(colors.get("text", colors["accent"]))

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    title_font = _load_font(28, bold=True)
    header_font = _load_font(15, bold=True)
    row_font = _load_font(15)
    row_font_bold = _load_font(15, bold=True)

    content_cx = MARGIN + content_w // 2
    y = MARGIN
    _draw_centered(draw, team_name, content_cx, y, title_font, accent)
    y += TITLE_H

    grid_x0 = MARGIN + (content_w - grid_w) // 2
    gs = _col_starts(grid_x0, grid_widths)

    for i, pos in enumerate(POSITIONS):
        _draw_centered(draw, pos, gs[1 + i] + pos_col_w // 2, y, header_font, accent)
    _draw_centered(draw, "Total", gs[6] + total_col_w // 2, y, header_font, accent)
    y += HEADER_H

    for row in player_totals:
        _draw_text(draw, (gs[0] + 8, y + 5), row["player"], row_font_bold, accent)
        for i, pos in enumerate(POSITIONS):
            val = row[pos]
            _draw_centered(draw, str(val) if val else "-", gs[1 + i] + pos_col_w // 2, y + 5, row_font, text)
        _draw_centered(draw, str(row["total"]) if row["total"] else "-", gs[6] + total_col_w // 2, y + 5, row_font_bold, text)
        y += ROW_H

    y += SECTION_GAP
    _draw_text(draw, (grid_x0, y), "Mins", header_font, accent)
    y += SUBTITLE_H

    seg_x0 = MARGIN + (content_w - seg_w) // 2
    ss = _col_starts(seg_x0, seg_widths)

    _draw_centered(draw, "Min", ss[0] + seg_minutes_col_w // 2, y, header_font, accent)
    for i, pos in enumerate(POSITIONS):
        _draw_centered(draw, pos, ss[1 + i] + seg_name_col_w // 2, y, header_font, accent)
    y += HEADER_H

    for seg in segments:
        _draw_centered(draw, str(seg["minutes"]), ss[0] + seg_minutes_col_w // 2, y + 5, row_font_bold, text)
        for i, pos in enumerate(POSITIONS):
            nickname = seg["positions"][pos]["nickname"]
            _draw_centered(draw, nickname, ss[1 + i] + seg_name_col_w // 2, y + 5, row_font, text)
        y += ROW_H

    if logo_bytes:
        try:
            logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            max_dim = max(min(LOGO_PANEL_W - 30, H - 2 * MARGIN), 40)
            logo.thumbnail((max_dim, max_dim), Image.LANCZOS)
            lx = W - MARGIN - logo.width
            ly = H - MARGIN - logo.height
            img.paste(logo, (lx, ly), logo)
        except Exception as e:
            print(f"[render] Failed to composite team logo: {e}")

    return _to_png_bytes(img)


def render_minutes_chart(team_name: str, player_totals: list[dict]) -> bytes:
    rows = [r for r in player_totals if r["total"] > 0]

    MARGIN = 32
    ROW_H = 42
    BAR_H = 22
    TITLE_H = 54
    label_w = 190
    value_w = 60
    max_bar_w = 360

    W = MARGIN * 2 + label_w + max_bar_w + value_w
    H = MARGIN * 2 + TITLE_H + max(len(rows), 1) * ROW_H

    img = Image.new("RGB", (W, H), _hex(SURFACE))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(22, bold=True)
    label_font = _load_font(15)
    value_font = _load_font(15, bold=True)

    _draw_text(draw, (MARGIN, MARGIN), f"{team_name} — Minutes by Player", title_font, _hex(INK))

    if not rows:
        _draw_text(draw, (MARGIN, MARGIN + TITLE_H), "No minutes logged.", label_font, _hex(INK_SECONDARY))
        return _to_png_bytes(img)

    max_val = max(r["total"] for r in rows)
    y = MARGIN + TITLE_H
    bar_x0 = MARGIN + label_w

    for i, row in enumerate(rows):
        color = _hex(BAR_PALETTE[i % len(BAR_PALETTE)])
        bar_w = max(6, round(max_bar_w * row["total"] / max_val))
        by0 = y + (ROW_H - BAR_H) // 2
        by1 = by0 + BAR_H
        bx1 = bar_x0 + bar_w

        draw.rounded_rectangle(
            [bar_x0, by0, bx1, by1], radius=4, fill=color,
            corners=(False, True, True, False),
        )

        _draw_vcenter(draw, (MARGIN, y), row["player"], ROW_H, label_font, _hex(INK))
        _draw_vcenter(draw, (bx1 + 8, y), str(row["total"]), ROW_H, value_font, _hex(INK_SECONDARY))
        y += ROW_H

    return _to_png_bytes(img)
