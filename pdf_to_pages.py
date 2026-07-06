#!/usr/bin/env python3
"""
Convert a folder of PDFs into per-page PNGs with .txt sidecars.

For each PDF in the input folder (e.g. COMIC1.pdf), creates a subfolder
(COMIC1/) containing one PNG per page (COMIC1 PAGE1.PNG, COMIC1 PAGE2.PNG, ...)
and a matching .txt sidecar for each PNG (COMIC1 PAGE1.PNG.txt, ...) with:

    comic:COMIC1
    page:1

Usage:
    python pdf_to_pages.py /path/to/pdf_folder [/path/to/output_folder] [--dpi 300]

If no output folder is given, subfolders are created inside the input folder.
"""

import argparse
import sys
from pathlib import Path
from typing import List

try:                       # PyMuPDF renamed its import to `pymupdf`; keep both working.
    import pymupdf as fitz
except ImportError:
    import fitz  # PyMuPDF


def convert_pdf(pdf_path: Path, output_root: Path, dpi: int = 300,
                write_sidecars: bool = True) -> List[Path]:
    """Render every page of ``pdf_path`` to a PNG under ``output_root/<stem>/``.

    Returns the list of PNG paths written. When ``write_sidecars`` is True (the
    default, used by the standalone CLI) each PNG also gets a ``comic:``/``page:``
    ``.TXT`` sidecar; FurTag renders with it True so the base tags land in the
    sidecar it later enriches perceptually.
    """
    stem = pdf_path.stem
    out_dir = output_root / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  ! Failed to open {pdf_path.name}: {e}", file=sys.stderr)
        return []

    generated: List[Path] = []
    for i, page in enumerate(doc, start=1):
        base_name = f"{stem} PAGE{i}.PNG"
        png_path = out_dir / base_name
        # Lowercase ".txt" to match FurTag's <file>.<ext>.txt sidecar convention,
        # so its perceptual tags append to this same file on any filesystem.
        txt_path = out_dir / f"{base_name}.txt"

        pix = page.get_pixmap(dpi=dpi)
        pix.save(png_path)
        generated.append(png_path)

        if write_sidecars:
            txt_path.write_text(f"comic:{stem}\npage:{i}\n", encoding="utf-8")

    doc.close()
    print(f"  {pdf_path.name}: {len(generated)} page(s) -> {out_dir}")
    return generated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_folder", type=Path, help="Folder containing PDF files")
    parser.add_argument(
        "output_folder",
        type=Path,
        nargs="?",
        default=None,
        help="Folder where per-comic subfolders are created (default: input folder)",
    )
    parser.add_argument(
        "--dpi", type=int, default=300, help="Resolution for rendered pages (default: 300)"
    )
    args = parser.parse_args()

    input_folder: Path = args.input_folder
    output_folder: Path = args.output_folder or input_folder

    if not input_folder.is_dir():
        print(f"Error: {input_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    output_folder.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(input_folder.glob("*.pdf")) + sorted(input_folder.glob("*.PDF"))
    pdfs = sorted(set(pdfs))

    if not pdfs:
        print(f"No PDFs found in {input_folder}")
        return

    print(f"Found {len(pdfs)} PDF(s) in {input_folder}")
    for pdf_path in pdfs:
        convert_pdf(pdf_path, output_folder, args.dpi)

    print("Done.")


if __name__ == "__main__":
    main()
