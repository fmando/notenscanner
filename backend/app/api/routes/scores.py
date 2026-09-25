import logging
import shutil
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select
from typing import List
from datetime import datetime, timezone
from pathlib import Path
import re

from app.models import Score, Part, ScoreRead, ScoreRename, PartRead, ScoreDetail
from app.services.storage import score_upload_dir, score_output_dir
from app.services.audiveris import prepare_input, run_omr
from app.services.musescore import export_score

router = APIRouter(prefix="/scores", tags=["scores"])
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".pdf"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

IN_PROGRESS_STATUSES = {"pending", "preparing", "transcribing", "typesetting",
                        "processing", "omr_done"}  # legacy names kept for existing rows


def _get_engine():
    from app.main import engine
    return engine


def _validate_score_id(score_id: str) -> None:
    if not _UUID_RE.match(score_id):
        raise HTTPException(status_code=400, detail="Invalid score ID")


async def process_score(score_id: str, ocr: bool = False):
    """Background task: runs OMR then MuseScore export. Opens its own DB session."""
    with Session(_get_engine()) as db:
        score = db.get(Score, score_id)
        if not score:
            return

        def _set_status(s: str):
            score.status = s
            score.updated_at = datetime.now(timezone.utc)
            db.add(score)
            db.commit()

        try:
            upload_dir = score_upload_dir(score_id)
            # New indexed naming (input-0000.*, input-0001.*, ...) is used for
            # multi-file uploads; legacy single-file scores are still named
            # input.<ext>. A given score only ever has one of the two.
            input_files = sorted(upload_dir.glob("input-*.*")) or sorted(upload_dir.glob("input.*"))
            if not input_files:
                raise FileNotFoundError(f"No input file(s) found in {upload_dir}")

            output_dir = score_output_dir(score_id)
            work_dir = output_dir / "work"
            work_dir.mkdir(parents=True, exist_ok=True)

            # Step 1 – prepare inputs (strip embedded secondary JPEG images,
            # rasterise oversized/multi-page PDFs). Each uploaded file is
            # prepared independently (own work subdir, so per-file page
            # rasterisation can't collide) and the results are concatenated
            # in upload order — multiple uploaded photos become, in effect,
            # consecutive pages of one book for run_omr() below.
            _set_status("preparing")
            inputs: list = []
            for idx, f in enumerate(input_files):
                inputs.extend(await prepare_input(f, work_dir / f"src{idx:03d}"))

            # Step 2 – Audiveris OMR
            _set_status("transcribing")
            musicxml_path = await run_omr(inputs, output_dir, ocr=ocr)

            # Step 3 – MuseScore typesetting & export
            _set_status("typesetting")
            result = await export_score(musicxml_path, output_dir)

            for part in result["parts"]:
                db_part = Part(
                    score_id=score_id,
                    name=part["name"],
                    midi_filename=part["midi"].name,
                )
                db.add(db_part)

            score.status = "ready"
            score.updated_at = datetime.now(timezone.utc)
            db.add(score)
            db.commit()

        except Exception as e:
            logger.exception("Processing failed for score %s: %s", score_id, e)
            try:
                score.status = "failed"
                score.error_message = str(e)
                score.updated_at = datetime.now(timezone.utc)
                db.add(score)
                db.commit()
            except Exception as commit_err:
                logger.exception("Failed to persist error state for score %s: %s", score_id, commit_err)


@router.post("", response_model=ScoreRead, status_code=201)
async def upload_score(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    ocr: bool = Form(False),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    suffixes = []
    for f in files:
        suffix = Path(f.filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type '{suffix}' ({f.filename}). Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )
        suffixes.append(suffix)

    if len(files) == 1:
        display_name = files[0].filename
    else:
        display_name = f"{files[0].filename} + {len(files) - 1} weitere Seite" + (
            "n" if len(files) > 2 else ""
        )

    with Session(_get_engine()) as db:
        score = Score(
            filename=f"input{suffixes[0]}" if len(files) == 1 else "input-multi",
            original_filename=display_name,
            status="pending",
        )
        db.add(score)
        db.commit()
        db.refresh(score)

        upload_dir = score_upload_dir(score.id)
        if len(files) == 1:
            # Unchanged legacy naming for the common single-file case.
            dest = upload_dir / f"input{suffixes[0]}"
            dest.write_bytes(await files[0].read())
        else:
            for idx, (f, suffix) in enumerate(zip(files, suffixes)):
                dest = upload_dir / f"input-{idx:04d}{suffix}"
                dest.write_bytes(await f.read())

        background_tasks.add_task(process_score, score.id, ocr)

        return ScoreRead.model_validate(score)


@router.get("", response_model=List[ScoreRead])
def list_scores():
    with Session(_get_engine()) as db:
        statement = select(Score).order_by(Score.created_at.desc())
        scores = db.exec(statement).all()
        return [ScoreRead.model_validate(s) for s in scores]


@router.get("/{score_id}", response_model=ScoreDetail)
def get_score(score_id: str):
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        score = db.get(Score, score_id)
        if not score:
            raise HTTPException(status_code=404, detail="Score not found")

        parts_statement = select(Part).where(Part.score_id == score_id)
        parts = db.exec(parts_statement).all()

        detail = ScoreDetail.model_validate(score)
        detail.parts = [PartRead.model_validate(p) for p in parts]
        return detail


@router.patch("/{score_id}", response_model=ScoreRead)
def rename_score(score_id: str, body: ScoreRename):
    _validate_score_id(score_id)
    name = body.display_name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="display_name must not be empty")
    if len(name) > 255:
        raise HTTPException(status_code=422, detail="display_name too long (max 255 chars)")

    with Session(_get_engine()) as db:
        score = db.get(Score, score_id)
        if not score:
            raise HTTPException(status_code=404, detail="Score not found")
        score.display_name = name
        score.updated_at = datetime.now(timezone.utc)
        db.add(score)
        db.commit()
        db.refresh(score)
        return ScoreRead.model_validate(score)


@router.delete("/{score_id}", status_code=204)
def delete_score(score_id: str):
    _validate_score_id(score_id)
    with Session(_get_engine()) as db:
        score = db.get(Score, score_id)
        if not score:
            raise HTTPException(status_code=404, detail="Score not found")
        if score.status in IN_PROGRESS_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete score while processing (status: {score.status})",
            )

        # Delete Part records
        parts = db.exec(select(Part).where(Part.score_id == score_id)).all()
        for part in parts:
            db.delete(part)

        db.delete(score)
        db.commit()

    # Remove files from disk
    for d in (score_upload_dir(score_id), score_output_dir(score_id)):
        if d.exists():
            shutil.rmtree(d)
