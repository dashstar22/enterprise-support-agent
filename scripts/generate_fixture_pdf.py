"""Generate the deterministic synthetic PDF fixture used by C3-T01."""

from __future__ import annotations

from pathlib import Path

OUTPUT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "e200-synthetic-safety-notice.pdf"
)


def build_pdf() -> bytes:
    """Create a small valid PDF without external libraries or clock-dependent metadata."""
    content = """BT
/F1 16 Tf
72 720 Td
(E-200 Synthetic Safety Notice) Tj
/F1 11 Tf
0 -28 Td
(For retrieval fixture only. This is not a real maintenance instruction.) Tj
0 -22 Td
(Fault code E01: inspect the main power supply and fuse before escalation.) Tj
0 -22 Td
(If the equipment remains unavailable, stop and hand off to technical support.) Tj
ET
""".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    current_offset = len(parts[0])
    for number, body in enumerate(objects, start=1):
        offsets.append(current_offset)
        object_bytes = f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        parts.append(object_bytes)
        current_offset += len(object_bytes)

    xref_offset = current_offset
    xref_rows = [b"0000000000 65535 f \n"]
    xref_rows.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    parts.extend(
        [
            b"xref\n0 6\n",
            b"".join(xref_rows),
            b"trailer\n<< /Size 6 /Root 1 0 R >>\n",
            b"startxref\n" + str(xref_offset).encode("ascii") + b"\n%%EOF\n",
        ]
    )
    return b"".join(parts)


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(build_pdf())


if __name__ == "__main__":
    main()
