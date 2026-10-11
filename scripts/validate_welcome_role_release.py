"""Check packed source, windowed executables and business seed without starting the app."""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from validate_checkout_release import canonical, subsystem

from xianyu_assistant import __version__
from xianyu_assistant.customer_service.message_roles import ROLE_READER_JS

ROOT = Path(__file__).resolve().parents[1]


def validate_javascript() -> int:
    def constant(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id == "ROLE_READER_JS":
            return ROLE_READER_JS
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = constant(node.left), constant(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    source = ROOT / "src/xianyu_assistant/customer_service/browser_adapter.py"
    expressions = []
    for call in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
        if (isinstance(call, ast.Call) and call.args
                and ((isinstance(call.func, ast.Attribute) and call.func.attr in {"evaluate", "evaluate_all"})
                     or (isinstance(call.func, ast.Name) and call.func.id == "evaluate_all"))):
            expression = constant(call.args[0])
            if expression is not None:
                expressions.append(expression)
    assert sum(ROLE_READER_JS in item for item in expressions) >= 2
    subprocess.run([str(ROOT / ".venv/Lib/site-packages/playwright/driver/node.exe"), "-e",
                    "for(const s of JSON.parse(require('fs').readFileSync(0,'utf8'))) new Function('return ('+s+')');"],
                   input=json.dumps(expressions), text=True, check=True, capture_output=True)
    return len(expressions)


def main() -> None:
    js_count = validate_javascript()
    archive = CArchiveReader(str(ROOT / "dist/XianyuAssistant/XianyuAssistant.exe"))
    code_archive = archive.open_embedded_archive(next(key for key in archive.toc if key.endswith(".pyz")))
    modules = ("xianyu_assistant", *("xianyu_assistant." + item for item in (
        "customer_service.models", "customer_service.message_roles", "customer_service.contact_policy",
        "customer_service.customer_service_worker", "customer_service.browser_adapter",
        "customer_service.browser_adapter_proxy", "customer_service.send_reliability",
        "customer_service.send_checks", "customer_service.quality_export", "customer_service.checkout",
        "persistence.contact_storage", "persistence.customer_service_repository", "persistence.runtime_storage",
        "browser.executor_supervisor", "browser.executor_child", "ui.customer_service_panel",
    )))
    matches = {}
    for module in modules:
        relative = Path(*module.split("."))
        source = ROOT / "src" / (relative / "__init__.py" if module == "xianyu_assistant" else relative.with_suffix(".py"))
        expected = canonical(compile(source.read_text(encoding="utf-8-sig"), str(source), "exec", dont_inherit=True, optimize=0))
        assert expected == canonical(code_archive.extract(module)), "Stale packaged source: " + module
        matches[module] = hashlib.sha256(source.read_bytes()).hexdigest()
    pe = {name: subsystem(ROOT / "dist/XianyuAssistant" / path) for name, path in {
        "application": "XianyuAssistant.exe", "playwright_node": "_internal/playwright/driver/node.exe",
    }.items()}
    assert all(value == 2 for value in pe.values())
    business = {
        "cs_products": "SELECT * FROM cs_products ORDER BY product_key",
        "cs_settings": "SELECT name,value FROM cs_settings ORDER BY name",
    }
    preserved = {}
    paths = (ROOT / "output/backups/before-welcome-role-fix-20261007-211737/runtime.db",
             ROOT / "build/installer-assets/xianyu_assistant.db")
    for table, query in business.items():
        rows = []
        for path in paths:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                rows.append(connection.execute(query).fetchall())
        assert rows[0] == rows[1], "Business seed changed: " + table
        preserved[table] = len(rows[1])
    audit_counts = {}
    for table in ("cs_quality_messages", "cs_quality_sends", "cs_run_sessions"):
        counts = []
        for path in paths:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                counts.append(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        assert counts[0] == counts[1], "Local audit record count changed: " + table
        audit_counts[table] = counts[1]
    installer = ROOT / f"dist/XianyuAssistant-Setup-{__version__}-win64.exe"
    assert installer.is_file()
    sha256 = hashlib.file_digest(installer.open("rb"), "sha256").hexdigest()
    report = {"success": True, "version": __version__, "packaged_source": matches,
              "javascript_expressions_parsed": js_count, "pe_subsystems": pe,
              "preserved_business_rows": preserved, "preserved_local_audit_counts": audit_counts,
              "installer_sha256": sha256, "installer_bytes": installer.stat().st_size,
              "browser_started": False, "llm_started": False, "message_submissions": 0}
    target = ROOT / f"output/welcome-role-packaged-validation-{__version__}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    installer.with_suffix(".sha256").write_text(sha256 + "  " + installer.name + "\n", encoding="ascii")
    print(json.dumps({"success": True, "version": __version__, "modules": len(modules), "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
