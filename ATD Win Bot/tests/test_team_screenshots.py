"""Regression coverage using real Pillow images and in-memory PDF exports."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import fitz
from PIL import Image, ImageChops, ImageDraw


def screenshot_namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    wanted = {'_autocrop_whitespace', '_export_team_range_png', '_team_export_range'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]

    def a1(row, col):
        letters = ''
        while col:
            col, rem = divmod(col - 1, 26)
            letters = chr(65 + rem) + letters
        return f'{letters}{row}'

    ns = dict(Image=Image, ImageChops=ImageChops, fitz=fitz,
              requests=NS(get=Mock()), EMOJI_TEAM_MAP={},
              _hidden_columns_for_ws=lambda ws: {3},
              gspread=NS(utils=NS(rowcol_to_a1=a1)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


class TeamScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.ns = screenshot_namespace()

    def test_pale_team_fills_keep_the_entire_cell_block(self):
        # The previous threshold of 60 discarded the peach fill in ATD 109.
        for colour in ('#ffc699', '#f0f0f0', '#ffffcc'):
            with self.subTest(colour=colour):
                img = Image.new('RGB', (400, 400), 'white')
                draw = ImageDraw.Draw(img)
                draw.rectangle((50, 60, 349, 339), fill=colour)
                draw.text((80, 80), 'Atlanta Hawks       Year', fill='#d30b32')
                result = self.ns['_autocrop_whitespace'](img)
                self.assertEqual(result.size, (300, 280))
                self.assertEqual(result.getpixel((0, 0)), img.getpixel((50, 60)))
                self.assertEqual(result.getpixel((299, 279)), img.getpixel((349, 339)))

    def test_unfilled_sheet_does_not_add_padding(self):
        img = Image.new('RGB', (100, 100), 'white')
        ImageDraw.Draw(img).rectangle((25, 30, 74, 69), fill='black')
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (50, 40))
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0))

    def test_blank_page_and_faint_raster_fringe(self):
        img = Image.new('RGB', (100, 100), '#fdfdfd')
        self.assertEqual(self.ns['_autocrop_whitespace'](img).size, img.size)
        ImageDraw.Draw(img).rectangle((30, 30, 69, 69), fill='black')
        self.assertEqual(self.ns['_autocrop_whitespace'](img).size, (40, 40))

    def test_blue_roster_preserves_internal_cell_lines_without_outer_frame(self):
        img = Image.new('RGB', (400, 400), 'white')
        draw = ImageDraw.Draw(img)
        draw.rectangle((50, 60, 349, 339), fill='#4a86e8')
        draw.line((50, 100, 349, 100), fill='white', width=2)
        draw.line((280, 60, 280, 339), fill='white', width=2)
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (300, 280))
        self.assertEqual(result.getpixel((0, 0)), (74, 134, 232))
        self.assertEqual(result.getpixel((299, 279)), (74, 134, 232))
        self.assertEqual(result.getpixel((100, 40)), (255, 255, 255))

    def test_range_matches_short_and_long_rosters_and_stops_before_next_team(self):
        for count in (0, 5, 10, 13):
            with self.subTest(count=count):
                data = [['Hawks', 'Year', 'Price', 'Celtics', 'Year']]
                data += [[f'Player {i}', '2023-24', '10'] for i in range(count)]
                data += [['Next team', 'Year'], ['Someone else', '2020-21']]
                result = self.ns['_team_export_range'](NS(), data, 1, 1)
                self.assertEqual(result, f'A1:B{count + 1}')

    def test_visible_price_column_and_offset_range(self):
        self.ns['_hidden_columns_for_ws'] = lambda ws: set()
        data = [[], ['', '', 'Hawks', 'Year', 'Price', 'Celtics', 'Year'],
                ['', '', 'Rose', '2010-11', '40'], []]
        self.assertEqual(self.ns['_team_export_range'](NS(), data, 2, 3), 'C2:E3')

    def test_pdf_export_keeps_every_page(self):
        with fitz.open() as doc:
            for colour in ((1, 0, 0), (0, 0, 1)):
                page = doc.new_page(width=100, height=100)
                page.draw_rect(fitz.Rect(20, 20, 80, 80), color=None, fill=colour)
            pdf = doc.tobytes()
        response = NS(content=pdf, raise_for_status=Mock())
        self.ns['requests'].get.return_value = response
        result = self.ns['_export_team_range_png']('sheet', 123, 'A1:B14', 'token')
        colours = {colour for _, colour in result.getcolors(result.width * result.height)}
        self.assertIn((255, 0, 0), colours)
        self.assertIn((0, 0, 255), colours)
        params = self.ns['requests'].get.call_args.kwargs['params']
        self.assertEqual(params['scale'], '4')
        self.assertEqual(params['range'], 'A1:B14')
        response.raise_for_status.assert_called_once()

    def test_failed_export_is_not_treated_as_an_image(self):
        self.ns['requests'].get.return_value.raise_for_status.side_effect = RuntimeError('export failed')
        with self.assertRaisesRegex(RuntimeError, 'export failed'):
            self.ns['_export_team_range_png']('sheet', 123, 'A1:B11', 'token')


if __name__ == '__main__':
    unittest.main()
