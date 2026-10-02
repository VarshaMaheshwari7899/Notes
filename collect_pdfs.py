"""
Copy every PDF already generated under ./downloads into one flat folder.

Usage:  uv run collect_pdfs.py
Result: ./all_pdfs/<Title>.pdf   (if two share a name, the shortcode is added)
"""

import re
import shutil
from pathlib import Path

SOURCE_DIR = Path("downloads")
DEST_DIR = Path("all_pdfs")


def main():
    DEST_DIR.mkdir(exist_ok=True)
    pdfs = sorted(SOURCE_DIR.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found under {SOURCE_DIR}/")
        return

    copied = 0
    for pdf in pdfs:
        dest = DEST_DIR / pdf.name
        if dest.exists():
            # folder looks like "Title [shortcode]" -> take the shortcode
            m = re.search(r"\[([^\]]+)\]$", pdf.parent.name)
            code = m.group(1) if m else pdf.parent.name
            dest = DEST_DIR / f"{pdf.stem} [{code}]{pdf.suffix}"
            if dest.exists():
                continue
        shutil.copy2(pdf, dest)
        copied += 1
        print(f"copied {dest}")

    print(f"\n{copied} PDF(s) in {DEST_DIR.resolve()}")


if __name__ == "__main__":
    main()