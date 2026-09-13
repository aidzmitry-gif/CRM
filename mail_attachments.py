"""Validation and decoding for user supplied email attachments.

Uploaded bytes are kept in the frozen MIME only.  Validation is bounded and
does not depend on a filesystem path or on optional office applications.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import re
import struct
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

from fastapi import HTTPException
from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

from modules.sales.mail_queue import Attachment, digest

MAX_ATTACHMENT_BYTES = 14 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 10
MAX_FILENAME_LENGTH = 255

ALLOWED_TYPES = {
    "application/pdf": {".pdf"},
    "text/plain": {".txt"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class UploadSpec:
    filename: str
    content_type: str
    content_base64: str


def _bad(message: str) -> HTTPException:
    return HTTPException(422, message)


def safe_filename(filename: str, content_type: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise _bad("Укажите имя файла")
    filename = filename.strip()
    if (
        len(filename) > MAX_FILENAME_LENGTH
        or _CONTROL_RE.search(filename)
        or any(char in filename for char in "/\\")
        or filename in {".", ".."}
    ):
        raise _bad("Некорректное имя вложения")
    allowed_suffixes = ALLOWED_TYPES.get(content_type)
    if allowed_suffixes is None or not any(filename.casefold().endswith(suffix) for suffix in allowed_suffixes):
        raise _bad("Расширение вложения не соответствует типу файла")
    return filename


def _zip_member_safe(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    normalized = posixpath.normpath(name.replace("\\", "/"))
    components = name.split("/")
    if name.endswith("/"):
        components = components[:-1]
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in components)
        or normalized == "."
        or normalized == ".."
        or normalized.startswith("../")
    ):
        return False
    # Unix mode 0120000 marks a symbolic link in an archive.
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        return False
    return info.file_size <= MAX_ATTACHMENT_BYTES


_FORBIDDEN_OOXML_MARKERS = {
    "activex",
    "embeddedobject",
    "embeddings",
    "externallink",
    "externallinks",
    "macros",
    "oleobject",
    "vba",
    "vbaproject",
}


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _ooxml_markers(value: str) -> set[str]:
    return {
        part.casefold()
        for part in re.split(r"[/\\#?:._-]+", value)
        if part
    }


def _is_forbidden_ooxml(value: str) -> bool:
    lowered = value.casefold()
    markers = _ooxml_markers(value)
    return bool(markers & _FORBIDDEN_OOXML_MARKERS) or any(
        marker in lowered
        for marker in ("macroenabled", "macro-enabled", "vba", "activex", "oleobject", "embeddedobject")
    )


def _validate_ooxml(content: bytes, content_type: str) -> None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 2048 or any(not _zip_member_safe(item) for item in infos):
                raise _bad("Небезопасное содержимое офисного архива")
            if sum(item.file_size for item in infos) > MAX_ATTACHMENT_BYTES:
                raise _bad("Размер распакованного офисного файла превышает 14 МиБ")
            names = [item.filename.replace("\\", "/") for item in infos]
            normalized_names = [name.casefold() for name in names]
            if len(set(normalized_names)) != len(normalized_names):
                raise _bad("Макросы и внешние ссылки в офисном файле запрещены")
            if any(
                any(part.casefold() in _FORBIDDEN_OOXML_MARKERS for part in name.split("/") if part)
                or any(part.casefold() == "externallinks" for part in name.split("/") if part)
                for name in names
            ):
                raise _bad("Макросы и внешние ссылки в офисном файле запрещены")
            if content_type.endswith("wordprocessingml.document"):
                main_part = "/word/document.xml"
                expected_main = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            else:
                main_part = "/xl/workbook.xml"
                expected_main = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
            if main_part[1:] not in names:
                raise _bad("Некорректный офисный файл")
            content_types = archive.read("[Content_Types].xml")
            root = ElementTree.fromstring(content_types)
            if _xml_local_name(root.tag) != "types":
                raise _bad("Некорректный тип офисного файла")
            main_override = False
            for element in root.iter():
                element_name = _xml_local_name(element.tag)
                if element_name not in {"override", "default"}:
                    continue
                part_name = element.attrib.get("PartName", "")
                part_type = element.attrib.get("ContentType", "")
                extension = element.attrib.get("Extension", "")
                if (
                    _is_forbidden_ooxml(part_type)
                    or _is_forbidden_ooxml(part_name)
                    or _is_forbidden_ooxml(extension)
                ):
                    raise _bad("Макросы и внешние ссылки в офисном файле запрещены")
                if element_name == "override" and part_name == main_part:
                    main_override = part_type.casefold() == expected_main.casefold()
            if not main_override:
                raise _bad("Некорректный тип офисного файла")
            for info in infos:
                if info.filename.casefold().endswith(".rels"):
                    root_rels = ElementTree.fromstring(archive.read(info))
                    for relationship in root_rels:
                        if _xml_local_name(relationship.tag) != "relationship":
                            continue
                        rel_type = relationship.attrib.get("Type", "")
                        relationship_target = relationship.attrib.get("Target", "")
                        if _is_forbidden_ooxml(rel_type) or _is_forbidden_ooxml(relationship_target):
                            raise _bad("Макросы и внешние ссылки в офисном файле запрещены")
                        target_mode = (relationship.attrib.get("TargetMode") or "").casefold()
                        target = (relationship.attrib.get("Target") or "").strip().casefold()
                        if target_mode == "external" or target.startswith(
                            ("http:", "https:", "file:", "\\\\")
                        ):
                            raise _bad("Внешние ссылки в офисном файле запрещены")
    except (zipfile.BadZipFile, RuntimeError, OverflowError) as exc:
        raise _bad("Некорректный офисный архив") from exc
    except (KeyError, UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise _bad("Некорректный офисный файл") from exc


def _pdf_name(value) -> str:
    return str(value).lstrip("/").casefold()


_FORBIDDEN_PDF_KEYS = {
    "aa",
    "af",
    "ef",
    "embeddedfiles",
    "fileattachment",
    "javascript",
    "js",
    "launch",
    "openaction",
    "richmedia",
    "sound",
    "submitform",
    "xfa",
}
_FORBIDDEN_PDF_ACTIONS = {
    "gotor",
    "importdata",
    "javascript",
    "launch",
    "named",
    "resetform",
    "submitform",
}
_MAX_PDF_OBJECTS = 10000
_MAX_PDF_PAGES = 10000


def _walk_pdf(value, *, seen: set[tuple[int, int]], state: list[int], depth: int = 0) -> None:
    if depth > 64:
        raise _bad("PDF-вложение имеет недопустимую глубину объектов")
    if state[0] <= 0:
        raise _bad("PDF-вложение слишком сложное")
    state[0] -= 1
    if hasattr(value, "idnum") and hasattr(value, "generation") and hasattr(value, "get_object"):
        marker = (value.idnum, value.generation)
        if marker in seen:
            return
        seen.add(marker)
        _walk_pdf(value.get_object(), seen=seen, state=state, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _walk_pdf(child, seen=seen, state=state, depth=depth + 1)
        return
    if not hasattr(value, "items"):
        return
    for key, child in value.items():
        key_name = _pdf_name(key)
        if key_name in _FORBIDDEN_PDF_KEYS:
            raise _bad("Активное содержимое в PDF-вложении запрещено")
        if key_name in {"type", "subtype"}:
            resolved = child.get_object() if hasattr(child, "get_object") else child
            if _pdf_name(resolved) in {"fileattachment", "filespec", "embeddedfile", "richmedia", "movie", "sound", "screen", "3d"}:
                raise _bad("Встроенные файлы и активные аннотации в PDF запрещены")
        if key_name == "a":
            action = child.get_object() if hasattr(child, "get_object") else child
            if hasattr(action, "get"):
                action_type = _pdf_name(action.get("/S", ""))
                if action_type in _FORBIDDEN_PDF_ACTIONS or action_type not in {"", "goto"}:
                    raise _bad("Активное содержимое в PDF-вложении запрещено")
        _walk_pdf(child, seen=seen, state=state, depth=depth + 1)


def _validate_uploaded_pdf(content: bytes) -> None:
    try:
        reader = PdfReader(
            BytesIO(content), strict=True, root_object_recovery_limit=_MAX_PDF_OBJECTS
        )
        if reader.is_encrypted:
            raise _bad("Зашифрованное PDF-вложение запрещено")
        seen: set[tuple[int, int]] = set()
        budget = [_MAX_PDF_OBJECTS]
        _walk_pdf(reader.root_object, seen=seen, state=budget)
        # Force traversal of the page tree and page annotations as pypdf can
        # defer parsing those objects until pages are accessed.
        pages = reader.pages
        if len(pages) > _MAX_PDF_PAGES:
            raise _bad("PDF-вложение слишком сложное")
        for page in pages:
            _walk_pdf(page, seen=seen, state=budget)
    except HTTPException:
        raise
    except (PdfReadError, PdfStreamError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise _bad("Некорректное PDF-вложение") from exc
    except Exception as exc:
        raise _bad("Некорректное PDF-вложение") from exc


def validate_content(content: bytes, content_type: str, *, uploaded: bool = True) -> None:
    if not content:
        raise _bad("Пустое вложение запрещено")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "Размер вложения превышает 14 МиБ")
    if content_type == "application/pdf":
        if not content.startswith(b"%PDF-"):
            raise _bad("Некорректное PDF-вложение")
        if uploaded:
            _validate_uploaded_pdf(content)
    elif content_type == "text/plain":
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _bad("Текстовое вложение должно быть в UTF-8") from exc
    elif content_type == "image/png":
        if len(content) < 24 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise _bad("Некорректное PNG-вложение")
        try:
            length = struct.unpack(">I", content[8:12])[0]
            if content[12:16] != b"IHDR" or length < 13 or len(content) < 16 + length:
                raise _bad("Некорректное PNG-вложение")
            width, height = struct.unpack(">II", content[16:24])
            if not width or not height:
                raise _bad("Некорректное PNG-вложение")
        except struct.error as exc:
            raise _bad("Некорректное PNG-вложение") from exc
    elif content_type == "image/jpeg":
        if len(content) < 4 or not content.startswith(b"\xff\xd8\xff") or not content.endswith(b"\xff\xd9"):
            raise _bad("Некорректное JPEG-вложение")
    elif content_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        if not content.startswith(b"PK\x03\x04"):
            raise _bad("Некорректный офисный файл")
        _validate_ooxml(content, content_type)
    else:
        raise _bad("Тип вложения не поддерживается")


def decode_upload(spec: UploadSpec) -> Attachment:
    content_type = (spec.content_type or "").strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise _bad("Тип вложения не поддерживается")
    filename = safe_filename(spec.filename, content_type)
    if not isinstance(spec.content_base64, str) or len(spec.content_base64) > 4 * MAX_ATTACHMENT_BYTES // 3 + 4:
        raise HTTPException(413, "Размер вложения превышает 14 МиБ")
    try:
        content = base64.b64decode(spec.content_base64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise _bad("Некорректное base64-вложение") from exc
    validate_content(content, content_type, uploaded=True)
    source_hash = digest(content)
    return Attachment(
        document_id=None,
        version=None,
        number="",
        source_sha256=source_hash,
        filename=filename,
        content=content,
        content_type=content_type,
    )


def uploads_from_payload(items: list[UploadSpec]) -> list[Attachment]:
    if len(items) > MAX_ATTACHMENT_COUNT:
        raise _bad("Можно добавить не более 10 вложений")
    attachments = [decode_upload(item) for item in items]
    if sum(len(item.content) for item in attachments) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "Общий размер вложений превышает 14 МиБ")
    return attachments
