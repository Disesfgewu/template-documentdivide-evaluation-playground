"""Deterministic synthetic text PDF generator (fixture F01, "insufficient
material" variant). Pure standard library; byte-identical output for the
same arguments, so the registry can pin sha256.

Every page carries different text (page number, a per-page sentence and a
per-page hash token), so a large PDF is never one page copied N times.
Usage: python -m fixtures.generators.synthetic_pdf <pages> <out.pdf>
"""
import hashlib
import sys

PAGE_W, PAGE_H = 595, 842  # A4 in points


def _page_text(i: int) -> list[str]:
    token = hashlib.sha256(f"lab-fixture-F01-page-{i}".encode()).hexdigest()[:16]
    return [f"LAB synthetic fixture F01 - page {i + 1}",
            f"Section {i // 10 + 1}, paragraph {i % 10 + 1}.",
            f"Measured value for item {i + 1} is {(i * 37) % 997} units.",
            f"Page token {token}"]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build(pages: int) -> bytes:
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 2000:
        raise ValueError("pages must be an integer in [1, 2000]")
    objs: list[bytes] = []  # objs[k] is object number k+1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")  # placeholder, filled after pages object exists
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for i in range(pages):
        lines = _page_text(i)
        ops = ["BT", "/F1 12 Tf", "72 770 Td", "16 TL"]
        for ln in lines:
            ops.append(f"({_escape(ln)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("ascii")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                        b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (pages_obj, PAGE_W, PAGE_H, font, content)))
    objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objs[pages_obj - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                           + b"] /Count %d >>" % pages)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    data = build(int(argv[0]))
    with open(argv[1], "wb") as f:
        f.write(data)
    print(hashlib.sha256(data).hexdigest(), argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
