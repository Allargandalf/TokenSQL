"""Bounded multipart reads shared by upload and chat convenience routes."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException, UploadFile
from .agent import AgentUpload

async def _read_upload(upload: UploadFile, max_bytes: int, total_remaining: int) -> AgentUpload:
    chunks: list[bytes] = []
    size = 0
    limit = min(max_bytes, max(0, total_remaining))
    try:
        while True:
            chunk = await upload.read(min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, detail="upload exceeds the file or total size limit")
            chunks.append(chunk)
    finally:
        await upload.close()
    return AgentUpload(upload.filename or "upload", b"".join(chunks), upload.content_type)

async def read_uploads(files: list[UploadFile], config: Any) -> list[AgentUpload]:
    if len(files) > config.max_files:
        for upload in files:
            await upload.close()
        raise HTTPException(413, detail="too many uploaded files")
    uploads = []
    total = 0
    for position, item in enumerate(files):
        try:
            upload = await _read_upload(item, config.max_file_bytes, config.max_total_bytes - total)
        except Exception:
            for remaining in files[position + 1:]:
                await remaining.close()
            raise
        uploads.append(upload)
        total += len(upload.content)
    return uploads
