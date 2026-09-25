"""Audiveris OMR service — subprocess wrapper."""
import asyncio
import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

AUDIVERIS_BIN = os.environ.get("AUDIVERIS_BIN", "/opt/audiveris/bin/Audiveris")
TIMEOUT_SECONDS = 900

# Audiveris hard limit: 20 million pixels per sheet.
# We target 18 million to stay safely below it.
AUDIVERIS_MAX_PIXELS = 18_000_000


def _extract_mxl(mxl_path: Path) -> Path:
    """Extract a .mxl (compressed MusicXML) to a .xml file next to it."""
    xml_path = mxl_path.with_suffix(".xml")
    with zipfile.ZipFile(mxl_path) as z:
        score_entry = None
        for name in z.namelist():
            if name.endswith(".xml") and not name.startswith("META-INF"):
                score_entry = name
                break
        if score_entry is None:
            raise RuntimeError(f"No XML found inside {mxl_path}")
        xml_path.write_bytes(z.read(score_entry))
    logger.info("Extracted %s -> %s", mxl_path, xml_path)
    return xml_path


def _get_pdf_page_size_pts(pdf_path: Path) -> tuple[float, float] | None:
    """Return (width_pt, height_pt) of the first page using gs, or None on error."""
    try:
        result = subprocess.run(
            [
                "gs", "-dNOPAUSE", "-dBATCH", "-dQUIET",
                "-sDEVICE=bbox",
                "-dLastPage=1",
                str(pdf_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        # gs bbox output goes to stderr: "%%BoundingBox: x0 y0 x1 y1"
        for line in result.stderr.splitlines():
            if line.startswith("%%HiResBoundingBox:") or line.startswith("%%BoundingBox:"):
                parts = line.split()
                if len(parts) == 5:
                    w = float(parts[3]) - float(parts[1])
                    h = float(parts[4]) - float(parts[2])
                    return w, h
    except Exception as exc:
        logger.warning("gs bbox failed: %s", exc)
    return None


def _get_pdf_page_count(pdf_path: Path) -> int | None:
    """Return the number of pages in a PDF using ghostscript, or None on error."""
    try:
        result = subprocess.run(
            [
                "gs", "-dNOPAUSE", "-dBATCH", "-dQUIET",
                "-sDEVICE=bbox",
                str(pdf_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        count = sum(1 for line in result.stderr.splitlines() if line.startswith("%%BoundingBox:"))
        return count or None
    except Exception as exc:
        logger.warning("gs page count failed: %s", exc)
        return None


def _safe_dpi_for_page(width_pt: float, height_pt: float) -> int:
    """Return a DPI that keeps the rasterised page under AUDIVERIS_MAX_PIXELS."""
    import math
    w_in = width_pt / 72.0
    h_in = height_pt / 72.0
    # pixels = (w_in * dpi)^2 * area_ratio <= MAX  =>  dpi <= sqrt(MAX / area)
    max_dpi = math.floor(math.sqrt(AUDIVERIS_MAX_PIXELS / (w_in * h_in)))
    # Cap at 300 DPI (above this adds no OMR benefit); no hard lower bound —
    # for very large pages even 80 DPI still gives thousands of pixels per staff.
    return min(300, max(72, max_dpi))


def _render_pdf_to_pngs(pdf_path: Path, output_dir: Path, dpi: int) -> list[Path]:
    """Render every page of a PDF to individual PNGs using ghostscript."""
    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "page-%04d.png")
    result = subprocess.run(
        [
            "gs", "-dNOPAUSE", "-dBATCH", "-dQUIET",
            "-sDEVICE=png16m",
            f"-r{dpi}",
            f"-sOutputFile={template}",
            str(pdf_path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gs render failed: {result.stderr[:500]}")
    pages = sorted(output_dir.glob("page-*.png"))
    if not pages:
        raise RuntimeError("gs produced no PNG pages")
    logger.info("Rendered %d page(s) at %d DPI: %s", len(pages), dpi, output_dir)
    return pages


def _strip_jpeg_trailer(input_file: Path, work_dir: Path) -> Path:
    """Strip a trailing embedded image some phone-camera JPEGs carry after the
    primary photo (Multi-Picture Format secondary/depth shot). Audiveris reads
    each embedded image in the file as a separate sheet and — same as the
    multi-page PDF case above — refuses to export the whole Book if any of
    them fails validation, even though the user only meant to upload one page.
    Returns the original path unchanged if there's nothing to strip.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = work_dir / input_file.name
    try:
        result = subprocess.run(
            ["exiftool", "-trailer:all=", "-o", str(out_path), str(input_file)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        logger.warning("exiftool failed for %s: %s", input_file.name, exc)
        return input_file

    if result.returncode != 0 or not out_path.exists():
        logger.warning("exiftool trailer strip failed for %s: %s", input_file.name, result.stderr[:300])
        return input_file

    if out_path.stat().st_size == input_file.stat().st_size:
        out_path.unlink(missing_ok=True)
        return input_file  # nothing was embedded, no change needed

    logger.info(
        "Stripped embedded trailer image from %s (%d -> %d bytes)",
        input_file.name, input_file.stat().st_size, out_path.stat().st_size,
    )
    return out_path


async def prepare_input(input_file: Path, work_dir: Path) -> list[Path]:
    """
    Return the list of files to feed to Audiveris.

    Rasterises the PDF to per-page PNGs whenever there's more than one page —
    Audiveris refuses to export a multi-sheet Book at all if any single sheet
    fails validation, so splitting multi-page PDFs lets run_omr() process each
    page independently and keep whatever pages succeed. Also rasterises a
    single oversized page to fit Audiveris's pixel limit.
    Returns the same input_file in a list if no pre-processing is needed.
    """
    suffix = input_file.suffix.lower()

    if suffix in (".jpg", ".jpeg"):
        input_file = await asyncio.get_event_loop().run_in_executor(
            None, _strip_jpeg_trailer, input_file, work_dir
        )

    if suffix != ".pdf":
        return [input_file]

    size = _get_pdf_page_size_pts(input_file)
    if size is None:
        return [input_file]

    w_pt, h_pt = size
    dpi = _safe_dpi_for_page(w_pt, h_pt)

    w_in = w_pt / 72.0
    h_in = h_pt / 72.0
    pixels_at_300 = (w_in * 300) * (h_in * 300)
    oversized = pixels_at_300 > AUDIVERIS_MAX_PIXELS

    page_count = _get_pdf_page_count(input_file)
    multi_page = page_count is not None and page_count > 1

    if not oversized and not multi_page:
        return [input_file]

    if oversized:
        logger.info(
            "PDF page %.1f×%.1f in (%.0fM px at 300 DPI) exceeds limit — "
            "rasterising at %d DPI (%.0fM px)",
            w_in, h_in, pixels_at_300 / 1e6, dpi,
            (w_in * dpi) * (h_in * dpi) / 1e6,
        )
    else:
        logger.info(
            "Multi-page PDF (%d pages) — rasterising at %d DPI so pages are "
            "processed individually (one bad page can't abort the whole export)",
            page_count, dpi,
        )

    png_dir = work_dir / "pages"
    pages = await asyncio.get_event_loop().run_in_executor(
        None, _render_pdf_to_pngs, input_file, png_dir, dpi
    )
    return pages


OCR_CONSTANT = "org.audiveris.omr.text.tesseract.TesseractOCR.Constants.useOCR=true"


async def _run_audiveris_once(inputs: list[Path], output_dir: Path, ocr: bool = False) -> bool:
    """
    Run one Audiveris subprocess call.  Returns True on rc=0.
    Raises asyncio.TimeoutError if the process exceeds TIMEOUT_SECONDS.
    Logs (but does not raise) on non-zero exit so that the caller can
    still collect any partial output Audiveris managed to write.
    """
    cmd = [
        AUDIVERIS_BIN,
        "-batch",
        "-export",
        "-output", str(output_dir),
    ]
    if ocr:
        cmd += ["-constant", OCR_CONSTANT]
    cmd += [
        "--",
        *[str(p) for p in inputs],
    ]
    logger.info("Running Audiveris: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise asyncio.TimeoutError(
            f"Audiveris timed out after {TIMEOUT_SECONDS}s"
        )

    stderr_text = stderr.decode(errors="replace")
    logger.debug("Audiveris stdout: %s", stdout.decode(errors="replace"))
    logger.debug("Audiveris stderr: %s", stderr_text)

    if proc.returncode != 0:
        logger.warning(
            "Audiveris exited rc=%d for %s: %s",
            proc.returncode, [p.name for p in inputs], stderr_text[:500],
        )
        return False
    return True


def _collect_omr_result(stem: str, output_dir: Path) -> Path | None:
    """Return the XML path produced for *stem*, or None if nothing was written.

    Audiveris writes a plain "{stem}.xml"/".mxl" when it finds one musical
    score on the sheet, but "{stem}.mvt1.mxl", ".mvt2.mxl", ... when it
    detects several distinct movements/pieces on the same physical page
    (seen e.g. with photographed hymnal pages carrying two hymns) - those
    were previously not recognised at all here, silently dropping the whole
    page. Merge them into one file for this page instead.
    """
    xml_path = output_dir / f"{stem}.xml"
    if xml_path.exists():
        return xml_path

    mxl_path = output_dir / f"{stem}.mxl"
    if mxl_path.exists():
        return _extract_mxl(mxl_path)

    mvt_files = sorted(output_dir.glob(f"{stem}.mvt*.mxl"))
    if not mvt_files:
        return None
    if len(mvt_files) == 1:
        return _extract_mxl(mvt_files[0])

    mvt_xml_paths = [_extract_mxl(p) for p in mvt_files]
    merged_path = output_dir / f"{stem}.movements-merged.xml"
    _merge_musicxml_files(mvt_xml_paths, merged_path)
    logger.info(
        "%s: merged %d movements into one page (%s)",
        stem, len(mvt_files), merged_path.name,
    )
    return merged_path


def _merge_musicxml_files(xml_paths: list[Path], output_path: Path) -> None:
    """Merge consecutive-page MusicXML files into one score.

    Parts are matched by list index.  Measures from pages 2..N are appended
    to the corresponding parts from page 1 and renumbered sequentially.
    """
    if len(xml_paths) == 1:
        shutil.copy2(str(xml_paths[0]), str(output_path))
        return

    parsed: list[tuple[ET.ElementTree, ET.Element]] = []
    for path in xml_paths:
        try:
            tree = ET.parse(str(path))
            parsed.append((tree, tree.getroot()))
        except Exception as exc:
            logger.warning("Skipping %s during merge (parse error): %s", path, exc)

    if not parsed:
        raise RuntimeError("No valid MusicXML files to merge")
    if len(parsed) == 1:
        shutil.copy2(str(xml_paths[0]), str(output_path))
        return

    def _ns(el: ET.Element) -> str:
        tag = el.tag
        return tag[:tag.index("}") + 1] if "{" in tag else ""

    base_tree, base_root = parsed[0]
    base_ns = _ns(base_root)

    for _, src_root in parsed[1:]:
        src_ns = _ns(src_root)
        base_parts = base_root.findall(f"{base_ns}part")
        src_parts = src_root.findall(f"{src_ns}part")

        for i, base_part in enumerate(base_parts):
            if i >= len(src_parts):
                continue
            src_part = src_parts[i]

            offset = len(base_part.findall(f"{base_ns}measure"))
            for j, measure in enumerate(src_part.findall(f"{src_ns}measure")):
                measure.set("number", str(offset + j + 1))
                base_part.append(measure)

    base_tree.write(str(output_path), xml_declaration=True, encoding="unicode")
    logger.info("Merged %d MusicXML pages → %s", len(parsed), output_path)


async def run_omr(inputs: list[Path], output_dir: Path, ocr: bool = False) -> Path:
    """
    Run Audiveris OMR on the given input files (already prepared).
    Returns path to the generated MusicXML file.

    For single inputs the original single-call behaviour is used.
    For multiple inputs — multi-page PDFs rasterised to per-page PNGs, or
    several separately uploaded pages/photos of one piece — each page is
    processed individually so that one slow or failing page does not abort
    the whole job, and the results are merged into one MusicXML file in the
    order given (callers are responsible for passing inputs in page order;
    this function does not re-sort).
    """
    if len(inputs) == 0:
        raise RuntimeError("run_omr called with no inputs")

    if len(inputs) == 1:
        # ── original single-call path ──────────────────────────────────────
        await _run_audiveris_once(inputs, output_dir, ocr=ocr)

        result = _collect_omr_result(inputs[0].stem, output_dir)
        if result is None:
            raise RuntimeError("Audiveris produced no output")
        logger.info("Audiveris produced: %s", result)
        return result

    # ── multi-page path: one Audiveris call per page ───────────────────────
    logger.info("Multi-page input (%d pages) — processing individually", len(inputs))
    xml_paths: list[Path] = []

    for i, inp in enumerate(inputs):
        # Each page gets its own output subdir: inputs coming from different
        # uploaded files can share a basename (e.g. two uploaded PDFs each
        # rasterising to page-0001.png), and Audiveris names its output after
        # the input's basename — a shared output_dir would let one page's
        # result silently overwrite another's.
        page_output_dir = output_dir / f"omr_{i:04d}"
        page_output_dir.mkdir(parents=True, exist_ok=True)
        try:
            await _run_audiveris_once([inp], page_output_dir, ocr=ocr)
        except asyncio.TimeoutError:
            logger.warning("Page %s timed out — skipping", inp.name)
            continue
        except Exception as exc:
            logger.warning("Page %s error — skipping: %s", inp.name, exc)
            continue

        result = _collect_omr_result(inp.stem, page_output_dir)
        if result is not None:
            xml_paths.append(result)
            logger.info("Page %s → %s", inp.name, result.name)
        else:
            logger.warning("Page %s: Audiveris produced no output", inp.name)

    if not xml_paths:
        raise RuntimeError("All pages failed OMR")

    if len(xml_paths) == 1:
        return xml_paths[0]

    merged_path = output_dir / "merged.xml"
    _merge_musicxml_files(xml_paths, merged_path)
    return merged_path
