import io
import os
from PIL import Image, ImageDraw, ImageFont


def font(size, bold=False):
    paths = [r'C:\Windows\Fonts\segoeuib.ttf' if bold else r'C:\Windows\Fonts\segoeui.ttf',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_tierlist(draft, ballot, tier_count=10):
    normal, heading = font(21), font(28, True)
    width, label_w, pad = 1200, 130, 24
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    count = max(tier_count, max((tier for _, tier in ballot.teams), default=1))
    rows = []
    for tier in range(1, count+1):
        lines, current = [], ''
        for team, chosen in ballot.teams:
            if chosen != tier:
                continue
            candidate = f'{current}   |   {team}' if current else team
            if measure.textlength(candidate, font=normal) > width-label_w-pad*2 and current:
                lines.append(current)
                current = team
            else:
                current = candidate
        lines.append(current or '—')
        rows.append((tier, lines, max(60, 26*len(lines)+24)))
    image = Image.new('RGB', (width, 130+sum(h for _, _, h in rows)+42), '#111827')
    draw = ImageDraw.Draw(image)
    draw.text((pad, 20), f'ATD {draft} • Your tier list', font=heading, fill='white')
    subtitle = f'{ballot.name or "CSV preview"} • {ballot.timestamp}'
    while measure.textlength(subtitle, font=normal) > width-2*pad:
        subtitle = subtitle[:-5] + '…'
    draw.text((pad, 66), subtitle, font=normal, fill='#cbd5e1')
    colors = ['#86efac', '#a3e635', '#facc15', '#fb923c', '#f87171']
    y = 120
    for tier, lines, height in rows:
        color = colors[min(4, (tier-1)*5//count)]
        draw.rectangle((0, y, label_w, y+height-2), fill=color)
        draw.rectangle((label_w+2, y, width, y+height-2), fill='#1f2937')
        draw.text((16, y+(height-26)//2), f'Tier {tier}', font=normal, fill='#111827')
        for j, line in enumerate(lines):
            draw.text((label_w+pad, y+12+j*26), line, font=normal, fill='white')
        y += height
    draw.text((pad, y+10), 'Tier 1 = best • Your original selections', font=font(17), fill='#cbd5e1')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
