#!/usr/bin/env python3
"""Live v2.4 repeated/delta/full-read browser chain; isolated DB and scripted SDK, no Qwen timing claims."""
from __future__ import annotations
import argparse
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

def workbook(rows):
    book=Workbook();sheet=book.active;sheet.title="Rates";sheet.append(["source_entry_id","model_id","CNY / million tokens"])
    for identifier,model_id,amount in rows:sheet.append([identifier,model_id,amount])
    out=BytesIO();book.save(out);return out.getvalue()
def entry(identifier,model_id,amount,row):
    return {"model_id":model_id,"amount":str(amount),"billing_item":"input","billing_metric":"input","pricing_unit_id":"cny_mtoken","source_entry_id":identifier,"source_sheet":"Rates","source_ref":"Rates!C"+str(row)}
class SDK:
    def __init__(self):
        self.actions=[[entry("a","A",1,2),entry("b","B",2,3)],[entry("a","A",4,2),entry("e","E",5,4)],[entry("a","A",4,2),entry("b","B",2,3),entry("e","E",5,4)]]
        self.calls=[];self.sessions=[]
    async def __call__(self,request):
        if request.url.path=="/global/health":return httpx.Response(200,json={"healthy":True})
        if request.url.path=="/session" and request.method=="POST":
            session={"id":"ses_v24_delta_"+str(len(self.sessions)+1),"title":json.loads(request.content)["title"]};self.sessions.append(session);return httpx.Response(200,json=session)
        if request.method=="GET":return httpx.Response(200,json=[])
        if request.url.path.endswith("/abort"):return httpx.Response(200,json=True)
        assert request.url.path.endswith("/message"),request.url.path
        body=json.loads(request.content);directory=Path(re.search(r"^Use only this run directory for writable output: (.+)$",body["system"],re.MULTILINE).group(1))
        assert self.actions,"unexpected semantic SDK request"
        rows=self.actions.pop(0);self.calls.append({"system_chars":len(body["system"]),"rows":len(rows),"changed_context":(directory/"changed_context.json").exists()})
        atomic_json(directory/"proposal.json",{"bundle":{"create":rows},"reconciliation_request":{"complete_read":True,"covered_sheets":["Rates"],"unresolved_items":0}})
        return httpx.Response(200,json={"info":{"role":"assistant","tokens":{"input":100,"output":20}},"parts":[{"type":"text","text":"合成报价已读取，等待审核。"}]})

def signature(job):
    return sorted((row["model_id"],str(row["amount"]),row.get("pricing_unit_id"),row.get("source_entry_id")) for row in job["proposal_bundle"]["create"])
def run(evidence_dir=None):
    checks=[];errors=[];api_errors=[];detail_reads=[];metrics={}
    with TemporaryDirectory(prefix="tokensql-v24-delta-browser-") as tmp:
        tmp=Path(tmp);store=WorkspaceStore(tmp/"synthetic.sqlite3")
        supplier=store.confirm_supplier({"name":"合成月度供应商"});sid=supplier["supplier_id"]
        config=AgentConfig(workspace_dir=tmp/"workspace",database_path=tmp/"synthetic.sqlite3",agent_dir=tmp/"agent",input_dir=tmp/"input",progress_dir=tmp/"progress",data_dir=tmp/"data",python_bin=Path("/usr/bin/python3"))
        sdk=SDK();manager=OpenCodeAgentManager(config=config,store=store,transport=httpx.MockTransport(sdk))
        app=FastAPI();app.include_router(create_data_router(store));app.include_router(create_agent_router(manager))
        server=ThreadingHTTPServer(("127.0.0.1",0),partial(StaticHandler,directory=str(STATIC)));Thread(target=server.serve_forever,daemon=True).start()
        original=workbook([("a","A",1),("b","B",2)]);updated=workbook([("a","A",4),("b","B",2),("e","E",5)])
        with TestClient(app) as client,sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True);page=browser.new_page(viewport={"width":1440,"height":1000});page.set_default_timeout(20000)
            page.on("pageerror",lambda error:errors.append(str(error)))
            def route(route):
                request=route.request;path=request.url.split("/api/workspace",1)[1]
                response=client.request(request.method,"/api/workspace"+path,content=request.post_data_buffer,headers={key:value for key,value in request.headers.items() if key.lower() in {"content-type","accept"}})
                if request.method=="GET" and re.fullmatch("/imports/[a-z0-9]+",path):detail_reads.append(path)
                if response.status_code>=400:api_errors.append({"path":path,"status":response.status_code,"body":response.text})
                route.fulfill(status=response.status_code,body=response.content,headers={key:value for key,value in response.headers.items() if key.lower() not in {"content-length","content-encoding","transfer-encoding"}})
            page.route("**/api/workspace/**",route)
            page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace",wait_until="networkidle")
            page.locator(".import-preferences summary").click();page.locator("#persistProfile").uncheck()
            def prepare(raw,version,full=False):
                page.locator("#supplierUpdateButton").click();expect(page.locator("#supplierUpdateDialog")).to_be_visible()
                page.locator("#updateSupplier").select_option(sid);page.locator("#updateSourceScope").fill("monthly");page.locator("#updateMode").select_option("incremental")
                page.locator("#updateSheets").fill("Rates");page.locator("#updateVersion").fill(version);page.locator("#updateScopeConfirmed").check()
                with page.expect_file_chooser() as chosen:page.locator("#prepareSupplierUpdateButton").click()
                chosen.value.set_files({"name":"monthly.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":raw})
                page.locator("#promptInput").fill("按表中来源编号对应模型，人民币 / 百万 token，输入价；增量更新，缺行保留。")
                if not page.locator("#fullReparse").is_visible():page.locator(".import-preferences summary").click()
                page.locator("#fullReparse").set_checked(full);page.locator("#persistProfile").uncheck()
                page.locator("#sendButton").click();expect(page.locator("#reviewDialog")).to_be_visible()
                return page.evaluate("TokenSQLReview.currentJob")
            first=prepare(original,"v1")
            assert len(sdk.calls)==1 and len(first["proposal_bundle"]["create"])==2,first
            page.locator("#confirmReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            baseline=store.get_supplier_baseline(sid,"monthly");assert baseline and baseline["revision"]==1,baseline
            initial={row["model_id"]:row for row in store.catalog_snapshot()["quotes"]}
            repeat=prepare(original,"v1")
            assert len(sdk.calls)==1 and repeat["metrics"]["semantic_requests"]==0,repeat
            assert repeat["metrics"]["reused_entries"]==2 and repeat["metrics"]["reinterpreted_entries"]==0,repeat
            expect(page.locator("#reviewSourceComparison")).to_contain_text("原表内容一致")
            page.reload(wait_until="networkidle");expect(page.locator("#reviewDialog")).to_be_visible()
            expect(page.locator("#reviewSourceComparison .source-table-pane")).to_have_count(2)
            assert page.evaluate("TokenSQLReview.currentJob.id")==repeat["id"]
            page.locator("#cancelReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            assert store.get_supplier_baseline(sid,"monthly")["revision"]==1
            checks.append("confirmed first source becomes baseline; repeated workbook uses zero semantic requests and survives lazy-detail refresh; cancel preserves baseline")
            metrics["repeat"]=repeat["metrics"]
            delta=prepare(updated,"v2")
            assert len(sdk.calls)==2 and delta["metrics"]["semantic_requests"]==1,delta
            assert delta["metrics"]["reused_entries"]==1 and delta["metrics"]["reinterpreted_entries"]==2,delta
            assert len(delta["proposal_bundle"]["create"])==3,delta
            expect(page.locator("#reviewSourceComparison .source-table-pane")).to_have_count(2)
            expect(page.locator("#reviewSourceComparison")).to_contain_text("重新理解条目")
            assert page.locator("#reviewSourceComparison .source-cell-modified").count()>0
            assert page.locator("#reviewSourceComparison .source-cell-added").count()>0
            if evidence_dir:page.screenshot(path=str(evidence_dir/"delta-review-desktop.jpg"),type="jpeg",quality=65)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            if evidence_dir:page.screenshot(path=str(evidence_dir/"delta-review-mobile.jpg"),type="jpeg",quality=65)
            page.set_viewport_size({"width":1440,"height":1000})
            page.locator("#confirmReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            final={row["model_id"]:row for row in store.catalog_snapshot()["quotes"]}
            assert final["A"]["id"]==initial["A"]["id"] and final["B"]["id"]==initial["B"]["id"]
            assert {key:str(value["amount"]) for key,value in final.items()}=={"A":"4","B":"2","E":"5"}
            assert store.get_supplier_baseline(sid,"monthly")["revision"]==2
            checks.append("delta model receives only changed/new source entries; UI shows one reused plus two reinterpreted and confirmation preserves quote IDs")
            metrics["delta"]=delta["metrics"]
            full=prepare(updated,"v2",True)
            assert len(sdk.calls)==3 and full["metrics"]["reused_entries"]==0 and full["metrics"]["reinterpreted_entries"]==3,full
            assert signature(full)==signature(delta),(signature(full),signature(delta))
            page.locator("#cancelReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            assert store.get_supplier_baseline(sid,"monthly")["revision"]==2
            metrics["full"]=full["metrics"]
            checks.append("explicit whole-sheet reinterpretation matches delta candidates; cancellation leaves confirmed database and baseline unchanged")
            assert not errors and not api_errors,(errors,api_errors)
            browser.close();client.portal.call(manager.shutdown)
        server.shutdown();store.close()
    return {"type":"real_api_store_workflow_browser_scripted_sdk","database":"isolated TemporaryDirectory removed on completion","checks":checks,"sdk_calls":len(sdk.calls),"job_detail_reads":len(detail_reads),"measured_metrics":metrics,"metric_scope":"actual scripted-SDK test processing, not real Qwen/business speed evidence","page_errors":errors,"api_errors":api_errors}
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir:args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir:(args.evidence_dir/"delta-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
