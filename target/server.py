from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent / "static"
app = FastAPI(title="Night Window 4.1", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/tenant-b/")
def tenant_b_index() -> FileResponse:
    return FileResponse(ROOT / "tenant-b" / "index.html")


@app.get("/tenant-b/{page}.html")
def tenant_b_page(page: str) -> FileResponse:
    candidate = (ROOT / "tenant-b" / f"{page}.html").resolve()
    tenant_root = (ROOT / "tenant-b").resolve()
    if candidate.parent != tenant_root or not candidate.exists():
        return FileResponse(ROOT / "not-found.html", status_code=404)
    return FileResponse(candidate)


@app.get("/runtime-config.js")
def runtime_config() -> Response:
    # This is a local fixture's verifier, not a production authentication design.
    pin = os.getenv("NIGHT_WINDOW_PIN", "1937")
    return Response(
        f"window.__NIGHT_WINDOW_PIN__ = {json.dumps(pin)};",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/{page}.html")
def page(page: str) -> FileResponse:
    candidate = (ROOT / f"{page}.html").resolve()
    if candidate.parent != ROOT.resolve() or not candidate.exists():
        return FileResponse(ROOT / "not-found.html", status_code=404)
    return FileResponse(candidate)


def main() -> None:
    uvicorn.run("target.server:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()

