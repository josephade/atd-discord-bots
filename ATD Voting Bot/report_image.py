"""A single lossless team report, sized to include every voter."""
import io
import re
from functools import lru_cache
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageColor, ImageFilter
from render import font
from team_report import ALIASES


NBA_COLORS = dict(zip(ALIASES.values(), [
    ('#E03A3E', '#FFC72C'), ('#007A33', '#BA9653'), ('#202020', '#FFFFFF'),
    ('#1D1160', '#00C4CC'), ('#CE1141', '#FFFFFF'), ('#6F263D', '#FFB81C'),
    ('#00538C', '#B8C4CA'), ('#0E2240', '#FEC524'), ('#1D42BA', '#ED174C'),
    ('#1D428A', '#FFC72C'), ('#CE1141', '#C4CED4'), ('#002D62', '#FDBB30'),
    ('#C8102E', '#1D428A'), ('#552583', '#FDB927'), ('#5D76A9', '#F5B112'),
    ('#98002E', '#F9A01B'), ('#00471B', '#EEE1C6'), ('#0C2340', '#78BE20'),
    ('#0C2340', '#C8102E'), ('#006BB6', '#F58426'), ('#007AC1', '#EF3B24'),
    ('#0077C0', '#C4CED4'), ('#006BB6', '#ED174C'), ('#1D1160', '#E56020'),
    ('#E03A3E', '#FFFFFF'), ('#5A2D81', '#C4CED4'), ('#202020', '#C4CED4'),
    ('#CE1141', '#FFFFFF'), ('#002B5C', '#F9A01B'), ('#002B5C', '#E31837')]))


@lru_cache(maxsize=100)
def nba_logo(team):
    code = next((code for code, name in ALIASES.items() if name == team), None)
    if code is None:
        return None
    code = {'gsw': 'gs', 'nop': 'no', 'sas': 'sa', 'nyk': 'ny', 'uta': 'utah'}.get(code, code)
    try:
        request = Request(f'https://a.espncdn.com/i/teamlogos/nba/500/{code}.png', headers={'User-Agent': 'ATD Voting Bot'})
        with urlopen(request, timeout=5) as response:
            return response.read(2_000_000)
    except (OSError, ValueError):
        return None


def logo_image(data):
    if data:
        try:
            image = Image.open(io.BytesIO(data)).convert('RGBA')
            image.thumbnail((136, 136), Image.Resampling.LANCZOS)
            return image
        except (OSError, ValueError):
            pass
    return None


def palette(team, logo):
    if team in NBA_COLORS:
        return NBA_COLORS[team]
    # Historic/college teams take their colours from the server's actual logo.
    if logo is not None:
        reduced = logo.resize((64, 64)).convert('RGBA')
        pixels = [p[:3] for p in reduced.getdata() if p[3] > 180 and max(p[:3])-min(p[:3]) > 40 and 50 < max(p[:3]) < 250]
        if pixels:
            strip = Image.new('RGB', (len(pixels), 1)); strip.putdata(pixels)
            color = strip.quantize(colors=4).convert('RGB').getcolors(len(pixels))
            rgb = max(color, key=lambda item: item[0])[1]
            return '#%02x%02x%02x' % rgb, '#FFFFFF'
    return '#315482', '#8ABCF5'


def blend(a, b, weight):
    a, b = ImageColor.getrgb(a), ImageColor.getrgb(b)
    return tuple(round(x*(1-weight)+y*weight) for x, y in zip(a, b))


def render_team_report(draft, report, logo_bytes=None, anonymous=False):
    logo = logo_image(logo_bytes)
    primary, accent = palette(report['team'], logo)
    bg, panel = blend('#0B1019', primary, .12), blend('#121B29', primary, .12)
    ink, muted = '#F4F6FA', '#AFBDCF'
    width, margin, gap, column = 1500, 44, 24, 694
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    def voter_label(vote):
        name = 'Hidden voter' if anonymous else vote['name']
        if 'calculated_value' in vote:
            return f"{name}({vote['calculated_value'] if vote['calculated_value'] is not None else '—'})"
        return name

    def draw_voter_text(xy, text, size=23):
        d.text(xy, text, font=font(size), fill=ink)
        if anonymous:
            # Blur a neutral placeholder, never recoverable real names.
            for match in re.finditer('Hidden voter', text):
                x = xy[0]+measure.textlength(text[:match.start()], font=font(size))
                right = x+measure.textlength('Hidden voter', font=font(size))
                box = (int(x)-2, int(xy[1]), int(right)+2, int(xy[1])+size+7)
                image.paste(image.crop(box).filter(ImageFilter.GaussianBlur(6)), box)

    def wrap(text, max_width, size=23):
        # Character wrapping also handles long unbroken names without clipping.
        lines, line = [], ''
        for char in text.replace('\n', ' '):
            if line and measure.textlength(line+char, font=font(size)) > max_width:
                lines.append(line); line = ''
            line += char
        return lines+[line]

    groups = []
    for tier in range(1, report['tiers']+1):
        entries = [(v, wrap(voter_label(v), column-108)) for v in report['votes'] if v['tier'] == tier]
        height = 68+sum(max(38, 29*len(lines)+10) for _, lines in entries)+16
        groups.append((tier, entries, max(118, height)))
    standouts = [v for v in report['votes'] if v['standout']]
    standout_lines = wrap('  /  '.join(f"{'Hidden voter' if anonymous else v['name']} · Tier {v['tier']}" for v in standouts) or 'No unusually high or low votes in this report.', width-2*margin-48)
    callout_height = 74+29*len(standout_lines)
    top = 428+callout_height
    body_height = sum(max(g[2] for g in groups[i:i+2])+gap for i in range(0, len(groups), 2))
    has_values = any('calculated_value' in v for v in report['votes'])
    height = top+body_height+106+(44 if has_values else 0)
    image = Image.new('RGB', (width, height), bg)
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, width, 9), fill=accent)
    d.rounded_rectangle((44, 42, 208, 206), radius=30, fill=blend('#162030', primary, .3))
    if logo:
        image.paste(logo, (126-logo.width//2, 124-logo.height//2), logo)
    else:
        d.text((80, 88), 'ATD', font=font(38, True), fill=accent)
    d.text((238, 46), f'ATD {draft}  /  TEAM VOTING REPORT', font=font(20, True), fill=muted)
    title = report['team']; title_size = 44
    while measure.textlength(title, font=font(title_size, True)) > 1160 and title_size > 24:
        title_size -= 1
    d.text((238, 83), title, font=font(title_size, True), fill=ink)
    d.text((238, 151), f"{report['tiers']} tiers · Original voter selections", font=font(24), fill=muted)
    stats = [(str(len(report['votes'])), 'VOTERS'), (f"{report['average']:.2f}", 'AVERAGE TIER'),
             (f"{report['median']:g}", 'MEDIAN TIER'), (str(len(standouts)), 'STANDOUTS')]
    for i, (value, label) in enumerate(stats):
        x = margin+i*359
        d.rounded_rectangle((x, 231, x+335, 326), radius=16, fill=panel)
        d.text((x+20, 240), value, font=font(36, True), fill=ink)
        d.text((x+20, 289), label, font=font(15, True), fill=muted)
    tile_w = (width-margin*2-(report['tiers']-1)*10)/report['tiers']
    for i, n in enumerate(report['counts']):
        x = round(margin+i*(tile_w+10))
        d.rounded_rectangle((x, 346, x+tile_w, 404), radius=12, fill=blend('#192231', primary, .40 if n else .12))
        d.text((x+14, 353), f'T{i+1}', font=font(15, True), fill=muted)
        d.text((x+14, 374), str(n), font=font(21, True), fill=ink)
    d.rounded_rectangle((margin, 424, width-margin, 424+callout_height), radius=18, fill=blend('#192231', primary, .3), outline=accent, width=2)
    d.text((margin+24, 438), f'STANDOUT VOTES  ·  {len(standouts)}', font=font(18, True), fill=ink)
    for i, line in enumerate(standout_lines):
        draw_voter_text((margin+24, 475+i*29), line)
    y = top+24
    for pair_start in range(0, len(groups), 2):
        pair = groups[pair_start:pair_start+2]
        row_height = max(g[2] for g in pair)
        for col, (tier, entries, _) in enumerate(pair):
            x = margin+col*(column+gap)
            d.rounded_rectangle((x, y, x+column, y+row_height), radius=18, fill=panel)
            d.text((x+22, y+16), f'TIER {tier}', font=font(22, True), fill=ink)
            count = sum(report['counts'])
            d.text((x+470, y+20), f"{len(entries)} votes · {len(entries)/count:.0%}", font=font(18), fill=muted)
            vy = y+64
            if not entries:
                d.text((x+22, vy), 'No votes', font=font(22), fill=muted)
            for voter, lines in entries:
                row_h = max(38, 29*len(lines)+10)
                if voter['standout']:
                    d.rounded_rectangle((x+12, vy-2, x+column-12, vy+row_h-4), radius=8, fill=blend('#1E2838', primary, .5))
                    d.text((x+24, vy), '!', font=font(21, True), fill=accent)
                else:
                    d.text((x+24, vy), '·', font=font(23), fill=muted)
                for li, line in enumerate(lines):
                    draw_voter_text((x+58, vy+li*29), line)
                vy += row_h
        y += row_height+gap
    d.text((margin, y+6), 'Tier 1 = best  ·  Latest valid selection per voter  ·  Highlighted rows = standouts', font=font(19), fill=muted)
    d.text((margin, y+37), 'Standout: beyond Q1–Q3 by more than max(2 tiers, 1.5 × IQR); minimum 5 voters.', font=font(18), fill=muted)
    d.text((margin, y+65), 'Source: current Google Form response sheet  /  Deleted responses excluded', font=font(17), fill=muted)
    if has_values:
        d.text((margin, y+93), 'Name(value) = second-table calculated value  /  — = value unavailable', font=font(17), fill=muted)
    output = io.BytesIO(); image.save(output, format='PNG')
    return output.getvalue()
