import fitz
import re
import sys

PDF_PATH = "../OHE6.pdf"

CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")

pages = [47, 49, 51, 52, 55]

doc = fitz.open(PDF_PATH)

for page_number in pages:
    page = doc[page_number - 1]

    print("\n" + "=" * 100)
    print(f"PAGE {page_number}")
    print("=" * 100)

    words = page.get_text("words")

    for w in words:
        x0, y0, x1, y1, text, block, line, word = w

        matches = CAS_RE.findall(text)

        for cas in matches:
            print(
                f"CAS={cas:<15} "
                f"x={x0:8.2f} "
                f"y={y0:8.2f} "
                f"x1={x1:8.2f} "
                f"y1={y1:8.2f} "
                f"block={block} "
                f"line={line} "
                f"text={text}"
            )