#!/usr/bin/env python3
"""Read-only browser smoke test for the TokenSQL workspace.

Agent and quote APIs are intercepted in the browser. The target service only
serves the workspace HTML/CSS/JS, so this test never mutates its database.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Route, expect, sync_playwright


def quote(quote_id: int, model_name: str) -> dict[str, object]:
    return {
        "id": quote_id,
        "model_name": model_name,
        "model_version": None,
        "manufacturer": "Smoke Labs",
        "provider": "Smoke Provider",
        "provider_type": "reseller",
        "amount": "1.25",
        "currency": "CNY",
        "unit": "token",
        "unit_quantity": "1000000",
        "metric": "input_tokens",
        "dimension": "token",
        "direction": "input",
        "component_type": "monetary",
        "discount_rate": None,
        "conditions": {},
        "effective_date": None,
        "source": "browser-smoke",
        "source_ref": None,
        "note": None,
        "revision": 1,
        "updated_at": "2026-10-09T00:00:00Z",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()

    url = args.url.rstrip("/")
    state = {"posts": 0, "failed_polled": False}
    page_errors: list[str] = []
    console_errors: list[str] = []

    def api_stub(route: Route) -> None:
        request_url = route.request.url
        if "/api/workspace/quotes" in request_url:
            items = [quote(1, "Smoke Model A"), quote(2, "Smoke Model B")]
            if state["failed_polled"]:
                items.append(quote(3, "Persisted Before Failure"))
            route.fulfill(json={
                "items": items,
                "total": len(items),
                "filter_options": {
                    "manufacturers": ["Smoke Labs"],
                    "providers": ["Smoke Provider"],
                    "currencies": ["CNY"],
                    "metrics": ["input_tokens"],
                    "dimensions": ["token"],
                },
            })
        elif request_url.endswith("/api/workspace/agent/status"):
            route.fulfill(json={"available": True, "model": "smoke-agent", "status": "ready"})
        elif request_url.endswith("/api/workspace/agent/messages"):
            assert route.request.method == "POST"
            assert "验证失败后的变更刷新" in (route.request.post_data or "")
            state["posts"] += 1
            route.fulfill(status=202, json={
                "run_id": "smoke-failed",
                "conversation_id": "smoke-conversation",
                "status": "queued",
            })
        elif request_url.endswith("/api/workspace/agent/runs/smoke-failed"):
            state["failed_polled"] = True
            route.fulfill(json={
                "run_id": "smoke-failed",
                "conversation_id": "smoke-conversation",
                "status": "failed",
                "assistant_message": None,
                "messages": [{
                    "role": "tool",
                    "kind": "tool",
                    "name": "tokensql_cli",
                    "status": "failed",
                    "text": "报价已写入，后续校验失败",
                }],
                "changes": {"added": 1, "updated": 0, "deleted": 0, "total": 1},
                "error": "模拟后续响应失败",
            })
        else:
            route.continue_()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.route("**/api/workspace/**", api_stub)
        page.goto(f"{url}/workspace", wait_until="networkidle")

        expect(page.locator("#quoteRows tr")).to_have_count(2)
        assert len(page.locator(".content-grid").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns.split(' ')"
        )) == 2

        page.locator("#promptInput").fill("验证失败后的变更刷新")
        page.locator("#sendButton").click()
        expect(page.locator("#conversation .run-progress")).to_have_count(0, timeout=15_000)
        expect(page.locator("#conversation .message.assistant").last).to_contain_text("执行失败")
        expect(page.locator("#conversation .message.assistant").last).to_contain_text("新增 1")
        expect(page.locator("#conversation .tool-summary")).to_contain_text("查看执行步骤（1）")
        expect(page.locator("#resultCount")).to_contain_text("3 条报价")
        expect(page.locator("#sendButton")).to_be_enabled()
        assert state == {"posts": 1, "failed_polled": True}

        if args.evidence_dir:
            args.evidence_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.evidence_dir / "workspace-smoke-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        assert len(page.locator(".content-grid").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns.split(' ')"
        )) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if args.evidence_dir:
            page.screenshot(path=str(args.evidence_dir / "workspace-smoke-mobile.png"), full_page=True)
        browser.close()

    assert not page_errors, page_errors
    assert not console_errors, console_errors
    print("workspace browser smoke passed: POST, tool summary, failed changes refresh, responsive layout")


if __name__ == "__main__":
    main()
