"""Real PDFium tile origins preserve CropBox/rotation geometry and pixels."""
import unittest
from lab_parser.escalation import EscalationPolicy, plan_next
from lab_parser.render import render_region
from pypdf import PdfWriter
from pypdf.generic import RectangleObject, DecodedStreamObject, NameObject
import pypdfium2
import io
import zlib


def rgb(asset):
    png = asset.png
    pos, compressed = 8, b''
    while pos < len(png):
        length = int.from_bytes(png[pos:pos + 4], 'big')
        if png[pos + 4:pos + 8] == b'IDAT':
            compressed += png[pos + 8:pos + 8 + length]
        pos += length + 12
    raw = zlib.decompress(compressed)
    stride = asset.width_px * 3 + 1
    assert all(raw[y * stride] == 0 for y in range(asset.height_px))
    return b''.join(raw[y * stride + 1:(y + 1) * stride] for y in range(asset.height_px))


class TileOriginIntegrationTests(unittest.TestCase):
    def test_exact_rgb_slices_for_nonzero_cropbox_and_all_rotations(self):
        for rotation in (0, 90, 180, 270):
            writer = PdfWriter()
            page = writer.add_blank_page(width=700, height=600)
            page.cropbox = RectangleObject((11, 23, 650, 570))
            page.rotate(rotation)
            stream = DecodedStreamObject()
            stream.set_data(b'\n'.join(
                f'{(i % 3) / 2} {(i % 5) / 4} {(i % 7) / 6} rg {i * 7} 0 4 600 re f'.encode()
                for i in range(100)
            ))
            page[NameObject('/Contents')] = writer._add_object(stream)
            output = io.BytesIO()
            writer.write(output)
            data = output.getvalue()
            document = pypdfium2.PdfDocument(data)
            page = document[0]
            size = page.get_size()
            page.close()
            document.close()
            box = (.123, .171, .889, .932)
            policy = EscalationPolicy(max_side_px=700, max_pixels=490000, max_tiles=64)
            plan = plan_next([], 'none', box=box, page_size_pt=size, policy=policy)
            whole = render_region(data, 0, box, 150)
            assert plan.action == 'render'
            assert (plan.width_px, plan.height_px) == (whole.width_px, whole.height_px)
            assert len(plan.tiles) == 4
            whole_rgb = rgb(whole)
            for index, tile in enumerate(plan.tiles):
                asset = render_region(data, 0, tile.box, 150)
                assert (asset.width_px, asset.height_px) == (tile.width_px, tile.height_px)
                ox, oy = tile.origin_px
                expected = b''.join(
                    whole_rgb[((oy + y) * whole.width_px + ox) * 3:
                              ((oy + y) * whole.width_px + ox + tile.width_px) * 3]
                    for y in range(tile.height_px)
                )
                assert rgb(asset) == expected, (rotation, index, tile.origin_px)



if __name__ == "__main__":
    unittest.main()
