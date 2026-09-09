"""Convert a frozen HTML original to PDF with no external resources or links."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
from html.parser import HTMLParser

from fastapi import HTTPException

MAX_HTML_BYTES = 4 * 1024 * 1024
_slots = asyncio.Semaphore(2)


class SafeOriginal(HTMLParser):
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "iframe", "object", "embed", "form", "input", "link", "base"}:
            raise ValueError("Active content is not allowed")
        if values.get("href") and not values["href"].startswith("#"):
            raise ValueError("External links are not allowed")
        if any(k.startswith("on") for k in values):
            raise ValueError("Event handlers are not allowed")


def render_original(source: str) -> bytes:
    from weasyprint import HTML
    from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

    SafeOriginal().feed(source)

    class EmbeddedImagesOnly(URLFetcher):
        def fetch(self, url, headers=None):
            match = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=\s]+)", url)
            if not match or len(url) > 2 * 1024 * 1024:
                raise FatalURLFetchingError("Document resource is not an embedded PNG/JPEG")
            try:
                data = base64.b64decode(match[2], validate=True)
            except ValueError as exc:
                raise FatalURLFetchingError("Invalid embedded image") from exc
            return URLFetcherResponse(url, data, {"Content-Type": match[1]})

    try:
        document = HTML(
            string=source, url_fetcher=EmbeddedImagesOnly(allowed_protocols={"data"})
        ).render()
    except FatalURLFetchingError:
        raise ValueError("Blocked document resource") from None
    if not 1 <= len(document.pages) <= 100:
        raise ValueError("Document page limit")
    if document.metadata.attachments or any(
        kind != "internal" for p in document.pages for kind, *_ in p.links
    ):
        raise ValueError("Unexpected PDF attachment or external link")
    return document.write_pdf()


def _subprocess_pdf(source: str) -> bytes:
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "WEASYPRINT_DLL_DIRECTORIES",
            "FONTCONFIG_FILE",
            "FONTCONFIG_PATH",
            "PYTHONPATH",
        }
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modules.sales.mail_pdf"],
            input=source.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=40,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise HTTPException(503, "Не удалось подготовить PDF в отведённое время") from exc
    if result.returncode or not result.stdout.startswith(b"%PDF-"):
        raise HTTPException(503, "PDF не подготовлен: проверьте оригинал и сервис PDF")
    if len(result.stdout) > 14 * 1024 * 1024:
        raise HTTPException(413, "PDF превышает допустимый размер")
    return result.stdout


async def pdf(source: str) -> bytes:
    if len(source.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(413, "Оригинал документа превышает допустимый размер")
    async with _slots:
        return await asyncio.to_thread(_subprocess_pdf, source)


if __name__ == "__main__":
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    try:
        data = sys.stdin.buffer.read(MAX_HTML_BYTES + 1)
        if len(data) > MAX_HTML_BYTES:
            raise ValueError("Input limit")
        sys.stdout.buffer.write(render_original(data.decode("utf-8")))
    except Exception:
        # Do not expose document text, filesystem paths, URLs or renderer traces.
        sys.stderr.write("PDF_RENDER_FAILED\n")
        raise SystemExit(1) from None
