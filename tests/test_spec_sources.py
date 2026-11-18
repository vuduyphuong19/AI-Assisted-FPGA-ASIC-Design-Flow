"""Tests for tools/drawio_text.py (draw.io spec sources -> citable Markdown).
Run: python3 -m unittest discover -s tests -v"""
import base64
import html
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import drawio_text  # noqa: E402

MODEL = """<mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="grp" value="sync_fifo" style="swimlane" vertex="1" parent="1"/>
<mxCell id="wr" value="&lt;b&gt;WRITE&lt;/b&gt;&lt;br&gt;ptr++" vertex="1" parent="grp"/>
<UserObject id="rd" label="READ" req="REQ-3"><mxCell vertex="1" parent="grp"/></UserObject>
<mxCell id="e1" value="push &amp;amp;&amp;amp; !full" edge="1" parent="1" source="wr" target="rd"/>
<mxCell id="e2" edge="1" parent="1" source="rd" target="wr"/>
<mxCell id="e2t" value="pop" vertex="1" connectable="0" parent="e2"/>
<mxCell id="e3" value="flush" edge="1" parent="1" source="rd"/>
</root></mxGraphModel>"""


def compress(xml: str) -> str:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(c.compress(urllib.parse.quote(xml).encode()) + c.flush()).decode()


class DrawioText(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def check(self, out):
        self.assertIn("| wr | WRITE / ptr++ | sync_fifo [grp] |", out)
        self.assertIn("| rd | READ | sync_fifo [grp] | req=REQ-3 |", out)
        self.assertIn("| e1 | WRITE / ptr++ [wr] | READ [rd] | push && !full |", out)
        self.assertIn("| e2 | READ [rd] | WRITE / ptr++ [wr] | pop |", out)    # label from child text cell
        self.assertNotIn("| e2t |", out)                                         # edge label is not a shape
        self.assertIn("missing end", out)
        self.assertIn("- e3: from READ [rd] to (none), label \"flush\"", out)

    def test_plain(self):
        out = drawio_text.render(self.write("a.drawio", f'<mxfile><diagram name="FSM" id="p">{MODEL}</diagram></mxfile>'))
        self.assertIn("## Page: FSM", out)
        self.check(out)

    def test_compressed_multi_page(self):
        p = self.write("b.drawio", f'<mxfile><diagram name="Block">{compress(MODEL)}</diagram>'
                                   f'<diagram name="Two">{MODEL}</diagram></mxfile>')
        out = drawio_text.render(p)
        self.assertIn("## Page: Block", out)
        self.assertIn("## Page: Two", out)
        self.check(out)

    def test_bare_model_and_svg(self):
        self.check(drawio_text.render(self.write("c.xml", MODEL)))
        mx = f'<mxfile><diagram name="S">{compress(MODEL)}</diagram></mxfile>'
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" content="{html.escape(mx)}"><g/></svg>'
        self.check(drawio_text.render(self.write("d.drawio.svg", svg)))

    def test_rejects_non_drawio(self):
        bad = self.write("e.svg", '<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>')
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "drawio_text.py"), str(bad)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no embedded draw.io diagram", r.stderr)


if __name__ == "__main__":
    unittest.main()
