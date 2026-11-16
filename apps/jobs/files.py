"""Files on a job (images, vector artwork, PDFs).

Anyone who can open the job can add one; Admin and Manager can remove one. Each add and
remove is on the job's timeline. Files are served only through the tracker, after the same
check as the job page, never straight from disk.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.audit.services import record

from . import money
from .models import JobFile

# Images, vector artwork and PDFs.
ALLOWED = {
    "png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic",
    "svg", "ai", "eps", "cdr", "dxf",
    "pdf",
}  # fmt: skip
# The server takes up to 50 MB per upload (deploy/caddy/Caddyfile).
MAX_BYTES = 45 * 1024 * 1024
ADD = "jobs.add_jobfile"
REMOVE = "jobs.delete_jobfile"


def _check(upload) -> str:
    name = (upload.name or "").strip().replace("/", "_").replace("\\", "_")[-200:]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED:
        raise ValidationError(f"{name}: only images, vector artwork (SVG, AI, EPS) and PDFs.")
    if upload.size > MAX_BYTES:
        raise ValidationError(f"{name} is over {MAX_BYTES // (1024 * 1024)} MB.")
    if upload.size == 0:
        raise ValidationError(f"{name} is empty.")
    if money.has_amount(name):
        raise ValidationError(f"{name}: {money.MESSAGE}")
    return name


def add(job, uploads, *, user) -> list[JobFile]:
    """Check every file first, then save them all, or none."""
    if not (user.has_perm(ADD) and user.has_perm("jobs.view_job")):
        raise PermissionDenied("You can't add files to jobs.")
    if not uploads:
        raise ValidationError("Choose a file first.")
    names = [_check(u) for u in uploads]
    saved = []
    with transaction.atomic():
        for upload, name in zip(uploads, names, strict=True):
            f = JobFile(job=job, name=name, size=upload.size, uploaded_by=user)
            f.file.save(name, upload, save=False)
            f.save()
            record(user, f, "create", {"file": [None, name]}, job=job)
            saved.append(f)
    return saved


def remove(f: JobFile, *, user) -> None:
    if not user.has_perm(REMOVE):
        raise PermissionDenied("Only Admin and Manager can remove files.")
    job, name = f.job, f.name
    with transaction.atomic():
        record(user, f, "delete", {"file": [name, None]}, job=job)
        stored = f.file.name
        f.delete()
        transaction.on_commit(lambda: f.file.storage.delete(stored))
