import asyncio
import logging
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session, select

from app.models import Score, Part
from app.services.storage import score_output_dir, parts_dir
from app.services.musescore import _run_musescore

router = APIRouter(prefix="/scores", tags=["files"])
logger = logging.getLogger(__name__)

SSE_MAX_SECONDS = 300
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# On-demand export formats: extension -> (media_type, description)
EXPORT_FORMATS = {
    "mxl":  ("application/vnd.recordare.musicxml", "MusicXML compressed"),
    "mscz": ("application/octet-stream",           "MuseScore native"),
    "ly":   ("text/x-lilypond",                    "LilyPond"),
    "mp3":  ("audio/mpeg",                         "MP3 audio"),
}


def _get_engine():
    from app.main import engine
    return engine


def _validate_score_id(score_id: str) -> None:
    if not _UUID_RE.match(score_id):
        raise HTTPException(status_code=400, detail="Invalid score ID")


def _require_ready_score(db: Session, score_id: str) -> Score:
    score = db.get(Score, score_id)
    if not score:
        raise HTTPException(status_code=404, detail="Score not found")
    if score.status != "ready":
        raise HTTPException(status_code=409, detail="Score is not ready yet")
    return score


def _download_name(score: Score, ext: str, suffix: str = "") -> str:
    """Return a sanitised download filename like 'Sinfonie Nr.5.pdf'."""
    base = score.display_name or score.original_filename
    stem = Path(base).stem  # strip any existing extension
    name = f"{stem}{suffix}.{ext}"
    # Replace characters that are problematic in filenames
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def _strip_forced_breaks(xml_bytes: bytes) -> bytes:
    """Remove forced layout hints so OSMD reflows freely to browser width.

    Uses regex on the raw bytes to avoid re-serialisation via ElementTree,
    which strips the XML declaration and DOCTYPE that OSMD requires.
    """
    text = xml_bytes.decode('utf-8', errors='replace')
    # Remove new-system / new-page attributes on <print> elements
    text = re.sub(r'\s+new-system="yes"', '', text)
    text = re.sub(r'\s+new-page="yes"', '', text)
    # Remove <system-layout> blocks — OSMD creates a new system for any <print>
    # that contains <system-layout>, even without new-system="yes"
    text = re.sub(r'\s*<system-layout>.*?</system-layout>', '', text, flags=re.DOTALL)
    # Remove <time-modification> blocks that lack <normal-type> — OSMD's Fraction
    # crashes with 'realValue undefined' on any actual:normal ratio without it
    def _drop_if_no_normal_type(m: re.Match) -> str:
        return '' if '<normal-type>' not in m.group(0) else m.group(0)
    text = re.sub(
        r'\s*<time-modification>.*?</time-modification>',
        _drop_if_no_normal_type,
        text,
        flags=re.DOTALL,
    )
    return text.encode('utf-8')


def _find_musicxml(score_id: str):
    """Return path to the extracted MusicXML file for a score."""
    output_dir = score_output_dir(score_id)
    xml_files = [f for f in output_dir.glob("*.xml") if f.parent == output_dir]
    if not xml_files:
        raise HTTPException(status_code=404, detail="MusicXML source file not found")
    return xml_files[0]


def _find_svg_pages(score_id: str, xml_stem: str) -> list:
    """Return sorted list of per-page SVGs: {stem}-1.svg, {stem}-2.svg, ..."""
    output_dir = score_output_dir(score_id)
    return sorted(output_dir.glob(f"{xml_stem}-*.svg"))


@router.get("/{score_id}/status")
async def score_status_sse(score_id: str):
    _validate_score_id(score_id)

    async def event_generator():
        elapsed = 0
        with Session(_get_engine()) as db:
            while elapsed < SSE_MAX_SECONDS:
                score = db.get(Score, score_id)
                if not score:
                    yield "data: not_found\n\n"
                    return
                db.expire(score)
                score = db.get(Score, score_id)
                status = score.status

                yield f"data: {status}\n\n"

                if status in ("ready", "failed"):
                    return

                await asyncio.sleep(1)
                elapsed += 1

        yield "data: timeout\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{score_id}/musicxml")
async def get_musicxml(score_id: str):
    """Serve a MuseScore-normalised MusicXML for OSMD rendering.

    Audiveris XML can contain structures that crash OSMD internally.
    Running it through MuseScore first produces clean, standard XML.
    The result is cached next to the source file as {stem}.ms.musicxml.
    Falls back to the raw Audiveris XML if MuseScore conversion fails.
    """
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        _require_ready_score(db, score_id)

    xml_path = _find_musicxml(score_id)
    clean_path = xml_path.with_suffix(".ms.musicxml")

    if not clean_path.exists():
        try:
            await _run_musescore(xml_path, clean_path)
        except Exception as exc:
            logger.warning("MuseScore normalisation failed, falling back to raw XML: %s", exc)
            clean_path = xml_path

    serve_path = clean_path if clean_path.exists() else xml_path
    content = _strip_forced_breaks(serve_path.read_bytes())
    return Response(content=content, media_type="application/xml")


@router.get("/{score_id}/pdf")
def get_pdf(score_id: str):
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        score = _require_ready_score(db, score_id)
        filename = _download_name(score, "pdf")
    output_dir = score_output_dir(score_id)
    pdf_files = [f for f in output_dir.glob("*.pdf") if f.parent == output_dir]
    if not pdf_files:
        raise HTTPException(status_code=404, detail="PDF file not found")
    return FileResponse(path=str(pdf_files[0]), filename=filename)


@router.get("/{score_id}/midi")
def get_midi(score_id: str):
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        score = _require_ready_score(db, score_id)
        filename = _download_name(score, "mid")
    output_dir = score_output_dir(score_id)
    midi_files = [f for f in output_dir.glob("*.mid") if f.parent == output_dir]
    if not midi_files:
        raise HTTPException(status_code=404, detail="MIDI file not found")
    return FileResponse(path=str(midi_files[0]), media_type="audio/midi", filename=filename)


@router.get("/{score_id}/parts/{part_name}/midi")
def get_part_midi(score_id: str, part_name: str):
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        score = _require_ready_score(db, score_id)
        statement = select(Part).where(
            Part.score_id == score_id,
            Part.name == part_name,
        )
        part = db.exec(statement).first()
        if not part:
            raise HTTPException(status_code=404, detail="Part not found")

        midi_path = parts_dir(score_id) / part.midi_filename
        if not midi_path.exists():
            raise HTTPException(status_code=404, detail="Part MIDI file not found")

        filename = _download_name(score, "mid", suffix=f" - {part_name}")
        return FileResponse(path=str(midi_path), media_type="audio/midi", filename=filename)


@router.get("/{score_id}/export/{fmt}")
async def export_format(score_id: str, fmt: str):
    """On-demand export: generate file with MuseScore if not already cached."""
    _validate_score_id(score_id)
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unknown format '{fmt}'. Supported: {', '.join(EXPORT_FORMATS)}")

    with Session(_get_engine()) as db:
        score = _require_ready_score(db, score_id)
        filename = _download_name(score, fmt)

    media_type, _ = EXPORT_FORMATS[fmt]
    xml_path = _find_musicxml(score_id)
    output_path = score_output_dir(score_id) / f"{xml_path.stem}.{fmt}"

    if not output_path.exists():
        logger.info("Generating %s export for %s", fmt, score_id)
        await _run_musescore(xml_path, output_path)

    if not output_path.exists():
        raise HTTPException(status_code=500, detail=f"Export to {fmt} failed")

    return FileResponse(path=str(output_path), media_type=media_type, filename=filename)


@router.get("/{score_id}/svg")
async def get_svg_info(score_id: str):
    """Return SVG page count; generate SVGs on demand if not yet cached."""
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        _require_ready_score(db, score_id)

    xml_path = _find_musicxml(score_id)
    pages = _find_svg_pages(score_id, xml_path.stem)

    if not pages:
        # Prefer the MuseScore-normalised XML for better SVG rendering quality.
        ms_path = xml_path.with_suffix(".ms.musicxml")
        svg_source = ms_path if ms_path.exists() else xml_path
        svg_target = score_output_dir(score_id) / f"{xml_path.stem}.svg"
        await _run_musescore(svg_source, svg_target)
        pages = _find_svg_pages(score_id, xml_path.stem)

    return {"pages": len(pages)}


def _original_files(score_id: str) -> tuple:
    """Return (upload_dir, [file_path, ...]) for the original upload(s), in
    page order. Multi-file uploads (input-0000.*, input-0001.*, ...) yield
    one path per uploaded page; legacy single-file scores (input.<ext>)
    yield a single-item list.
    """
    from app.services.storage import score_upload_dir
    with Session(_get_engine()) as db:
        _require_ready_score(db, score_id)
    upload_dir = score_upload_dir(score_id)
    files = sorted(upload_dir.glob("input-*.*")) or sorted(upload_dir.glob("input.*"))
    if not files:
        raise HTTPException(status_code=404, detail="Original file not found")
    return upload_dir, files


async def _render_pdf_page_png(pdf_path: Path, page: int, output_path: Path) -> None:
    """Render one PDF page to PNG using Ghostscript (150 dpi)."""
    import asyncio
    cmd = [
        "gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=png16m", "-r150",
        f"-dFirstPage={page}", f"-dLastPage={page}",
        f"-sOutputFile={output_path}",
        str(pdf_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Ghostscript timed out rendering page {page}")
    if proc.returncode != 0:
        raise RuntimeError(f"Ghostscript failed (rc={proc.returncode}): {stderr.decode(errors='replace')}")


@router.get("/{score_id}/original/info")
def get_original_info(score_id: str):
    """Return page count for the original upload."""
    _validate_score_id(score_id)
    _, files = _original_files(score_id)
    if len(files) == 1 and files[0].suffix.lower() == ".pdf":
        from pypdf import PdfReader
        pages = len(PdfReader(str(files[0])).pages)
    else:
        # One uploaded file per page (images), or several PDFs uploaded
        # together — each counts as one page here (see get_original_page).
        pages = len(files)
    return {"pages": pages}


@router.get("/{score_id}/original/page/{page}")
async def get_original_page(score_id: str, page: int):
    """Serve a page of the original as PNG (renders PDF pages on demand)."""
    _validate_score_id(score_id)
    upload_dir, files = _original_files(score_id)

    image_types = {
        ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".tiff": "image/tiff", ".tif": "image/tiff",
    }

    if len(files) == 1 and files[0].suffix.lower() == ".pdf":
        file_path = files[0]
        cache = upload_dir / f"preview_p{page}.png"
        if not cache.exists():
            try:
                await _render_pdf_page_png(file_path, page, cache)
            except Exception as exc:
                logger.error("PDF page render failed: %s", exc)
                raise HTTPException(status_code=500, detail="Failed to render PDF page")
        if not cache.exists():
            raise HTTPException(status_code=500, detail="Rendered page not found")
        return FileResponse(str(cache), media_type="image/png")

    # Multi-file upload (or a single image): page N is the Nth uploaded file.
    if page < 1 or page > len(files):
        raise HTTPException(status_code=404, detail="Page not found")
    file_path = files[page - 1]
    ext = file_path.suffix.lower()

    if ext in image_types:
        return FileResponse(str(file_path), media_type=image_types[ext])

    if ext == ".pdf":
        # A PDF among several uploaded files — show its first page only.
        cache = upload_dir / f"preview_multi_p{page}.png"
        if not cache.exists():
            try:
                await _render_pdf_page_png(file_path, 1, cache)
            except Exception as exc:
                logger.error("PDF page render failed: %s", exc)
                raise HTTPException(status_code=500, detail="Failed to render PDF page")
        if not cache.exists():
            raise HTTPException(status_code=500, detail="Rendered page not found")
        return FileResponse(str(cache), media_type="image/png")

    raise HTTPException(status_code=400, detail="Unsupported file type")


@router.get("/{score_id}/svg/{page}")
async def get_svg_page(score_id: str, page: int):
    """Serve one SVG page (1-indexed)."""
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        _require_ready_score(db, score_id)

    xml_path = _find_musicxml(score_id)
    pages = _find_svg_pages(score_id, xml_path.stem)

    if not pages or page < 1 or page > len(pages):
        raise HTTPException(status_code=404, detail="SVG page not found")

    return FileResponse(
        path=str(pages[page - 1]),
        media_type="image/svg+xml",
        filename=pages[page - 1].name,
    )
