#!/usr/bin/env python3
"""Synthetic browser + real API/store/workflow, deterministic mock OpenCode SDK. No Qwen or production."""
from __future__ import annotations
import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import hashlib
import httpx
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright

PROJECT = Path(__file__).resolve().parents[1]
FIRST = "计价尺度缺失：请确认每千 token 还是每百万 token？"
SECOND = "仍需确认：Tokens 应按每百万 token 吗？"


def mock_app():
    from tokensql.workspace.app import create_app
    from tokensql.workspace.agent import AgentConfig, OpenCodeAgentManager
    from tokensql.workspace.config import WorkspaceSettings
    from tokensql.workspace.memory import atomic_json
    from tokensql.workspace.store import WorkspaceStore
    settings = WorkspaceSettings()
    assert settings.test_mode and str(settings.workspace_dir).startswith("/tmp/tokensql-waiting-real-")
    cfg = AgentConfig(workspace_dir=settings.workspace_dir, database_path=settings.database_path,
                      agent_dir=settings.agent_dir, input_dir=settings.input_dir,
                      progress_dir=settings.progress_dir, data_dir=settings.data_dir,
                      python_bin=Path(sys.executable))
    cfg.agent_dir.mkdir(parents=True, exist_ok=True)
    sdk_path = cfg.agent_dir / "mock-sdk.json"
    sdk = json.loads(sdk_path.read_text()) if sdk_path.exists() else {"sessions":{}, "calls":[]}
    async def handler(request):
        path, method = request.url.path, request.method
        if path == "/global/health": return httpx.Response(200,json={"healthy":True})
        if path == "/session" and method == "POST":
            sid = "ses_mock_wait_" + str(len(sdk["sessions"])+1)
            sdk["sessions"][sid] = []
            atomic_json(sdk_path,sdk)
            return httpx.Response(200,json={"id":sid})
        if path == "/session":
            return httpx.Response(200,json=[{"id":sid,"title":sid,"directory":str(cfg.agent_dir)}
                                           for sid in sdk["sessions"]])
        sid = path.split("/")[2]
        if path.endswith("/abort"): return httpx.Response(200,json=True)
        if method == "GET": return httpx.Response(200,json=sdk["sessions"].get(sid,[]))
        body = json.loads(request.content)
        text = body["parts"][0]["text"]
        directory = Path(body["system"].split("Use only this run directory for writable output: ",1)[1].split("\n",1)[0].strip())
        assert directory.is_relative_to(cfg.agent_dir / "runs")
        attempt = int(re.search(r"Current attempt: (\d+)",text).group(1))
        sdk["calls"].append({"session":sid,"attempt":attempt,"directory":str(directory),"text":text})
        await asyncio.sleep(0.35)
        if attempt == 1 or attempt == 2 and "仍不确定" in text:
            question = FIRST if attempt == 1 else SECOND
            answer = "需要您补充后再解析：" + question
            atomic_json(directory / "needs_input.json",{"questions":[question],"summary":"合成文件缺少 token 尺度，不猜测。"})
        else:
            answer = "已根据您的补充生成草案，请审核确认；没有直接入库。"
            atomic_json(directory / "proposal.json",{"bundle":{"create":[{
                "model_id":"qa-wait-model","discount_rate":"0.8","pricing_unit_id":"cny_mtoken",
                "source":"Synthetic.xlsx","source_ref":"Synthetic!A2"}]}})
        sdk["sessions"][sid].extend([
            {"info":{"role":"user"},"parts":[{"type":"text","text":text}]},
            {"info":{"role":"assistant"},"parts":[{"type":"text","text":answer}]}])
        atomic_json(sdk_path,sdk)
        return httpx.Response(200,json={"info":{"role":"assistant"},"parts":[{"type":"text","text":answer}]})
    manager = OpenCodeAgentManager(config=cfg,store=WorkspaceStore(cfg.database_path),
                                   transport=httpx.MockTransport(handler))
    return create_app(settings,store=manager.store,agent_manager=manager)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8897)
    parser.add_argument("--chromium",default="/home/user/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--evidence-dir",type=Path,default=Path("/tmp/tokensql-waiting-real-evidence"))
    parser.add_argument("--serve",action="store_true")
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        uvicorn.run(mock_app(),host="127.0.0.1",port=args.port,log_level="warning")
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1",args.port)) == 0: raise RuntimeError("QA port occupied; will not stop another process")
    root = Path(tempfile.mkdtemp(prefix="tokensql-waiting-real-"))
    args.evidence_dir.mkdir(parents=True,exist_ok=True)
    env = os.environ.copy()
    for key in ("workspace","agent","input","progress","data"):
        env["TOKENSQL_"+key.upper()+"_DIR"] = str(root / key)
    env.update(TOKENSQL_TEST_MODE="true",TOKENSQL_FX_AUTO_REFRESH="false",PYTHONDONTWRITEBYTECODE="1")
    log = (root / "server.log").open("w")
    process = None
    client = httpx.Client(base_url="http://127.0.0.1:"+str(args.port),timeout=15)
    def request(path,body=None,method=None):
        response = client.request(method or ("POST" if body is not None else "GET"),"/api/workspace/"+path,json=body)
        response.raise_for_status()
        return response.json()
    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--serve","--port",str(args.port)],
                                   cwd=PROJECT,env=env,stdout=log,stderr=subprocess.STDOUT)
        for _ in range(100):
            if process.poll() is not None: raise RuntimeError((root/"server.log").read_text())
            try:
                if client.get("/health").json().get("isolated_test"): return
            except Exception: pass
            time.sleep(.1)
        raise RuntimeError("Isolated service did not start")
    def stop():
        if process:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
    def sdk(): return json.loads((root/"agent"/"mock-sdk.json").read_text())
    def active():
        return next(item for item in request("imports")["items"] if item["status"] == "waiting_for_input")
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Synthetic"
    sheet.append(["model_id","discount","unit"]); sheet.append(["qa-wait-model","八折","Tokens"])
    buffer = BytesIO(); workbook.save(buffer); original = buffer.getvalue()
    errors, uploads, starts, responses = [], [], [], []
    try:
        start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True,executable_path=args.chromium)
            page = browser.new_page(viewport={"width":1500,"height":960})
            page.on("pageerror",lambda error:errors.append(str(error)))
            def record(req):
                if req.method == "POST":
                    if req.url.endswith("/imports/upload"): uploads.append(req.url)
                    if req.url.endswith("/imports/start"): starts.append(req.post_data_json)
                    if req.url.endswith("/respond"): responses.append(req.post_data_json)
            page.on("request",record)
            page.goto(str(client.base_url)+"/workspace",wait_until="networkidle")
            page.locator("#filePicker").set_input_files({"name":"Synthetic.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":original})
            page.locator("#promptInput").fill("合成验收：解析前先确认缺失的计价尺度，再生成草案。")
            page.locator("#sendButton").click()
            expect(page.locator("#waitingPanel")).to_be_visible(timeout=15000)
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            expect(page.locator("#conversation")).to_contain_text("需要您补充后再解析："+FIRST)
            expect(page.locator("#conversation")).not_to_contain_text("任务未完成")
            expect(page.locator("#sendButton")).to_be_enabled()
            expect(page.locator("#attachButton")).to_be_disabled()
            expect(page.locator("#reviewOpenButton")).not_to_be_visible()
            assert request("quotes")["total"] == 0 and request("catalog")["models"] == []
            first = active(); identifier,sid = first["id"],first["conversation_id"]
            assert first["attempt"] == 1 and first["files"][0]["status"] == "progress"
            assert len(sdk()["calls"]) == 1 and len(uploads) == len(starts) == 1
            sha = hashlib.sha256(original).hexdigest()
            assert hashlib.sha256(next((root/"progress").rglob("*.xlsx")).read_bytes()).hexdigest() == sha
            page.route("**/agent/sessions/*/messages",lambda route:route.fulfill(status=503,json={"detail":"synthetic history unavailable"}))
            page.reload(wait_until="networkidle")
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            expect(page.locator("#conversation")).to_contain_text("历史暂不可读取")
            assert page.locator("#conversation .message-body > p").all_text_contents().count("需要您补充后再解析："+FIRST) == 1
            page.unroute("**/agent/sessions/*/messages")
            page.reload(wait_until="networkidle")
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            assert page.locator("#conversation .message-body > p").all_text_contents().count("需要您补充后再解析："+FIRST) == 1
            page.locator("#newSessionButton").click()
            expect(page.locator("#waitingPanel")).not_to_be_visible()
            restoring = []
            page.route("**/agent/sessions/*/messages",lambda route:restoring.append(route))
            page.locator("#sessionSelect").select_option(sid)
            expect(page.locator("#sendButton")).to_be_disabled()
            page.evaluate("window.TokenSQL.addFiles([new File(['x'],'restoring-extra.xlsx')])")
            expect(page.locator("#fileChips")).to_be_empty()
            expect(page.locator("#toastRegion")).to_contain_text("正在恢复会话任务")
            import_count = len(request("imports")["items"])
            lookup = page.evaluate("""async () => {
              const original = window.confirm; let confirmations = 0;
              window.confirm = () => { confirmations += 1; return false; };
              try {
                await window.TokenSQLReview.lookupOfficial({model_id:"qa-restore-missing-official"});
                return {confirmations,restoring:window.TokenSQL.state.sessionRestoring};
              } finally { window.confirm = original; }
            }""")
            assert lookup == {"confirmations":0,"restoring":True}
            page.locator("#catalogButton").click()
            expect(page.locator("#catalogDialog")).to_be_visible()
            page.locator("#newOfficialButton").click()
            expect(page.locator("#reviewDialog")).not_to_be_visible()
            expect(page.locator("#toastRegion")).to_contain_text("稍候再创建手工草案")
            assert len(request("imports")["items"]) == import_count and len(sdk()["calls"]) == 1
            page.locator("#closeCatalogButton").click()
            assert len(restoring) == 1
            page.locator("#newSessionButton").click()
            expect(page.locator("#sendButton")).to_be_enabled()
            assert page.evaluate("window.TokenSQL.state.sessionRestoring") is False
            restoring[0].fulfill(json={"messages":[{"role":"assistant","text":"OBSOLETE_WAITING_SESSION"}]})
            page.unroute("**/agent/sessions/*/messages")
            page.wait_for_timeout(100)
            expect(page.locator("#waitingPanel")).not_to_be_visible()
            expect(page.locator("#conversation")).not_to_contain_text("OBSOLETE_WAITING_SESSION")
            page.locator("#sessionSelect").select_option(sid)
            expect(page.locator("#waitingPanel")).to_be_visible()
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            expect(page.locator("#attachButton")).to_be_disabled()
            page.evaluate("window.TokenSQL.addFiles([new File(['x'],'extra.xlsx')])")
            expect(page.locator("#fileChips")).to_be_empty()
            expect(page.locator("#toastRegion")).to_contain_text("不能添加新附件")
            # A second tab answers first; this page must recover a 409 without duplicate inference.
            request("imports/"+identifier+"/respond",{"expected_revision":first["revision"],"prompt":"仍不确定"})
            page.locator("#promptInput").fill("每百万 token")
            page.locator("#sendButton").click()
            expect(page.locator("#waitingQuestions")).to_contain_text(SECOND,timeout=15000)
            expect(page.locator("#toastRegion")).to_contain_text("版本或状态已变化")
            second = active()
            assert second["id"] == identifier and second["conversation_id"] == sid and second["attempt"] == 2
            assert len(sdk()["calls"]) == 2 and request("quotes")["total"] == 0
            page.locator("#promptInput").fill("已确认每百万 token，人民币，八折。")
            page.evaluate("document.getElementById('sendButton').click();document.getElementById('sendButton').click()")
            expect(page.locator("#reviewDialog")).to_be_visible(timeout=15000)
            ready = request("imports/"+identifier)
            assert ready["status"] == "ready" and ready["attempt"] == 3 and len(ready["clarifications"]) == 2
            assert ready["conversation_id"] == sid and len(sdk()["calls"]) == 3
            assert all(call["session"] == sid for call in sdk()["calls"])
            assert sdk()["calls"][-1]["directory"].endswith("/attempts/3")
            assert len(uploads) == len(starts) == 1 and request("quotes")["total"] == 0
            page.locator("#confirmReviewButton").click()
            expect(page.locator("#reviewDialog")).not_to_be_visible()
            saved = request("quotes")["items"]
            assert len(saved) == 1 and saved[0]["discount_rate"] == "0.80"
            assert hashlib.sha256(next((root/"data").rglob("*.xlsx")).read_bytes()).hexdigest() == sha
            # Waiting state, files and questions survive a complete service restart; no inference is restarted.
            page.locator("#newSessionButton").click()
            page.locator("#filePicker").set_input_files({"name":"Synthetic-cancel.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":original})
            page.locator("#promptInput").fill("合成取消与重启验收：先询问。")
            page.locator("#sendButton").click()
            expect(page.locator("#waitingPanel")).to_be_visible(timeout=15000)
            cancel_job = active(); before = request("quotes")["items"]; memory = request("agent/memory")
            calls_before = len(sdk()["calls"])
            stop(); start()
            page.reload(wait_until="networkidle")
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            assert active()["id"] == cancel_job["id"] and len(sdk()["calls"]) == calls_before
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(args.evidence_dir/"waiting-mobile.png"),full_page=True)
            page.set_viewport_size({"width":1500,"height":960})
            page.screenshot(path=str(args.evidence_dir/"waiting-desktop.png"),full_page=True)
            cancel_url = "**/imports/" + cancel_job["id"] + "/cancel"
            page.route(cancel_url,lambda route:route.fulfill(status=503,json={"detail":"synthetic unavailable"}))
            page.locator("#abortButton").click()
            expect(page.locator("#toastRegion")).to_contain_text("取消请求未确认完成")
            expect(page.locator("#waitingQuestions")).to_contain_text(FIRST)
            expect(page.locator("#sendButton")).to_be_enabled()
            page.unroute(cancel_url)
            assert request("imports/"+cancel_job["id"])["status"] == "waiting_for_input"
            page.locator("#abortButton").click()
            expect(page.locator("#waitingPanel")).not_to_be_visible()
            expect(page.locator("#attachButton")).to_be_enabled()
            assert request("imports/"+cancel_job["id"])["status"] == "cancelled"
            assert request("quotes")["items"] == before and request("agent/memory") == memory
            assert len(list((root/"input").rglob("*.xlsx"))) == 1 and not list((root/"progress").rglob("*.xlsx"))
            assert hashlib.sha256(next((root/"input").iterdir()).read_bytes()).hexdigest() == sha
            # The convenience action is a real human clarification, never a direct confirmation.
            page.locator("#newSessionButton").click()
            page.locator("#promptInput").fill("合成可空字段继续验收：先询问。")
            page.locator("#sendButton").click()
            expect(page.locator("#waitingPanel")).to_be_visible(timeout=15000)
            page.locator("#continueUnknownButton").click()
            expect(page.locator("#reviewDialog")).to_be_visible(timeout=15000)
            assert "非关键项请留空继续" in sdk()["calls"][-1]["text"]
            assert request("quotes")["items"] == before
            page.locator("#cancelReviewButton").click()
            expect(page.locator("#reviewDialog")).not_to_be_visible()
            # Cancel while a pre-respond history read is held: no late POST or stale UI overwrite.
            page.locator("#newSessionButton").click()
            page.locator("#promptInput").fill("合成取消前置历史请求验收。")
            page.locator("#sendButton").click()
            expect(page.locator("#waitingPanel")).to_be_visible(timeout=15000)
            held = []
            page.route("**/agent/sessions/*/messages",lambda route: held.append(route))
            count_before = len(sdk()["calls"]); responses_before = len(responses)
            page.locator("#promptInput").fill("这个回答在真正发送前被我取消。")
            page.locator("#sendButton").click()
            expect(page.locator("#sendButton")).to_be_disabled()
            page.wait_for_timeout(100)
            assert len(held) == 1
            page.locator("#abortButton").click()
            expect(page.locator("#jobStrip")).not_to_be_visible()
            held[0].fulfill(json={"messages":[]})
            page.unroute("**/agent/sessions/*/messages")
            page.wait_for_timeout(200)
            assert len(sdk()["calls"]) == count_before and len(responses) == responses_before
            expect(page.locator("#waitingPanel")).not_to_be_visible()
            expect(page.locator("#sendButton")).to_be_enabled()
            assert request("quotes")["items"] == before and request("agent/memory") == memory
            browser.close()
        assert not errors,errors
        result = {"passed":True,"kind":"synthetic_real_api_mock_agent_browser","root":str(root),
                  "production_accessed":False,"real_qwen_called":False,"multi_round_same_job_session_file":True,
                  "waiting_no_commit_or_busy_lock":True,"refresh_and_session_restore":True,
                  "sdk_history_outage_does_not_hide_waiting_questions":True,"session_restoration_blocks_attachments_and_send":True,
                  "restore_blocks_official_lookup_and_manual_draft":True,"new_session_safely_ends_obsolete_restore":True,
                  "service_restart_preserves_waiting_without_inference":True,"conflict_no_duplicate_inference":True,
                  "double_click_single_inference":True,"attachments_rejected_while_waiting":True,
                  "review_confirm_only_commit":True,"cancel_returns_original_hash_no_data_memory_writes":True,
                  "unknown_shortcut_is_explicit_human_answer":True,"cancelled_history_cannot_submit_or_overwrite":True,
                  "cancel_failure_recovers_latest_state":True,
                  "sdk_calls":len(sdk()["calls"]),"browser_respond_requests":len(responses)}
        (args.evidence_dir/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps(result,ensure_ascii=False))
    finally:
        stop();client.close();log.close()


if __name__ == "__main__": main()
