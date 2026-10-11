"""Compare packaged Python code with source and verify windowed executables/seed prices."""
from __future__ import annotations

import hashlib
import json
import marshal
import sqlite3
import struct
import types
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]


def canonical(code: types.CodeType) -> tuple:
    return (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
            code.co_flags, code.co_code, code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_exceptiontable,
            tuple(canonical(item) if isinstance(item, types.CodeType) else item for item in code.co_consts))


def subsystem(path: Path) -> int:
    with path.open("rb") as handle:
        handle.seek(60)
        offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(offset + 24 + 68)
        return struct.unpack("<H", handle.read(2))[0]


def main() -> None:
    archive = CArchiveReader(str(ROOT / "dist/XianyuAssistant/XianyuAssistant.exe"))
    code_archive = archive.open_embedded_archive(next(key for key in archive.toc if key.endswith(".pyz")))
    modules = ("xianyu_assistant", *(
        "xianyu_assistant." + item for item in (
            "customer_service.checkout", "customer_service.contact_policy", "customer_service.fact_guard",
            "customer_service.customer_service_worker", "customer_service.browser_adapter",
            "customer_service.browser_adapter_proxy", "customer_service.price_reliability",
            "customer_service.quality_export", "persistence.runtime_storage",
            "browser.executor_supervisor", "browser.executor_child", "ui.customer_service_panel",
        )
    ))
    checks = {}
    for module in modules:
        relative = Path(*module.split("."))
        source = ROOT / "src" / (relative / "__init__.py" if module == "xianyu_assistant" else relative.with_suffix(".py"))
        expected = canonical(compile(source.read_text(encoding="utf-8-sig"), str(source), "exec", dont_inherit=True, optimize=0))
        actual = canonical(code_archive.extract(module))
        if expected != actual:
            raise AssertionError("Packaged code is stale: " + module)
        checks[module] = hashlib.sha256(marshal.dumps(actual)).hexdigest()
    executables = {"application": ROOT / "dist/XianyuAssistant/XianyuAssistant.exe",
                   "playwright_node": ROOT / "dist/XianyuAssistant/_internal/playwright/driver/node.exe"}
    pe = {name: subsystem(path) for name, path in executables.items()}
    if any(value != 2 for value in pe.values()):
        raise AssertionError("A console subsystem remains")
    tables = {"cs_products": "SELECT product_key,listed_price,minimum_price FROM cs_products ORDER BY product_key",
              "cs_settings": "SELECT name,value FROM cs_settings ORDER BY name"}
    preserved = {}
    paths = (ROOT / "output/backups/before-checkout-order-fix-20261006-171436/runtime.db",
             ROOT / "build/installer-assets/xianyu_assistant.db")
    for table, query in tables.items():
        data = []
        for path in paths:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                data.append(connection.execute(query).fetchall())
        if data[0] != data[1]:
            raise AssertionError("Seed changed saved values: " + table)
        preserved[table] = len(data[1])
    report = {"success": True, "packaged_modules_match_current_source": checks,
              "pe_subsystems": pe, "preserved_seed_rows": preserved,
              "browser_started": False, "llm_started": False}
    path = ROOT / "output/checkout-order-packaged-validation-0.1.10.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": True, "modules": len(checks), "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
