"""Verify the reply-question release without opening a browser or user profile."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from validate_checkout_release import canonical, subsystem

from xianyu_assistant import __version__

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    executable = ROOT / "dist/XianyuAssistant/XianyuAssistant.exe"
    archive = CArchiveReader(str(executable))
    bundled = archive.open_embedded_archive(next(k for k in archive.toc if k.endswith(".pyz")))
    modules = ("xianyu_assistant", *("xianyu_assistant." + name for name in (
        "customer_service.battery_models", "customer_service.reply_questions",
        "customer_service.semantic_analysis", "customer_service.fact_guard",
        "customer_service.customer_service_worker", "customer_service.quality_export",
        "persistence.customer_service_repository", "customer_service.message_roles",
        "customer_service.contact_policy", "customer_service.checkout",
        "customer_service.send_checks", "customer_service.send_reliability",
        "customer_service.negotiation", "customer_service.price_reliability",
        "customer_service.current_catalog", "customer_service.media_cache",
        "customer_service.browser_adapter", "browser.executor_supervisor",
        "browser.executor_child", "ui.customer_service_panel", "persistence.seed_data",
    )))
    hashes = {}
    for name in modules:
        relative = Path(*name.split("."))
        source = ROOT / "src" / (relative / "__init__.py" if name == "xianyu_assistant"
                                 else relative.with_suffix(".py"))
        expected = compile(source.read_text(encoding="utf-8-sig"), str(source), "exec",
                           dont_inherit=True, optimize=0)
        assert canonical(expected) == canonical(bundled.extract(name)), f"Stale module: {name}"
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    pe = {"application": subsystem(executable), "playwright_node": subsystem(
        executable.parent / "_internal/playwright/driver/node.exe")}
    assert set(pe.values()) == {2}
    live_database = Path(os.environ["LOCALAPPDATA"]) / "XianyuAssistant/xianyu_assistant.db"
    seed_database = ROOT / "build/installer-assets/xianyu_assistant.db"
    comparisons = {}
    queries = {
        "cs_products": "SELECT * FROM cs_products ORDER BY product_key",
        "cs_settings": "SELECT name,value FROM cs_settings ORDER BY name",
        "cs_quality_messages": "SELECT COUNT(*) FROM cs_quality_messages",
        "cs_quality_sends": "SELECT COUNT(*) FROM cs_quality_sends",
        "cs_customer_turns": "SELECT COUNT(*) FROM cs_customer_turns",
    }
    for table, query in queries.items():
        rows = []
        for path in (live_database, seed_database):
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                rows.append(db.execute(query).fetchall())
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert rows[0] == rows[1], f"Saved data differs: {table}"
        comparisons[table] = len(rows[0]) if table in {"cs_products", "cs_settings"} else rows[0][0][0]
    installer = ROOT / f"dist/XianyuAssistant-Setup-{__version__}-win64.exe"
    assert installer.is_file()
    with installer.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    installer.with_suffix(".sha256.txt").write_text(checksum + "  " + installer.name + "\n",
                                                   encoding="ascii")
    report = {"success": True, "version": __version__, "packaged_source": hashes,
              "pe_subsystems": pe, "preserved_business_and_audit_rows": comparisons,
              "installer": str(installer), "installer_bytes": installer.stat().st_size,
              "installer_sha256": checksum, "browser_started": False,
              "llm_called": False, "message_submissions": 0}
    target = ROOT / "output/reply-question-fix-validation/packaged-validation.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": True, "version": __version__, "modules": len(modules),
                      "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
