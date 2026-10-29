#!/usr/bin/env python3
"""v2.4 profile browser acceptance against real API/store on an isolated synthetic DB."""
from __future__ import annotations
import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlparse
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from tokensql.workspace.data_routes import create_data_router
from tokensql.workspace.store import WorkspaceStore
from workspace_browser_smoke import Fixture, StaticHandler, STATIC

def run(evidence_dir=None):
    checks=[];errors=[];mutations=[]
    with TemporaryDirectory(prefix="tokensql-v24-profile-browser-") as tmp:
        store=WorkspaceStore(Path(tmp)/"synthetic.sqlite3")
        store.apply_bundle({"models":[{"model_id":"model-a"}]})
        a=store.confirm_supplier({"name":"甲供应商"});b=store.confirm_supplier({"name":"乙供应商"})
        app=FastAPI();app.include_router(create_data_router(store))
        fixture=Fixture()
        original_catalog=fixture.catalog
        def catalog():
            value=original_catalog();value["suppliers"]=[a,b];return value
        fixture.catalog=catalog
        server=ThreadingHTTPServer(("127.0.0.1",0),partial(StaticHandler,directory=str(STATIC)))
        Thread(target=server.serve_forever,daemon=True).start()
        with TestClient(app) as client,sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={"width":1440,"height":900});page.set_default_timeout(12000)
            page.on("pageerror",lambda error:errors.append(str(error)))
            def route(route):
                path=urlparse(route.request.url).path.removeprefix("/api/workspace")
                if path.endswith("/profile") or path.endswith("/profile/history"):
                    request=route.request
                    response=client.request(request.method,"/api/workspace"+path,content=request.post_data_buffer,headers={"content-type":"application/json"})
                    if request.method!="GET":
                        mutations.append({"path":path,"status":response.status_code,"payload":request.post_data_json})
                        if response.status_code>=400:print(response.text,flush=True)
                    route.fulfill(status=response.status_code,body=response.content,headers={"content-type":"application/json"});return
                fixture.route(route)
            page.route("**/api/workspace/**",route)
            page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace",wait_until="networkidle")
            page.locator("#memoryButton").click()
            expect(page.locator("#profileDialog")).to_be_visible()
            expect(page.locator("#profileMeta")).to_contain_text("版本 0")
            page.locator("#profilePrompt").fill("按原币原单位导入")
            page.locator("#addProfileMappingButton").click()
            page.get_by_label("映射 1 原始模型名称",exact=True).fill("渠道 A")
            page.get_by_label("映射 1 系统 model_id",exact=True).fill("model-a")
            page.get_by_label("映射 1 渠道 ID",exact=True).fill("channel-a")
            page.get_by_label("映射 1 别名",exact=True).fill("A,别名甲")
            page.locator("#addProfileRuleButton").click()
            page.get_by_label("规则 1 类别",exact=True).select_option("unit")
            page.get_by_label("规则 1 内容",exact=True).fill("价格为人民币 / 百万 token")
            page.locator("#saveProfileButton").click()
            expect(page.locator("#profileSaveState")).to_have_text("已保存", timeout=1000)
            expect(page.locator("#profileMeta")).to_contain_text("版本 1")
            saved=client.get("/api/workspace/suppliers/"+a["supplier_id"]+"/profile").json()
            assert saved["revision"]==1 and saved["model_mappings"][0]["aliases"]==["A","别名甲"],saved
            assert saved["rules"][0]["evidence"]["source"]=="manual"
            checks.append("real profile API saves supplier mappings, channel aliases, typed rules, prompt and evidence atomically")
            page.locator("#profileNewScope").fill("国内渠道")
            page.locator("#addProfileScopeButton").click()
            page.locator("#profilePrompt").fill("国内报价按工作表分项")
            page.locator("#addProfileRuleButton").click()
            page.get_by_label("规则 1 内容",exact=True).fill("输入和输出分项报价")
            page.locator("#saveProfileButton").click()
            expect(page.locator("#profileMeta")).to_contain_text("版本 2")
            page.locator("#profileScope").select_option("")
            expect(page.locator("#profilePrompt")).to_have_value("按原币原单位导入")
            page.locator("#profileSupplier").select_option(b["supplier_id"])
            expect(page.locator("#profileMeta")).to_contain_text("乙供应商")
            expect(page.locator("#profilePrompt")).to_have_value("")
            expect(page.locator("#profileMappings .profile-entry")).to_have_count(0)
            page.locator("#profileSupplier").select_option(a["supplier_id"])
            expect(page.locator("#profileMeta")).to_contain_text("甲供应商")
            page.locator("#profileScope").select_option("国内渠道")
            expect(page.locator("#profilePrompt")).to_have_value("国内报价按工作表分项")
            checks.append("series prompt and rules remain separate from supplier defaults and other suppliers")
            if evidence_dir:page.screenshot(path=str(evidence_dir/"supplier-profile-desktop.jpg"),type="jpeg",quality=65)
            page.locator("#profileScope").select_option("")
            page.locator("#profileMappings .profile-entry").get_by_role("button",name="删除",exact=True).click()
            page.locator("#profileRules .profile-entry").get_by_role("button",name="删除",exact=True).click()
            page.locator("#saveProfileButton").click()
            expect(page.locator("#profileMeta")).to_contain_text("版本 3")
            saved=client.get("/api/workspace/suppliers/"+a["supplier_id"]+"/profile").json()
            assert not saved["model_mappings"] and not saved["rules"] and saved["sources"]["国内渠道"]["rules"]
            checks.append("deleting a mapping and rule preserves the other quotation series")
            page.reload(wait_until="networkidle")
            page.locator("#memoryButton").click()
            expect(page.locator("#profileMeta")).to_contain_text("版本 3")
            page.locator("#profileScope").select_option("国内渠道")
            expect(page.get_by_label("规则 1 内容",exact=True)).to_have_value("输入和输出分项报价")
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            rect=page.locator("#profileDialog").bounding_box();assert rect["x"]>=0 and rect["x"]+rect["width"]<=391,rect
            if evidence_dir:page.screenshot(path=str(evidence_dir/"supplier-profile-mobile.jpg"),type="jpeg",quality=65)
            checks.append("browser refresh restores persisted profiles and mobile dialog stays inside viewport")
            page.set_viewport_size({"width":1440,"height":900})
            page.locator("#openGlobalPreferencesButton").click()
            page.locator("#addGlobalPreferenceButton").click()
            page.get_by_label("应用偏好 1",exact=True).fill("默认使用中文说明")
            page.locator("#saveGlobalPreferencesButton").click()
            expect(page.locator("#globalPreferencesDialog")).to_be_hidden()
            assert any(rule["scope"]=="workspace" and rule["text"]=="默认使用中文说明" for rule in fixture.memory["rules"])
            assert any(rule["scope"]=="supplier:Smoke Supplier" for rule in fixture.memory["rules"])
            checks.append("application preferences preserve unrelated legacy scoped evidence")
            current=client.get("/api/workspace/suppliers/"+a["supplier_id"]+"/profile").json()
            response=client.put("/api/workspace/suppliers/"+a["supplier_id"]+"/profile",json={"expected_revision":current["revision"],"default_prompt":"另一个窗口的要求","model_mappings":current["model_mappings"],"rules":current["rules"],"sources":current["sources"]})
            assert response.status_code==200,response.text
            page.locator("#profilePrompt").fill("当前窗口的修订")
            page.locator("#saveProfileButton").click()
            expect(page.locator("#reloadProfileButton")).to_be_visible()
            expect(page.locator("#profilePrompt")).to_have_value("当前窗口的修订")
            page.locator("#reloadProfileButton").click()
            expect(page.locator("#profileMeta")).to_contain_text("版本 4")
            page.locator("#profileScope").select_option("")
            expect(page.locator("#profilePrompt")).to_have_value("另一个窗口的要求")
            checks.append("stale revision preserves draft until explicit latest-profile reload")
            assert not errors,errors
            assert store.catalog_snapshot()["quotes"]==[]
            browser.close()
        server.shutdown();store.close()
    return {"type":"real_profile_api_sqlite_browser","database":"isolated TemporaryDirectory removed on completion","checks":checks,"profile_mutations":len(mutations),"page_errors":errors}
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir:args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir:(args.evidence_dir/"profile-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
