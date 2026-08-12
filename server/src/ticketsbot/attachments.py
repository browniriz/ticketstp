from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .models import Attachment, utcnow
from .services.roles import UserError

BLOCKED_EXTENSIONS = {
    ".html", ".htm", ".xhtml", ".shtml", ".svg", ".mhtml",
    ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll", ".apk", ".jar",
    ".js", ".jse", ".mjs", ".vbs", ".vbe", ".ps1", ".sh", ".hta", ".wsf", ".reg",
    ".php", ".phtml", ".php3", ".php4", ".php5", ".pht",
}
BLOCKED_MIMES = {
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/javascript",
    "text/javascript", "application/x-msdownload", "application/x-msdos-program",
    "application/x-sh", "application/x-httpd-php", "application/x-msdownload; format=pe32",
}
DATA_URL = re.compile(r"^data:([^;]{0,120});base64,(.*)$", re.DOTALL | re.IGNORECASE)
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


@dataclass
class StagedAttachment:
    temp_path: Path
    token: str
    stored_name: str
    original_name: str
    mime_type: str
    size: int


def safe_filename(value: str | None) -> str:
    name = unicodedata.normalize("NFKC", INVISIBLE.sub("", str(value or "")))
    name = re.split(r"[\\/]", name)[-1]
    name = "".join("_" if ord(c) < 0x20 or ord(c) == 0x7f or c in '<>:"/\\|?*' else c for c in name)
    return name.strip()[:120] or "attachment"


def stage_data_url(settings, data_url: str, filename: str | None) -> StagedAttachment:
    match = DATA_URL.match(str(data_url or ""))
    if not match:
        raise UserError("Некорректный формат файла.")
    mime = match.group(1).lower().strip()
    name = safe_filename(filename)
    ext = Path(name).suffix.lower()
    if mime in BLOCKED_MIMES or ext in BLOCKED_EXTENSIONS:
        raise UserError("Такой тип файла нельзя прикреплять.")
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > ((settings.max_attachment_bytes + 2) // 3) * 4 + 4:
        raise UserError("Файл превышает лимит 20 МиБ.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise UserError("Некорректный формат файла.")
    if len(content) > settings.max_attachment_bytes:
        raise UserError("Файл превышает лимит 20 МиБ.")
    media = Path(settings.media_dir)
    staging = media / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    stored = token + (ext if ext and len(ext) <= 12 else "")
    temp = staging / (token + ".tmp")
    try:
        with temp.open("xb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise UserError("Не удалось сохранить файл.") from exc
    return StagedAttachment(temp, token, stored, name, mime, len(content))


def finalize(staged: StagedAttachment, settings) -> Path:
    destination = Path(settings.media_dir) / staged.stored_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.temp_path.replace(destination)
    return destination


def discard(staged: StagedAttachment | None) -> None:
    if staged:
        staged.temp_path.unlink(missing_ok=True)


def attachment_url(settings, token: str) -> str:
    return settings.public_base_url.rstrip("/") + "/media/" + token


def model_from_stage(staged: StagedAttachment, ticket_id: int) -> Attachment:
    return Attachment(token=staged.token, ticket_id=ticket_id, stored_name=staged.stored_name,
                      original_name=staged.original_name, mime_type=staged.mime_type, size=staged.size)
