#!/usr/bin/env python3
"""Static v2.4 table comparison UI acceptance from actual synthetic XLSX snapshots."""
from __future__ import annotations
import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from playwright.sync_api import expect, sync_playwright
from tokensql.workspace.table_diff import compare_snapshots, read_workbooks
from workspace_browser_smoke import Fixture, StaticHandler, STATIC

def workbook(path,new=False):
    book=Workbook();sheet=book.active;sheet.title="报价"
    rows=[["模型","输入价","输出价"],["Model-A",10,20],["Model-B",12,24],["Model-C",14,28],["Model-D",16,32]]
    if new:rows=[rows[0],rows[3],["Model-A",11,20],["Model-E",18,36],rows[4]]
    for row in rows:sheet.append(row)
    sheet["B3" if new else "B2"].comment=Comment("本期确认价格" if new else "上期确认价格","Fixture")
    if new:sheet["A5"].font=Font(color="315BDB",bold=True);sheet.row_dimensions[5].height=24
    rules=book.create_sheet("计价说明");rules.append(["币种","单位"]);rules.append(["CNY","百万 token"])
    book.save(path)

def run(evidence_dir=None):
    errors=[];checks=[]
    with TemporaryDirectory(prefix="tokensql-v24-diff-browser-") as tmp:
        tmp=Path(tmp);old=tmp/"old.xlsx";new=tmp/"new.xlsx";workbook(old);workbook(new,True)
        comparison=compare_snapshots(read_workbooks([{"path":old,"filename":"上期报价.xlsx"}]),read_workbooks([{"path":new,"filename":"本期报价.xlsx"}]))
        counts={key:len(value) for key,value in comparison["groups"].items()}
        assert all(counts[kind] for kind in ("added","modified","deleted","moved","presentation")),counts
        fixture=Fixture()
        server=ThreadingHTTPServer(("127.0.0.1",0),partial(StaticHandler,directory=str(STATIC)));Thread(target=server.serve_forever,daemon=True).start()
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={"width":1440,"height":1000});page.set_default_timeout(12000)
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.route("**/api/workspace/**",fixture.route)
            page.goto("http://127.0.0.1:"+str(server.server_port)+"/workspace",wait_until="networkidle")
            payload={"comparison":comparison,"metrics":{"read_compare_seconds":0.017,"model_seconds":0.406,"total_seconds":0.423,"semantic_requests":1,"submitted_message_chars":1200,"reused_entries":3,"reinterpreted_entries":2,"clarification_count":0,"changed_context_chars":500},"contextUsage":{"task_llm_steps":2,"task_tokens":{"input":120,"output":35,"cache":{"read":40,"write":0}}}}
            page.evaluate("""payload => {
                const target=document.createElement("div");target.id="sourceComparisonFixture";
                document.querySelector("#reviewSections").replaceChildren(target);
                document.querySelector("#reviewMeta").textContent="合成原表对比 · 界面验收";
                TokenSQLSourceComparison.render(target,payload);
                document.querySelector("#reviewDialog").showModal();
            }""",payload)
            expect(page.locator(".source-table-pane")).to_have_count(2)
            expect(page.locator(".source-table-panes")).to_contain_text("Model-A")
            expect(page.locator(".source-table-panes")).to_contain_text("Model-E")
            expect(page.locator(".source-table-panes")).to_contain_text("Model-B")
            expect(page.locator("#sourceComparisonFixture")).to_contain_text("SDK 请求")
            expect(page.locator("#sourceComparisonFixture")).to_contain_text("模型步骤")
            expect(page.locator("#sourceComparisonFixture")).to_contain_text("缓存读取 token")
            assert page.locator(".source-cell-modified").count()>0
            assert page.locator(".source-cell-added").count()>0
            assert page.locator(".source-cell-deleted").count()>0
            page.get_by_text("处理详情",exact=True).click()
            expect(page.get_by_text("本期 SDK 输入 token",exact=True)).to_be_visible()
            page.get_by_text("处理详情",exact=True).click()
            assert page.evaluate("""() => {
                const cell={type:'number',value:'0.15',number_format:'0.00%',format_kind:'percent'};
                const base={version:1,files:[{file_key:'f',filename:'percent.xlsx',sheets:[{sheet_key:'s',name:'Rates',rows:[{row:1,cells:[{column:1,address:'A1',...cell}]}]}]}]};
                const host=document.createElement('div');TokenSQLSourceComparison.render(host,{comparison:{old_snapshot:base,new_snapshot:base,groups:{},row_matches:[{old:{sheet_key:'s',row:1},new:{sheet_key:'s',row:1}}]}});
                return host.querySelector('.source-cell-button').textContent==='15.00%';
            }""")
            checks.append("actual XLSX diff renders changed/new/deleted/moved/presentation groups and adjacent context side by side")
            page.locator(".source-cell-modified button").first.click()
            expect(page.locator(".source-cell-detail")).to_have_attribute("open","")
            expect(page.locator(".source-cell-detail-body")).to_contain_text("原始值")
            expect(page.locator(".source-cell-detail-body")).to_contain_text("数字格式")
            page.locator(".source-change-filter").filter(has_text="展示格式").click()
            expect(page.locator(".source-table-panes")).to_contain_text("Model-D")
            page.locator(".source-change-filter").filter(has_text="全部变化").click()
            page.get_by_label("对比工作表").select_option(label="计价说明")
            page.get_by_label("查看整张工作表").check()
            expect(page.locator(".source-table-panes")).to_contain_text("百万 token")
            checks.append("type filters, sheet selection, full-sheet view and cell provenance/number format details work")
            page.get_by_label("对比工作表").select_option(index=0)
            if evidence_dir:page.screenshot(path=str(evidence_dir/"source-comparison-desktop.jpg"),type="jpeg",quality=65)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            rect=page.locator("#reviewDialog").bounding_box();assert rect["x"]>=0 and rect["x"]+rect["width"]<=391,rect
            expect(page.locator(".source-table-pane")).to_have_count(2)
            if evidence_dir:page.screenshot(path=str(evidence_dir/"source-comparison-mobile.jpg"),type="jpeg",quality=65)
            checks.append("measured-field labels distinguish SDK requests, LLM steps and token counts; narrow dialog keeps two comparison panes")
            page.evaluate("""() => TokenSQLReview.openReview({id:'official-display-fixture',revision:1,status:'ready',bundle:{},preview:{},
                official_catalog_changes:[{official_price_id:'official-input',changed_fields:['amount'],before:{model_id:'A',billing_item:'input',amount:'20',currency:'CNY',pricing_unit_id:'cny_mtoken'},after:{model_id:'A',billing_item:'input',amount:'30',currency:'CNY',pricing_unit_id:'cny_mtoken'}}]})""")
            expect(page.locator(".official-source-changes")).to_contain_text("20 CNY / 百万 token → 30 CNY / 百万 token")
            checks.append("official source proposal shows explicit current-to-source amount before and after")
            assert not errors,errors
            browser.close()
        server.shutdown()
    return {"type":"real_xlsx_snapshot_diff_static_browser","database":"none; all browser API traffic intercepted","checks":checks,"row_group_counts":counts,"metrics":"synthetic display fixture, not performance evidence","page_errors":errors}
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--evidence-dir",type=Path);args=parser.parse_args()
    if args.evidence_dir:args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result=run(args.evidence_dir)
    if args.evidence_dir:(args.evidence_dir/"diff-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
