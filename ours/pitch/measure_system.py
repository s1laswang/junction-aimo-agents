import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}


def _list_mcp_tools(mcp_obj):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(mcp_obj.list_tools())
    out, err = [], []

    def runner():
        try:
            out.append(asyncio.run(mcp_obj.list_tools()))
        except BaseException as exc:
            err.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return out[0]


def measure(root=None):
    base = Path(root) if root is not None else ROOT
    env_keys = ("AIMO_DB", "AIMO_KEY_DIR", "AIMO_AGENT_KEYS")
    saved = {k: os.environ.get(k) for k in env_keys}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AIMO_DB"] = os.path.join(tmp, "aimo.db")
        os.environ["AIMO_KEY_DIR"] = os.path.join(tmp, "keys")
        os.environ.pop("AIMO_AGENT_KEYS", None)
        try:
            from aimo import mandates, mcp_server
            from aimo.app import app
            from scripts import smoke_demo

            mcp_obj = getattr(mcp_server, "mcp", None) or getattr(mcp_server, "server")
            mcp_tool_names = sorted(t.name for t in _list_mcp_tools(mcp_obj))

            paths = app.openapi().get("paths", {})
            v1 = {p: ops for p, ops in paths.items() if p.startswith("/v1/")}
            v1_operations = sum(
                sum(1 for method in ops if method.lower() in HTTP_METHODS)
                for ops in v1.values()
            )
            non_v1_paths = sorted(p for p in paths if not p.startswith("/v1/"))

            recipes_doc = json.loads((base / "data_extra" / "recipes.json").read_text(encoding="utf-8"))
            dishes = recipes_doc.get("dishes", [])

            seeded = mandates.all()
            mandates_by_customer = {
                m.customer: {
                    "per_order_eur": m.per_order_eur,
                    "per_week_eur": m.per_week_eur,
                    "exclude_allergens": list(m.exclude_allergens),
                    "substitutions": m.substitutions,
                    "approvers": list(m.approvers),
                }
                for m in seeded
            }

            scenario_names = sorted(p.stem for p in (base / "scenarios").glob("s*.py") if p.is_file())
            ui_view_names = sorted(p.name for p in (base / "web").glob("*.html") if p.is_file())
            smoke_check_labels = [label for label, _ in smoke_demo.CHECKS]

            return {
                "mcp_tools": len(mcp_tool_names),
                "mcp_tool_names": mcp_tool_names,
                "openapi_paths": len(paths),
                "v1_paths": len(v1),
                "v1_operations": v1_operations,
                "non_v1_paths": non_v1_paths,
                "recipes": len(dishes),
                "seeded_customers": len(mandates_by_customer),
                "mandates": mandates_by_customer,
                "scenarios": len(scenario_names),
                "scenario_names": scenario_names,
                "ui_views": len(ui_view_names),
                "ui_view_names": ui_view_names,
                "smoke_checks": len(smoke_check_labels),
                "smoke_check_labels": smoke_check_labels,
            }
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def main():
    data = measure()
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return data


if __name__ == "__main__":
    main()
