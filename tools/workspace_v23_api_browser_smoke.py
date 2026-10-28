#!/usr/bin/env python3
"""Actual API/Store/Workflow browser chain on a temporary synthetic SQLite DB; only SDK is scripted."""
from __future__ import annotations
import argparse
from decimal import Decimal
from functools import partial
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright
from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
from tokensql.workspace.agent_routes import create_agent_router
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore
from workspace_browser_smoke import StaticHandler, STATIC


def entry(model_id, amount, supplier_id=None, **extra):
    return {"model_id":model_id,"amount":amount,"supplier_id":supplier_id,"billing_item":"input","billing_metric":"input",
            "pricing_unit_id":"cny_mtoken","pricing_conditions":{},"source_sheet":"Rates",**extra}


def workbook():
    book=Workbook();sheet=book.active;sheet.title="Rates";sheet.append(["model_id","price"]);sheet.append(["A","3"]);sheet.append(["B","4"])
    out=BytesIO();book.save(out);return out.getvalue()


class SDK:
    def __init__(self):
        self.actions=[[entry("A","2")],[entry("A","3"),entry("B","4")]]
        self.calls=0
    async def __call__(self,request):
        if request.url.path=="/global/health":return httpx.Response(200,json={"healthy":True})
        if request.url.path=="/session" and request.method=="POST":return httpx.Response(200,json={"id":"ses_v23_api_synthetic"})
        if request.method=="GET":return httpx.Response(200,json=[])
        if request.url.path.endswith("/abort"):return httpx.Response(200,json=True)
        assert request.url.path.endswith("/message"),request.url.path
        body=json.loads(request.content)
        directory=Path(re.search(r"^Use only this run directory for writable output: (.+)$",body["system"],re.MULTILINE).group(1))
        assert self.actions,"unexpected SDK call";rows=self.actions.pop(0);self.calls+=1
        atomic_json(directory/"proposal.json",{"bundle":{"create":rows},"reconciliation_request":{"complete_read":True,"covered_sheets":["Rates"],"unresolved_items":0}})
        return httpx.Response(200,json={"info":{"role":"assistant"},"parts":[{"type":"text","text":"合成报价已读取，等待审核。"}]})


def run(evidence_dir=None):
    with TemporaryDirectory(prefix="tokensql-v23-api-browser-") as tmp:
        tmp=Path(tmp);store=WorkspaceStore(tmp/"synthetic.sqlite3")
        supplier=store.confirm_supplier({"name":"合成供应商"});sid=supplier["supplier_id"]
        other=store.confirm_supplier({"name":"其他供应商"})
        seeded=store.apply_bundle({"create":[entry("A","1",sid,source_scope="monthly"),entry("C","5",sid,source_scope="monthly"),entry("Manual","9",sid),entry("Other","8",other["supplier_id"],source_scope="monthly")]})["items"]
        a,c=seeded[:2]
        config=AgentConfig(workspace_dir=tmp/"workspace",database_path=tmp/"synthetic.sqlite3",agent_dir=tmp/"agent",input_dir=tmp/"input",progress_dir=tmp/"progress",data_dir=tmp/"data",python_bin=Path("/usr/bin/python3"))
        sdk=SDK();manager=OpenCodeAgentManager(config=config,store=store,transport=httpx.MockTransport(sdk))
        app=FastAPI();app.include_router(create_data_router(store));app.include_router(create_agent_router(manager))
        server=ThreadingHTTPServer(("127.0.0.1",0),partial(StaticHandler,directory=str(STATIC)));Thread(target=server.serve_forever,daemon=True).start()
        calls=[];errors=[];api_errors=[];proposal_patches=[]
        with TestClient(app) as client,sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={"width":1440,"height":900});page.set_default_timeout(12000)
            page.on("pageerror",lambda error:errors.append(str(error)))
            def route(route):
                request=route.request;path=request.url.split("/api/workspace",1)[1]
                headers={key:value for key,value in request.headers.items() if key.lower() in {"content-type","accept"}}
                body=request.post_data_buffer
                response=client.request(request.method,"/api/workspace"+path,content=body,headers=headers)
                if request.method!="GET":calls.append({"method":request.method,"path":path,"status":response.status_code})
                if request.method=="PATCH" and path.endswith("/proposal"):proposal_patches.append(request.post_data_json)
                if response.status_code>=400:api_errors.append({"path":path,"status":response.status_code,"body":response.text})
                route.fulfill(status=response.status_code,headers={key:value for key,value in response.headers.items() if key.lower() not in {"content-length","content-encoding","transfer-encoding"}},body=response.content)
            page.route("**/api/workspace/**",route)
            page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace",wait_until="networkidle")
            def prepare(mode,version):
                page.locator("#supplierUpdateButton").click();expect(page.locator("#supplierUpdateDialog")).to_be_visible()
                page.locator("#updateSupplier").select_option(sid);page.locator("#updateSourceScope").fill("monthly");page.locator("#updateMode").select_option(mode)
                page.locator("#updateSheets").fill("Rates");page.locator("#updateVersion").fill(version)
                page.locator("#updateScopeConfirmed").check()
                if mode=="full":page.locator("#updateCompleteRead").check()
                with page.expect_file_chooser() as chosen:page.locator("#prepareSupplierUpdateButton").click()
                chosen.value.set_files({"name":"synthetic-"+version+".xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":workbook()})
                page.locator("#sendButton").click();expect(page.locator("#reviewDialog")).to_be_visible()
            def edit_proposal(amount, count):
                job=page.evaluate("TokenSQLReview.currentJob")
                assert len(job["proposal_bundle"]["create"])==count,job
                assert len(job["bundle"]["update"])==1,job
                page.get_by_label("\u65b0\u589e\u4f9b\u5e94\u5546\u62a5\u4ef7 1 \u539f\u5e01\u91d1\u989d",exact=True).fill(amount)
                page.get_by_label("\u65b0\u589e\u4f9b\u5e94\u5546\u62a5\u4ef7 1 \u6709\u6548\u5b9a\u4ef7\u4f9d\u636e",exact=True).select_option("amount")
                page.locator("#previewReviewButton").click()
                page.wait_for_function("revision => TokenSQLReview.currentJob.revision > revision",arg=job["revision"])
                updated=page.evaluate("TokenSQLReview.currentJob")
                assert updated["preview"]["ready"],updated
                assert len(updated["proposal_bundle"]["create"])==count,updated
                assert updated["proposal_bundle"]["create"][0]["amount"]==amount,updated
                assert updated["proposal_bundle"]["create"][0]["pricing_basis"]=="amount",updated
                assert updated["preview"]["groups"]["update"][0]["after"]["amount"]==amount,updated
                assert len(proposal_patches[-1]["bundle"]["create"])==count,proposal_patches
                assert not proposal_patches[-1]["bundle"].get("update"),proposal_patches
                return updated
            before=store.catalog_snapshot()["quotes"]
            prepare("incremental","v1")
            assert store.catalog_snapshot()["quotes"]==before
            job=page.evaluate("TokenSQLReview.currentJob")
            assert job["preview"]["ready"] and len(job["preview"]["groups"]["update"])==1 and not job["preview"]["groups"]["delete"],job
            incremental_amount="2.1250000000000000000000000001"
            job=edit_proposal(incremental_amount,1)
            assert store.catalog_snapshot()["quotes"]==before
            assert len(job["preview"]["groups"]["update"])==1 and not job["preview"]["groups"]["delete"],job
            page.locator("#confirmReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            assert store.get_quote(a["id"]).amount==Decimal(incremental_amount) and store.get_quote(c["id"]).amount==Decimal("5")
            assert store.get_quote(a["id"]).revision==a["revision"]+1
            before_full=store.catalog_snapshot()["quotes"]
            prepare("full","v2")
            assert store.catalog_snapshot()["quotes"]==before_full
            job=page.evaluate("TokenSQLReview.currentJob");groups=job["preview"]["groups"]
            assert job["preview"]["ready"],job
            assert {key:len(groups[key]) for key in ("create","update","delete")}=={"create":1,"update":1,"delete":1},groups
            assert groups["update"][0]["before"]["id"]==a["id"] and groups["delete"][0]["before"]["id"]==c["id"]
            expect(page.locator("#reviewPreview")).to_contain_text("变更前");expect(page.locator("#reviewPreview")).to_contain_text("变更后")
            full_amount="3.1250000000000000000000000001"
            job=edit_proposal(full_amount,2);groups=job["preview"]["groups"]
            assert store.catalog_snapshot()["quotes"]==before_full
            assert {key:len(groups[key]) for key in ("create","update","delete")}=={"create":1,"update":1,"delete":1},groups
            assert groups["update"][0]["before"]["id"]==a["id"] and groups["delete"][0]["before"]["id"]==c["id"]
            assert groups["create"][0]["after"]["model_id"]=="B",groups
            expect(page.locator("#reviewPreview")).to_contain_text(full_amount)
            if evidence_dir:
                page.screenshot(path=str(evidence_dir/"supplier-update-api-editor.jpg"),type="jpeg",quality=55)
                page.locator("#reviewPreview").scroll_into_view_if_needed()
                page.screenshot(path=str(evidence_dir/"supplier-update-api-review.jpg"),type="jpeg",quality=55)
            page.locator("#confirmReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            assert store.get_quote(a["id"]).amount==Decimal(full_amount) and store.get_quote(a["id"]).revision==a["revision"]+2
            assert {row["model_id"] for row in store.catalog_snapshot()["quotes"]}=={"A","B","Manual","Other"}
            assert sdk.calls==2 and len(proposal_patches)==2 and not errors and not api_errors,(errors,api_errors)
            browser.close();client.portal.call(manager.shutdown)
        server.shutdown();store.close()
        return {"type":"real_api_store_workflow_browser_with_scripted_sdk","database":"isolated TemporaryDirectory; removed on completion","checks":["incremental updates A in place and preserves absent C","review form edits amount and pricing basis on proposal source, then re-previews without dropping updates","29-digit Decimal amount survives browser form, API preview and persisted store exactly","full update previews exactly 1 modify + 1 create + 1 delete","one click applies transaction; A retains quote_id and revision increments twice","manual and other supplier quotations preserved","preview never writes business rows; synthetic XLSX only"],"sdk_calls":sdk.calls,"proposal_patches":len(proposal_patches),"mutations":calls,"page_errors":errors,"api_errors":api_errors}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir:args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir:(args.evidence_dir/"api-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
