"""Regression tests for the PDF raster fringe in !teamshot and !matchup."""
import ast
import io
from pathlib import Path
import unittest

from PIL import Image, ImageChops, ImageDraw


def namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    wanted = {'_trim_export_fringe', '_autocrop_whitespace', '_compose_matchup_image'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = dict(Image=Image, ImageChops=ImageChops, io=io)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    return ns


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()

    def test_portland_grey_bottom_row_is_removed(self):
        img = Image.new('RGB', (558, 511), 'black')
        ImageDraw.Draw(img).line((0, 510, 557, 510), fill=(171, 171, 171))
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (558, 510))
        self.assertEqual(result.crop((0, 509, 558, 510)).getcolors(), [(558, (0, 0, 0))])

    def test_fringe_and_pdf_margin_on_all_sides(self):
        img = Image.new('RGB', (120, 100), 'white')
        draw = ImageDraw.Draw(img)
        draw.rectangle((9, 9, 110, 90), fill='#ababab')
        draw.rectangle((10, 10, 109, 89), fill='black')
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (100, 80))
        self.assertEqual(result.getcolors(), [(8000, (0, 0, 0))])

    def test_pale_fills_and_text_are_preserved(self):
        for fill in ('#ffc699', '#f0f0f0', '#ffffcc'):
            with self.subTest(fill=fill):
                img = Image.new('RGB', (120, 100), 'white')
                draw = ImageDraw.Draw(img)
                draw.rectangle((10, 10, 109, 89), fill=fill)
                draw.text((15, 15), 'Roster', fill='red')
                result = self.ns['_autocrop_whitespace'](img)
                self.assertEqual(result.size, (100, 80))
                self.assertEqual(result.getpixel((99, 79)), img.getpixel((109, 89)))

    def test_unfilled_sheet_text_is_not_trimmed(self):
        img = Image.new('RGB', (120, 100), 'white')
        draw = ImageDraw.Draw(img)
        draw.rectangle((10, 10, 30, 30), fill='black')
        draw.rectangle((80, 70, 109, 89), fill='red')
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (100, 80))
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(result.getpixel((99, 79)), (255, 0, 0))

    def test_matchup_has_no_bottom_white_strip(self):
        images = []
        for height in (100, 130):
            img = Image.new('RGB', (100, height), 'black')
            ImageDraw.Draw(img).line((0, height - 1, 99, height - 1), fill='#ababab')
            images.append(self.ns['_autocrop_whitespace'](img))
        combined = Image.open(io.BytesIO(self.ns['_compose_matchup_image'](*images)))
        self.assertEqual(combined.crop((0, combined.height - 1, combined.width, combined.height)).getcolors(),
                         [(combined.width, (0, 0, 0))])

    def test_rockets_neighbor_colour_and_text_fringe_is_removed(self):
        img = Image.new('RGB', (559, 551), (195, 17, 65))
        draw = ImageDraw.Draw(img)
        draw.text((510, 30), '2025', fill='white')
        draw.rectangle((556, 0, 558, 550), fill=(74, 134, 232))
        draw.rectangle((556, 40, 558, 65), fill='white')
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.size, (556, 551))
        self.assertEqual(result.tobytes(), img.crop((0, 0, 556, 551)).tobytes())

    def test_clean_target_edge_is_not_shaved(self):
        img = Image.new('RGB', (556, 551), (195, 17, 65))
        ImageDraw.Draw(img).text((520, 20), '88', fill='white')
        result = self.ns['_autocrop_whitespace'](img)
        self.assertEqual(result.tobytes(), img.tobytes())

    def test_substantial_neighbor_is_not_guessed_away(self):
        img = Image.new('RGB', (559, 551), (195, 17, 65))
        ImageDraw.Draw(img).rectangle((549, 0, 558, 550), fill='blue')
        self.assertEqual(self.ns['_trim_export_fringe'](img).size, img.size)


if __name__ == '__main__':
    unittest.main()
