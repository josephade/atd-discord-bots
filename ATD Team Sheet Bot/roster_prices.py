"""Keep roster prices numeric when moving players between sheet slots."""
import math


def numeric_price(value):
    if value is None or value == '':
        return ''
    text = str(value).strip().replace('$', '').replace(',', '')
    if not text:
        return ''
    try:
        number = float(text)
    except (ValueError, TypeError):
        raise ValueError('A roster price is invalid. Ask the commissioner to check it before swapping.') from None
    if not math.isfinite(number):
        raise ValueError('A roster price is invalid. Ask the commissioner to check it before swapping.')
    return number


def read_prices(manager, ranges):
    # Formatted values ("$9") written with RAW become text, which ignores
    # number formatting and can disappear from SUM totals. Read the numbers.
    cells = manager._call('batch_get', ranges, value_render_option='UNFORMATTED_VALUE')
    if len(cells) != len(ranges):
        raise ValueError('Could not read both roster prices. No swap was made.')
    return [numeric_price(cell[0][0] if cell and cell[0] else '') for cell in cells]
