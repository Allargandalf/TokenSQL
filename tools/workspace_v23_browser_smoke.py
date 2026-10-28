#!/usr/bin/env python3
"""v2.3 synthetic browser acceptance. Every API request is intercepted; no business DB or model service."""
from __future__ import annotations
import argparse
from copy import deepcopy
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import expect, sync_playwright
from workspace_browser_smoke import Fixture, StaticHandler, STATIC, PRICING_UNITS, model, quote


def official_price(identifier, metric, amount, conditions=None):
    return {"official_price_id": identifier, "model_id": "price-model", "billing_metric": metric,
            "billing_item": metric, "pricing_conditions": conditions or {}, "amount": str(amount),
            "pricing_unit_id": "cny_mtoken", "currency": "CNY", "unit": "token", "unit_quantity": "1000000",
            "source": "Synthetic official", "source_ref": "Price!B2", "revision": 1}


def offer(identifier, **changes):
    value = quote(identifier)
    value.update(model_id="price-model", model_name="Do not repeat this name", supplier_id="supplier-a",
                 supplier="甲供应商", billing_metric="input", billing_item="input", discount_scope="unknown",
                 pricing_conditions={}, discount_rate=None, raw_discount_rate=None, tags=[], supplier_model_id=None)
    value.update(changes)
    return value


class ComparisonFixture(Fixture):
    def __init__(self):
        super().__init__()
        names = [("supplier-a", "甲供应商"), ("supplier-b", "乙供应商"), ("supplier-c", "丙供应商")]
        self.suppliers = [{"supplier_id": identifier, "name": name, "aliases": [], "revision": 1} for identifier, name in names]
        self.prices = [official_price("input-price", "input", "20"), official_price("output-price", "output", "40"),
                       official_price("cache-price", "cache_read", "5")]
        self.quotes = [offer(index, supplier_id=names[index % 3][0], supplier=names[index % 3][1],
                             supplier_model_id="plan-"+str(index), amount=str(700-index)) for index in range(1, 651)]
        self.quotes += [offer(651, amount="0.001", supplier_model_id="tie-a", source_scope="monthly", source_entry_id="stable-a", pricing_basis="amount"),
                        offer(652, amount="0.001", supplier_id="supplier-b", supplier="乙供应商", supplier_model_id="tie-b")]
        self.page_reads = []
        self.waiting = None

    def catalog(self):
        return {"models": [model("price-model")], "official_prices": deepcopy(self.prices), "suppliers": deepcopy(self.suppliers)}

    def compute(self, item):
        value = super().compute(item)
        value["raw_discount_rate"] = value.get("discount_rate")
        return value

    def route(self, route):
        request = route.request
        path = urlparse(request.url).path.removeprefix("/api/workspace")
        data = request.post_data_json if request.method in {"POST", "PATCH"} and "application/json" in request.headers.get("content-type", "") else {}
        if path == "/quotes" and request.method == "GET":
            self.page_reads.append(parse_qs(urlparse(request.url).query))
        if path == "/imports" and self.waiting:
            route.fulfill(json={"items": [self.waiting]}); return
        if self.waiting and path == "/imports/" + self.waiting["id"]:
            route.fulfill(json=self.waiting); return
        if self.waiting and path.endswith("/respond"):
            assert data["supplier_id"] == "supplier-a" and data["expected_revision"] == 7
            self.mutations.append((request.method, path, deepcopy(data)))
            self.waiting = {**self.waiting, "status": "ready", "revision": 8, "bundle": {"create": []}, "preview": {"items": []}}
            route.fulfill(json=self.waiting); return
        if path == "/suppliers":
            route.fulfill(json={"items": self.suppliers}); return
        super().route(route)


def evaluate_candidates(page, values):
    return page.evaluate("""(values) => TokenSQLComparison.buildCandidates(values).map(c => ({id:c.quote.id,metric:c.metric,
        amount:c.amount,discount:c.discount,strict:c.strict,estimated:c.estimated,reason:c.reason,scope:c.scope,group:c.groupKey,
        official:c.official?.official_price_id}))""", values)


def run(evidence_dir=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(STATIC)))
    Thread(target=server.serve_forever, daemon=True).start()
    fixture = ComparisonFixture()
    checks = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/workspace/**", fixture.route)
        page.goto("http://127.0.0.1:" + str(server.server_port) + "/workspace", wait_until="networkidle")
        expect(page.locator("#quoteRows tr")).to_have_count(1)
        expect(page.locator("#quoteRows")).to_contain_text("price-model")
        assert "Do not repeat this name" not in page.locator("#quoteRows").inner_text()
        assert page.locator("#metricFilter").input_value() == ""
        assert len(fixture.page_reads) >= 2 and fixture.page_reads[1]["offset"] == ["500"]
        checks.append("empty metric and full 652-candidate initial fetch")
        if evidence_dir: page.screenshot(path=str(evidence_dir/"overview-1440.jpg"),type="jpeg",quality=55)
        page.locator("button.model-link").click()
        expect(page.locator("#quoteRows tr")).to_have_count(100)
        first_ids = page.locator("#quoteRows tr").evaluate_all("rows => rows.slice(0,2).map(row=>Number(row.dataset.id))")
        assert set(first_ids) == {651, 652}, first_ids
        assert page.locator("#quoteRows .minimum-badge").count() == 2
        assert page.locator("#comparisonHead").inner_text().split() == ["供应商", "供应商价", "折扣", "适用条件摘要", "报价日期", "操作"]
        page.locator("#rankingSort").select_option("price_desc")
        assert page.locator("#quoteRows tr").first.get_attribute("data-id") == "1"
        page.locator("#nextPageButton").click()
        expect(page.locator("#quoteRows tr").first).to_have_attribute("data-id", "101")
        page.locator("#exportFormat").select_option("csv")
        with page.expect_download(): page.locator("#exportFilteredButton").click()
        rows = fixture.view_exports[-1]["rows"]
        assert len(rows) == 652 and rows[0]["quote_id"] == 1 and rows[-1]["supplier_price"] == "0.001"
        assert [row["quote_id"] for row in rows] == page.evaluate("TokenSQLComparison.rows.map(row=>row.quote.id)")
        assert sum(row["minimum"] for row in rows) == 2
        checks.append("global ascending/descending sorting, tie minima, paging and 652-row display/export agreement")
        page.locator("#rankingSort").select_option("price_asc")
        page.locator("#quoteRows tr").first.get_by_role("button", name="详情", exact=True).click()
        expect(page.locator("#officialDialog")).to_be_visible()
        expect(page.locator("#officialDialogContent")).to_contain_text("tie-")
        page.locator("#closeOfficialButton").click()
        page.locator("#quoteRows tr").first.get_by_role("button",name="\u7f16\u8f91",exact=True).click()
        expect(page.locator('#quoteForm [name="source_scope"]')).to_have_value("monthly")
        expect(page.locator('#quoteForm [name="source_entry_id"]')).to_have_value("stable-a")
        expect(page.locator('#quoteForm [name="pricing_basis"]')).to_have_value("amount")
        page.locator("#editorDialog [data-close-editor]").first.click()
        page.locator("#newQuoteButton").click()
        for field in ("model_id","supplier","source_scope","source_entry_id","pricing_basis"):
            expect(page.locator('#quoteForm [name="'+field+'"]')).to_have_value("")
        page.locator("#editorDialog [data-close-editor]").first.click()
        checks.append("new quotation resets prior source scope, source entry and pricing basis")
        for width, height in [(1440,900),(1280,720)]:
            page.set_viewport_size({"width": width, "height": height})
            geometry = page.evaluate("""() => ({body:document.body.scrollWidth,width:innerWidth,
                composer:document.querySelector('#composer').getBoundingClientRect().toJSON(),
                table:document.querySelector('#tableWrap').getBoundingClientRect().toJSON()})""")
            assert geometry["body"] <= width and geometry["composer"]["bottom"] <= height
            assert geometry["table"]["height"] > 200, geometry
            if evidence_dir:
                page.screenshot(path=str(evidence_dir / f"comparison-{width}.jpg"),type="jpeg",quality=55)
        checks.append("compact readable desktop geometry at 1440x900 and 1280x720")
        # Whole-model rules expand only in memory. An amount-only item exception removes the default candidate.
        default = offer(800, billing_metric=None, billing_item=None, amount=None, discount_rate="0.70", discount_scope="model")
        exception = offer(801, amount="18")
        candidates = evaluate_candidates(page, [default, exception])
        assert not any(row["id"] == 800 and row["metric"] == "input" for row in candidates), candidates
        assert any(row["id"] == 800 and row["metric"] == "output" and row["amount"] == "28" for row in candidates)
        assert any(row["id"] == 801 and row["metric"] == "input" and row["amount"] == "18" for row in candidates)
        conflict = evaluate_candidates(page, [offer(802, amount="2"), offer(803, amount="3")])
        assert all(not row["strict"] and "冲突" in row["reason"] for row in conflict)
        checks.append("whole-model expansion, amount-only item override and same-priority conflict exclusion")
        # Tax, context endpoints, cache TTL, market and time form independent comparison scenes.
        for key, values in {"tax_status": ["included","excluded","unknown"], "market": ["domestic","international"],
                            "time_band": ["busy","idle"], "cache_ttl_seconds": [300,3600],
                            "input_tokens": [{"min":0,"max":128000,"max_inclusive":True},{"min":128000,"min_inclusive":False} ]}.items():
            offers = [offer(900+i, model_id="no-official", supplier_model_id="separate"+str(i), pricing_conditions={key:value}, amount="2") for i,value in enumerate(values)]
            candidates = evaluate_candidates(page, offers)
            assert len({row["group"] for row in candidates}) == len(values), (key,candidates)
        missing = evaluate_candidates(page, [offer(910, model_id="no-official", amount="2"), offer(911, model_id="no-official", amount="3", supplier_id="supplier-b", supplier="乙供应商")])
        assert all(row["strict"] for row in missing), missing
        unresolved = evaluate_candidates(page, [offer(912, pricing_conditions={"raw":{"membership":"unclear"}})])
        assert all(not row["strict"] for row in unresolved)
        discount_only = evaluate_candidates(page, [offer(913, model_id="no-official", billing_metric=None,billing_item=None,amount=None,discount_rate="0.50",discount_scope="model")])
        assert discount_only[0]["amount"] is None and not discount_only[0]["strict"]
        checks.append("tax/context/TTL/market/time isolation, no-official actual amount and unresolved conditions")
        # A precise amount which happens to be the smallest may still be only an estimate.
        page.evaluate("""() => {TokenSQLConversion.state.scale='mtoken';TokenSQLConversion.state.customFactors.set('price-model\\u0000input',
            {tokens_per_second:TokenSQLConversion._exact.parseDecimal('100'),source:'synthetic benchmark'});} """)
        estimated = evaluate_candidates(page,[offer(920,amount="0.0000001",pricing_unit_id="cny_second",unit="second",unit_quantity="1")])
        assert estimated[0]["estimated"] and not estimated[0]["strict"], estimated
        page.evaluate("TokenSQLConversion.state.scale=null")
        basis = evaluate_candidates(page,[offer(921,amount="15",discount_rate="0.50",pricing_basis="discount_rate")])
        assert basis[0]["amount"] == "10", basis
        dated = page.evaluate("""(values) => TokenSQLComparison.sortCandidates(TokenSQLComparison.buildCandidates(values),'date_asc').map(c=>c.quote.id)""", [offer(930,amount="1",supplier_model_id="date-a",effective_date="2026-10-01",updated_at="2026-10-28T00:00:00Z"),offer(931,amount="1",supplier_model_id="date-b",effective_date="2026-10-20",updated_at="2026-10-02T00:00:00Z"),offer(932,amount="1",supplier_model_id="date-c",updated_at="2026-01-01T00:00:00Z")])
        assert dated == [930,931,932], dated
        checks.append("estimate never strict minimum; explicit discount basis and effective-date sorting")
        # Waiting cards use the same conversation scroll area and survive refreshing the selected session.
        waiting = {"id":"waiting-v23","conversation_id":"saved-session","status":"waiting_for_input","revision":7,"attempt":2,
                   "questions":["问题"+str(index)+"："+"很长的单元格条件说明。"*45 for index in range(1,13)],
                   "question_summary":"请确认本批供应商与适用范围", "waiting_reason":"supplier", "files":[{"id":"f1","filename":"synthetic.xlsx"}]}
        fixture.waiting = waiting
        page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace?session=saved-session",wait_until="networkidle")
        expect(page.locator("#waitingPanel")).to_be_visible()
        assert page.locator("#conversation #waitingPanel").count() == 1
        assert page.locator("#waitingQuestions li").count() == 12
        geometry = page.evaluate("""() => ({waitStyle:getComputedStyle(document.querySelector('#waitingPanel')).overflowY,
            composer:document.querySelector('#composer').getBoundingClientRect().toJSON(),height:innerHeight})""")
        assert geometry["waitStyle"] == "visible" and geometry["composer"]["bottom"] <= geometry["height"]
        if evidence_dir: page.screenshot(path=str(evidence_dir/"waiting-1280.jpg"),type="jpeg",quality=55)
        page.locator("#horizontalSplitter").focus();page.keyboard.press("End")
        assert page.evaluate("document.body.scrollWidth <= innerWidth")
        page.locator("#resetLayoutButton").click()
        page.locator("#horizontalSplitter").focus();page.keyboard.press("ArrowLeft")
        assert page.evaluate("TokenSQLLayout.preference.fraction") == 64
        page.locator("#verticalSplitter").focus();page.keyboard.press("ArrowUp")
        assert page.evaluate("TokenSQLLayout.preference.composer") > 160
        box=page.locator("#horizontalSplitter").bounding_box();page.mouse.move(box["x"]+box["width"]/2,box["y"]+box["height"]/2);page.mouse.down();page.mouse.move(box["x"]-50,box["y"]+box["height"]/2);page.mouse.up()
        assert page.evaluate("TokenSQLLayout.preference.fraction") < 64
        page.locator("#resetLayoutButton").click();assert page.evaluate("TokenSQLLayout.preference.fraction") == 66
        page.locator("#toggleChatButton").click();assert page.locator(".agent-panel").is_hidden();page.locator("#toggleChatButton").click()
        page.set_viewport_size({"width":390,"height":760});page.locator("#showChatButton").click()
        expect(page.locator(".agent-panel")).to_be_visible();expect(page.locator(".data-panel")).to_be_hidden()
        assert page.locator("#composer").bounding_box()["y"]+page.locator("#composer").bounding_box()["height"] <= 760
        assert page.locator("#sendButton").bounding_box()["y"]+page.locator("#sendButton").bounding_box()["height"] <= page.locator("#composer").bounding_box()["y"]+page.locator("#composer").bounding_box()["height"]
        if evidence_dir: page.screenshot(path=str(evidence_dir/"waiting-mobile.jpg"),type="jpeg",quality=55)
        page.locator("#waitingSupplierSelect").select_option("supplier-a")
        page.locator("#promptInput").fill("请按已确认供应商继续。")
        page.locator("#sendButton").click()
        expect(page.locator("#reviewDialog")).to_be_visible()
        assert any(path.endswith("/respond") for _,path,_ in fixture.mutations)
        assert not any(path == "/imports/start" for _,path,_ in fixture.mutations)
        checks.append("long waiting card in one scroller, refresh restore, same-job supplier response, desktop/mobile drag and reset")
        assert not errors, errors
        browser.close()
    server.shutdown()
    return {"type":"mock_browser_synthetic_only","checks":checks,"api_mutations_intercepted":len(fixture.mutations),"page_errors":errors}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir: args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir: (args.evidence_dir/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
