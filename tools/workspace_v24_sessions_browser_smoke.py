#!/usr/bin/env python3
"""v2.4 supplier-routed browser acceptance using temporary SQLite and scripted SDK only."""
from __future__ import annotations
import argparse
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
from tokensql.workspace.agent_routes import create_agent_router
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.memory import atomic_json
from tokensql.workspace.store import WorkspaceStore
from workspace_browser_smoke import StaticHandler, STATIC
from workspace_v23_api_browser_smoke import entry, workbook

class SDK:
    def __init__(self):self.sessions=[];self.calls=[]
    async def __call__(self,request):
        if request.url.path=="/global/health":return httpx.Response(200,json={"healthy":True})
        if request.url.path=="/session" and request.method=="POST":
            session={"id":"ses_v24_"+str(len(self.sessions)+1),"title":json.loads(request.content)["title"]}
            self.sessions.append(session);return httpx.Response(200,json=session)
        if request.method=="GET":return httpx.Response(200,json=[])
        if request.url.path.endswith("/abort"):return httpx.Response(200,json=True)
        assert request.url.path.endswith("/message"),request.url.path
        body=json.loads(request.content)
        directory=Path(re.search(r"^Use only this run directory for writable output: (.+)$",body["system"],re.MULTILINE).group(1))
        session=request.url.path.split("/")[2]
        previous=sum(call["session"]==session for call in self.calls)
        self.calls.append({"session":session,"body":body,"directory":str(directory)})
        if session=="ses_v24_1" and not previous:
            atomic_json(directory/"needs_input.json",{"questions":["请确认本期价格单位。"],"summary":"确认单位后继续"})
            text="请确认本期价格单位。"
        else:
            if session=="ses_v24_2":await asyncio.sleep(1.5)
            atomic_json(directory/"proposal.json",{"bundle":{"create":[entry("A" if session=="ses_v24_1" else "B","3")]}})
            text="合成报价已读取，等待审核。"
        return httpx.Response(200,json={"info":{"role":"assistant","tokens":{"input":100,"output":20}},"parts":[{"type":"text","text":text}]})

def run(evidence_dir=None):
    checks=[];errors=[];calls=[];api_errors=[]
    with TemporaryDirectory(prefix="tokensql-v24-sessions-browser-") as tmp:
        tmp=Path(tmp);store=WorkspaceStore(tmp/"synthetic.sqlite3")
        a=store.confirm_supplier({"name":"甲供应商"});b=store.confirm_supplier({"name":"乙供应商"})
        sid=a["supplier_id"];bid=b["supplier_id"]
        store.update_supplier_profile(sid,{"expected_revision":0,"default_prompt":"甲供应商默认要求","sources":{"monthly":{"default_prompt":"甲供应商月度要求"}}})
        store.update_supplier_profile(bid,{"expected_revision":0,"default_prompt":"乙供应商默认要求"})
        config=AgentConfig(workspace_dir=tmp/"workspace",database_path=tmp/"synthetic.sqlite3",agent_dir=tmp/"agent",input_dir=tmp/"input",progress_dir=tmp/"progress",data_dir=tmp/"data",python_bin=Path("/usr/bin/python3"))
        sdk=SDK();manager=OpenCodeAgentManager(config=config,store=store,transport=httpx.MockTransport(sdk))
        app=FastAPI();app.include_router(create_data_router(store));app.include_router(create_agent_router(manager))
        server=ThreadingHTTPServer(("127.0.0.1",0),partial(StaticHandler,directory=str(STATIC)));Thread(target=server.serve_forever,daemon=True).start()
        with TestClient(app) as client,sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={"width":1440,"height":900});page.set_default_timeout(12000)
            page.on("pageerror",lambda error:errors.append(str(error)))
            def route(route):
                request=route.request;path=request.url.split("/api/workspace",1)[1]
                headers={key:value for key,value in request.headers.items() if key.lower() in {"content-type","accept"}}
                response=client.request(request.method,"/api/workspace"+path,content=request.post_data_buffer,headers=headers)
                if request.method!="GET":calls.append({"method":request.method,"path":path,"status":response.status_code,"data":request.post_data_json if "application/json" in request.headers.get("content-type","") else None})
                if response.status_code>=400:api_errors.append({"path":path,"status":response.status_code,"body":response.text})
                route.fulfill(status=response.status_code,headers={key:value for key,value in response.headers.items() if key.lower() not in {"content-length","content-encoding","transfer-encoding"}},body=response.content)
            page.route("**/api/workspace/**",route)
            page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace",wait_until="networkidle")
            page.locator("#conversationSupplier").select_option(sid)
            expect(page.locator("#promptInput")).to_have_value("甲供应商默认要求")
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_1'")
            page.locator("#importSourceScope").fill("monthly");page.locator("#importSourceScope").press("Tab")
            expect(page.locator("#promptInput")).to_have_value("甲供应商月度要求")
            page.locator("#filePicker").set_input_files({"name":"a.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":workbook()})
            page.locator("#sendButton").click();expect(page.locator("#waitingPanel")).to_be_visible()
            waiting=page.evaluate("TokenSQLReview.currentJob")
            assert waiting["conversation_id"]=="ses_v24_1" and waiting["supplier_id"]==sid and waiting["source_scope"]=="monthly",waiting
            assert store.catalog_snapshot()["quotes"]==[]
            page.reload(wait_until="networkidle")
            expect(page.locator("#waitingPanel")).to_be_visible()
            expect(page.locator("#importSourceScope")).to_have_value("monthly")
            assert page.evaluate("TokenSQLReview.currentJob.id")==waiting["id"]
            checks.append("refresh restores waiting task, supplier, current series and original file")
            page.locator("#conversationSupplier").select_option(bid)
            expect(page.locator("#promptInput")).to_have_value("乙供应商默认要求")
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_2'")
            page.locator("#filePicker").set_input_files({"name":"b-unsent.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":workbook()})
            expect(page.locator("#attachmentSummary")).to_contain_text("1")
            page.locator("#conversationSupplier").select_option(sid)
            expect(page.locator("#waitingPanel")).to_be_visible()
            expect(page.locator("#promptInput")).to_have_value("")
            expect(page.locator("#attachmentSummary")).to_contain_text("0")
            resumed=page.evaluate("TokenSQLReview.currentJob")
            assert resumed["id"]==waiting["id"] and resumed["conversation_id"]==waiting["conversation_id"]
            checks.append("supplier switch restores A waiting job and keeps B unsent attachment out of A response")
            page.locator("#promptInput").fill("本期为人民币 / 百万 token")
            page.locator("#sendButton").click();expect(page.locator("#reviewDialog")).to_be_visible()
            ready=page.evaluate("TokenSQLReview.currentJob")
            assert ready["id"]==waiting["id"] and ready["attempt"]==2 and ready["files"][0]["id"]==waiting["files"][0]["id"],ready
            assert [call["session"] for call in sdk.calls]==["ses_v24_1","ses_v24_1"]
            page.locator("#reviewPersistProfile").uncheck()
            page.locator("#previewReviewButton").click()
            page.wait_for_function("revision => TokenSQLReview.currentJob.revision > revision",arg=ready["revision"])
            ready=page.evaluate("TokenSQLReview.currentJob")
            assert ready["persist_profile"] is False
            preference_patch=next(call for call in reversed(calls) if call["path"].endswith("/proposal"))
            assert preference_patch["data"]["persist_profile"] is False and "bundle" not in preference_patch["data"]
            checks.append("review persistence-only change is saved by revision without resubmitting candidate entries")
            assert not any(call["path"]=="/imports/start" and call["data"]["supplier_id"]==bid for call in calls if call["data"])
            page.locator("#hideReviewButton").click()
            page.locator("#newSessionButton").click()
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_3'")
            assert page.evaluate("TokenSQLReview.currentJob") is None
            expect(page.locator("#reviewDialog")).to_be_hidden()
            expect(page.locator("#promptInput")).to_have_value("甲供应商月度要求")
            expect(page.locator("#supplierLatestButton")).to_be_visible()
            page.locator("#supplierLatestButton").click()
            expect(page.locator("#reviewDialog")).to_be_visible()
            assert page.evaluate("TokenSQL.state.conversationId")=="ses_v24_3"
            assert page.evaluate("TokenSQLReview.currentJob.id")==ready["id"]
            page.locator("#hideReviewButton").click()
            page.locator("#continueSupplierSessionButton").click()
            page.wait_for_function("TokenSQLReview.currentJob === null && !TokenSQL.state.sessionRestoring")
            checks.append("new round before confirmation stays in the fresh default session; prior ready result opens only on request")
            page.locator("#conversationSupplier").select_option(bid)
            expect(page.locator("#attachmentSummary")).to_contain_text("1")
            expect(page.locator("#promptInput")).to_have_value("乙供应商默认要求")
            page.locator("#sendButton").click()
            page.wait_for_function("sid => TokenSQLReview.currentJob?.supplier_id === sid && !TokenSQL.state.submitting",arg=bid)
            beta=page.evaluate("TokenSQLReview.currentJob")
            assert beta["supplier_id"]==bid and beta["conversation_id"]=="ses_v24_2"
            page.locator("#conversationSupplier").select_option(sid)
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_3' && !TokenSQL.state.sessionRestoring")
            expect(page.locator("#reviewDialog")).to_be_hidden()
            expect(page.locator("#importSourceScope")).to_have_value("monthly")
            client.portal.call(manager.wait,beta["id"])
            assert page.evaluate("TokenSQLReview.currentJob") is None
            page.locator("#supplierLatestButton").click()
            expect(page.locator("#reviewDialog")).to_be_visible()
            assert page.evaluate("TokenSQLReview.currentJob.id")==ready["id"]
            page.locator("#confirmReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            page.locator("#conversationSupplier").select_option(bid)
            expect(page.locator("#reviewDialog")).to_be_visible()
            assert page.evaluate("TokenSQLReview.currentJob.id")==beta["id"]
            page.locator("#cancelReviewButton").click();expect(page.locator("#reviewDialog")).to_be_hidden()
            page.locator("#conversationSupplier").select_option(sid)
            page.wait_for_function("sid => TokenSQL.state.supplierId === sid && !TokenSQL.state.sessionRestoring",arg=sid)
            assert {row["supplier_id"] for row in store.catalog_snapshot()["quotes"]}=={sid}
            checks.append("switching while B runs detaches B polling and restores fresh A session; B completes in its own session")
            checks.append("same-job answer retains session/files; B has its own session/files and cancellation leaves only confirmed A")
            page.locator("#newSessionButton").click()
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_4'")
            expect(page.locator("#promptInput")).to_have_value("甲供应商月度要求")
            page.reload(wait_until="networkidle")
            page.wait_for_function("TokenSQL.state.conversationId === 'ses_v24_4'")
            expect(page.locator("#conversationSupplier")).to_have_value(sid)
            expect(page.locator("#importSourceScope")).to_have_value("monthly")
            expect(page.locator("#promptInput")).to_have_value("甲供应商月度要求")
            assert len(sdk.sessions)==4
            checks.append("new round reuses current supplier profile; refresh restores bound session and quotation series")
            if evidence_dir:page.screenshot(path=str(evidence_dir/"supplier-routed-session.jpg"),type="jpeg",quality=65)
            page.set_viewport_size({"width":390,"height":844});page.locator("#showChatButton").click()
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            if evidence_dir:page.screenshot(path=str(evidence_dir/"supplier-routed-mobile.jpg"),type="jpeg",quality=65)
            assert not errors and not api_errors,(errors,api_errors)
            browser.close();client.portal.call(manager.shutdown)
        server.shutdown();store.close()
    return {"type":"real_api_store_workflow_browser_scripted_sdk","database":"isolated TemporaryDirectory removed on completion","checks":checks,"sdk_calls":len(sdk.calls),"sessions":len(sdk.sessions),"page_errors":errors,"api_errors":api_errors}
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir:args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir:(args.evidence_dir/"sessions-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
