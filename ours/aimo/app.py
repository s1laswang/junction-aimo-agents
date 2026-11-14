import importlib
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

app = FastAPI(title="AIMO Trusted Trade Gateway", version="0.1.0")


@app.get("/health")
def health():
    return {"ok": True}


for mod, attr, prefix in [("aimo.api", "router", "/v1"), ("aimo.discovery", "router", "")]:
    try:
        app.include_router(getattr(importlib.import_module(mod), attr), prefix=prefix)
    except ModuleNotFoundError as e:
        if e.name != mod:
            raise

try:
    from aimo.mcp_server import mount as mount_mcp
    mount_mcp(app)
except ModuleNotFoundError as e:
    if e.name != "aimo.mcp_server":
        raise

web = Path(__file__).resolve().parent.parent / "web"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
