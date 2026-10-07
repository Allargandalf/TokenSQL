"""Streamlit management UI for TokenSQL.

Importing this module does not import Streamlit, render widgets, or contact the
API. Run it through Streamlit or call main().
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

from tokensql.ui.client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    APIError,
    RequestValidationError,
    TokenSQLClient,
    TokenSQLClientError,
)


def main() -> None:
    """Render the management UI and perform API calls only from user actions."""
    import streamlit as st

    st.set_page_config(page_title="TokenSQL 管理台", page_icon="🧾", layout="wide")
    st.title("TokenSQL 报价管理")
    st.caption("Excel 预览、人工审核、模型别名与可追溯价格查询")

    with st.sidebar:
        st.header("连接")
        base_url = st.text_input(
            "TokenSQL API",
            value=os.getenv("TOKENSQL_API_URL", DEFAULT_BASE_URL),
            help="默认连接本机 FastAPI，不会把文件或密钥写入日志。",
        )
        timeout = st.number_input(
            "超时（秒）",
            min_value=1.0,
            max_value=120.0,
            value=float(DEFAULT_TIMEOUT_SECONDS),
            step=1.0,
        )

    try:
        client = TokenSQLClient(base_url, timeout=timeout)
    except RequestValidationError as exc:
        st.error(str(exc))
        return

    try:
        try:
            health = client.health()
        except TokenSQLClientError as exc:
            st.error(f"API 不可用：{exc}")
            st.info("请确认 FastAPI 已启动，并检查侧栏中的 API 地址。")
            return

        _render_health(st, health)
        import_tab, review_tab, alias_tab, price_tab = st.tabs(
            ["Excel 导入", "待审核项", "模型别名", "价格与导出"]
        )
        with import_tab:
            _render_import(st, client)
        with review_tab:
            _render_reviews(st, client)
        with alias_tab:
            _render_aliases(st, client)
        with price_tab:
            _render_prices(st, client)
    finally:
        client.close()


def _render_health(st: Any, health: Mapping[str, Any]) -> None:
    status = str(health.get("status", "unknown")).lower()
    if status in {"ok", "healthy", "ready"}:
        st.success("TokenSQL API 已连接")
    else:
        st.warning(f"API 状态：{status}")

    qwen = health.get("qwen", health.get("llm"))
    if qwen is None:
        st.info("健康检查未报告 Qwen 状态；歧义数据应进入人工审核。")
        return
    if isinstance(qwen, Mapping):
        qwen_status = str(qwen.get("status", "unknown")).lower()
        available = qwen.get("available", qwen_status in {"ok", "healthy", "ready"})
        reason = qwen.get("reason") or qwen.get("detail")
    else:
        available = bool(qwen)
        reason = None
    if not available:
        suffix = f"：{reason}" if reason else ""
        st.warning(f"Qwen 当前不可用{suffix}。仍可预览，歧义项必须人工审核。")


def _render_import(st: Any, client: TokenSQLClient) -> None:
    st.subheader("Excel 导入")
    st.caption("仅接受 .xlsx；预览不会写库，必须再次点击“确认导入”。")
    uploaded = st.file_uploader("选择报价文件", type=["xlsx"])
    context_text = st.text_area(
        "预览上下文（JSON）",
        value="{}",
        height=110,
        help=(
            "可选 profile 对象支持 header_row、discount_scale 和 "
            "price_headers。"
        ),
    )
    if uploaded is None:
        return

    content = uploaded.getvalue()
    fingerprint = hashlib.sha256(content).hexdigest()
    if st.button("预览解析", type="primary"):
        try:
            context = _parse_object(context_text, "预览上下文")
            preview = client.profile_import(uploaded.name, content, context=context)
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            st.session_state["_tokensql_preview"] = preview
            st.session_state["_tokensql_preview_fingerprint"] = fingerprint

    preview = st.session_state.get("_tokensql_preview")
    is_current = (
        preview is not None
        and st.session_state.get("_tokensql_preview_fingerprint") == fingerprint
    )
    if not is_current:
        st.info("请先预览当前文件，确认工作表、表头和待审核原因。")
        return

    st.markdown("#### 当前文件预览")
    _show_preview(st, preview)
    default_request = json.dumps(
        {
            "data_source_id": 1,
            "parser_version": "streamlit-v1",
            "provider_id": None,
            "vendor_context": {},
        },
        ensure_ascii=False,
        indent=2,
    )
    request_text = st.text_area(
        "导入请求（JSON）",
        value=default_request,
        height=180,
        help=(
            "data_source_id 与 parser_version 必填；可选 profile、sheet_names、"
            "provider_id、vendor_context、initiated_by。"
        ),
    )
    st.warning("确认导入会创建导入批次；不确定项将进入人工审核。")
    if st.button("确认导入", type="primary"):
        try:
            request = _parse_object(request_text, "导入请求")
            if not request.get("data_source_id"):
                raise RequestValidationError("data_source_id is required")
            if not request.get("parser_version"):
                raise RequestValidationError("parser_version is required")
            result = client.import_workbook(
                uploaded.name,
                content,
                request=request,
            )
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            st.success("导入请求已完成")
            st.json(result)


def _show_preview(st: Any, preview: Mapping[str, Any]) -> None:
    workbook = preview.get("workbook", preview)
    if isinstance(workbook, Mapping):
        name = workbook.get(
            "workbook_name", workbook.get("source_name", "未命名工作簿")
        )
        sheets = workbook.get("sheets", [])
        st.write(f"工作簿：{name}")
        if isinstance(sheets, list) and sheets:
            st.dataframe(sheets, use_container_width=True)
    staged = preview.get("staged_quotes", [])
    if isinstance(staged, list):
        st.write(f"候选报价：{len(staged)} 条")
        if staged:
            st.dataframe(staged, use_container_width=True)


def _render_reviews(st: Any, client: TokenSQLClient) -> None:
    st.subheader("待审核项")
    st.markdown("#### 候选报价确认")
    staged_id_text = st.text_input("候选报价 ID", key="confirm_staged_id")
    load_col, confirm_col = st.columns(2)
    if load_col.button("加载候选详情", key="load_staged"):
        try:
            staged_id = _required_int(staged_id_text, "候选报价 ID")
            st.session_state["_tokensql_staged_detail"] = (
                client.get_staged_quote(staged_id)
            )
        except TokenSQLClientError as exc:
            _display_error(st, exc)
    staged_detail = st.session_state.get("_tokensql_staged_detail")
    if staged_detail is not None:
        st.json(staged_detail)
    confirmation_text = st.text_area(
        "人工报价确认（JSON）",
        value="{}",
        height=260,
        key="staged_confirmation",
        help=(
            "需明确 actor、source/provider/model/offering/plan IDs、时间、币种、"
            "候选索引和计费组件；确认后仍须逐项审核批准。"
        ),
    )
    if confirm_col.button("提交人工确认", key="confirm_staged"):
        try:
            staged_id = _required_int(staged_id_text, "候选报价 ID")
            confirmation = _parse_object(confirmation_text, "人工报价确认")
            result = client.confirm_staged_quote(
                staged_id, confirmation=confirmation
            )
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            st.success("确认已保存；请继续批准全部审核项后再发布。")
            st.session_state["_tokensql_staged_detail"] = result
            st.json(result)

    left, right = st.columns([2, 1])
    reviewer = left.text_input("审核人", key="reviewer")
    status = right.selectbox("状态", ["open", "resolved", "dismissed"], index=0)
    if st.button("刷新审核列表"):
        try:
            st.session_state["_tokensql_reviews"] = client.list_reviews(status=status)
        except TokenSQLClientError as exc:
            _display_error(st, exc)

    reviews = st.session_state.get("_tokensql_reviews", [])
    if not reviews:
        st.info("暂无已加载的待审核项。")
        return

    for item in reviews:
        review_id = item.get("id")
        title = f"#{review_id} · {item.get('review_type', 'review')}"
        with st.expander(title):
            st.json(item)
            resolution_text = st.text_area(
                "审核结论（JSON）",
                value="{}",
                key=f"resolution_{review_id}",
            )
            create_alias_rule = st.checkbox(
                "批准后创建可复用别名规则",
                key=f"alias_rule_{review_id}",
            )
            approve_col, reject_col = st.columns(2)
            if approve_col.button("批准", key=f"approve_{review_id}", type="primary"):
                try:
                    resolution = _parse_object(resolution_text, "审核结论")
                    result = client.approve_review(
                        review_id,
                        reviewer=reviewer,
                        resolution=resolution,
                        create_alias_rule=create_alias_rule,
                    )
                except TokenSQLClientError as exc:
                    _display_error(st, exc)
                else:
                    st.success(f"审核项 #{review_id} 已批准")
                    st.json(result)
            if reject_col.button("拒绝", key=f"reject_{review_id}"):
                try:
                    resolution = _parse_object(resolution_text, "审核结论")
                    result = client.reject_review(
                        review_id,
                        reviewer=reviewer,
                        resolution=resolution,
                    )
                except TokenSQLClientError as exc:
                    _display_error(st, exc)
                else:
                    st.success(f"审核项 #{review_id} 已拒绝")
                    st.json(result)


def _render_aliases(st: Any, client: TokenSQLClient) -> None:
    st.subheader("模型别名")
    if st.button("加载模型与供应商"):
        try:
            st.session_state["_tokensql_models"] = client.list_models()
            st.session_state["_tokensql_providers"] = client.list_providers()
        except TokenSQLClientError as exc:
            _display_error(st, exc)

    models = st.session_state.get("_tokensql_models")
    providers = st.session_state.get("_tokensql_providers")
    if models is not None:
        st.caption("标准模型")
        st.dataframe(models, use_container_width=True)
    if providers is not None:
        st.caption("供应商")
        st.dataframe(providers, use_container_width=True)

    alias = st.text_input("别名")
    standard_model_id = st.number_input("标准模型 ID", min_value=1, step=1)
    provider_id = st.text_input("供应商 ID（可选）")
    model_version_id = st.text_input("模型版本 ID（可选）")
    source_note = st.text_input("来源说明（可选）")
    st.caption("直接创建的别名默认未经验证；请通过审核流程确认并生成可信规则。")
    if st.button("保存别名", type="primary"):
        body: dict[str, Any] = {
            "alias": alias,
            "standard_model_id": int(standard_model_id),
        }
        try:
            optional_provider = _optional_int(provider_id, "供应商 ID")
            optional_version = _optional_int(model_version_id, "模型版本 ID")
            if optional_provider is not None:
                body["provider_id"] = optional_provider
            if optional_version is not None:
                body["model_version_id"] = optional_version
            if source_note.strip():
                body["source_note"] = source_note.strip()
            result = client.create_alias(body)
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            st.success("别名已保存")
            st.json(result)


def _render_prices(st: Any, client: TokenSQLClient) -> None:
    st.subheader("报价历史")
    plan_text = st.text_input("报价方案 ID", key="history_plan")
    if st.button("查询历史"):
        try:
            plan_id = _required_int(plan_text, "报价方案 ID")
            rows = client.price_history(pricing_plan_id=plan_id)
            st.session_state["_tokensql_history"] = rows
            st.session_state["_tokensql_history_plan"] = plan_id
        except TokenSQLClientError as exc:
            _display_error(st, exc)

    history = st.session_state.get("_tokensql_history")
    if history is not None:
        st.dataframe(history, use_container_width=True)
        try:
            csv_data = client.export_prices_csv(
                pricing_plan_id=st.session_state["_tokensql_history_plan"]
            )
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            st.download_button(
                "下载 CSV",
                data=csv_data,
                file_name="tokensql-prices.csv",
                mime="text/csv",
            )

    with st.expander("验证与发布管理"):
        st.caption("所有操作都要求显式操作者、已验证确认和可追溯来源。")
        actor = st.text_input("操作者", key="management_actor")

        st.markdown("##### 发布审核后的候选报价")
        staged_id = st.text_input("候选报价 ID", key="publish_staged_id")
        publish_plan_id = st.text_input("报价方案 ID", key="publish_plan_id")
        publish_artifact_id = st.text_input(
            "来源工件 ID", key="publish_artifact_id"
        )
        publish_key = st.text_input("幂等键", key="publish_idempotency_key")
        official = st.checkbox("这是官方候选报价", key="publish_official")
        official_source_id = st.text_input(
            "官方数据源 ID", key="publish_official_source_id"
        )
        if st.button("验证并发布", type="primary", key="publish_quote"):
            try:
                arguments = {
                    "staged_quote_id": _required_int(staged_id, "候选报价 ID"),
                    "pricing_plan_id": _required_int(
                        publish_plan_id, "报价方案 ID"
                    ),
                    "source_artifact_id": _required_int(
                        publish_artifact_id, "来源工件 ID"
                    ),
                    "actor": actor,
                    "idempotency_key": publish_key,
                }
                if official:
                    result = client.publish_official_quote(
                        **arguments,
                        data_source_id=_required_int(
                            official_source_id, "官方数据源 ID"
                        ),
                    )
                else:
                    result = client.publish_reviewed_quote(**arguments)
            except TokenSQLClientError as exc:
                _display_error(st, exc)
            else:
                st.success("报价已发布或幂等复用")
                st.json(result)

        st.markdown("##### 创建已验证汇率")
        fx_base = st.text_input("基准币种", value="USD", key="fx_base")
        fx_quote = st.text_input("计价币种", value="CNY", key="fx_quote")
        fx_rate = st.text_input("汇率", key="fx_rate")
        fx_date = st.text_input("汇率日期（YYYY-MM-DD）", key="fx_date")
        fx_source = st.text_input("汇率数据源 ID", key="fx_source")
        if st.button("保存已验证汇率", key="save_fx"):
            try:
                result = client.create_fx_rate(
                    base_currency=fx_base,
                    quote_currency=fx_quote,
                    rate=fx_rate,
                    rate_date=fx_date,
                    data_source_id=_required_int(fx_source, "汇率数据源 ID"),
                    actor=actor,
                )
            except TokenSQLClientError as exc:
                _display_error(st, exc)
            else:
                st.success("汇率已保存或幂等复用")
                st.json(result)

        st.markdown("##### 持久化标准化结果")
        normalize_component = st.text_input(
            "价格组件 ID", key="normalize_component"
        )
        normalize_artifact = st.text_input(
            "价格来源工件 ID", key="normalize_artifact"
        )
        normalize_fx = st.text_input("汇率记录 ID（可选）", key="normalize_fx")
        normalize_algorithm = st.text_input(
            "算法版本",
            value="tokensql-normalization-v1",
            key="normalize_algorithm",
        )
        if st.button("保存标准化结果", key="save_normalization"):
            try:
                result = client.persist_normalization(
                    price_component_id=_required_int(
                        normalize_component, "价格组件 ID"
                    ),
                    source_artifact_id=_required_int(
                        normalize_artifact, "价格来源工件 ID"
                    ),
                    fx_rate_id=_optional_int(normalize_fx, "汇率记录 ID"),
                    algorithm_version=normalize_algorithm,
                    actor=actor,
                )
            except TokenSQLClientError as exc:
                _display_error(st, exc)
            else:
                st.success("标准化结果已保存或幂等复用")
                st.json(result)

    st.markdown("#### 价格比较")
    left_col, right_col = st.columns(2)
    left_id = left_col.text_input("左侧价格组件 ID")
    right_id = right_col.text_input("右侧价格组件 ID")
    left_normalization = left_col.text_input("左侧标准化记录 ID（可选）")
    right_normalization = right_col.text_input("右侧标准化记录 ID（可选）")
    algorithm_version = st.text_input("标准化算法版本（可选）")
    if st.button("比较价格"):
        try:
            result = client.compare_prices(
                left_component_id=_required_int(left_id, "左侧价格组件 ID"),
                right_component_id=_required_int(right_id, "右侧价格组件 ID"),
                left_normalization_id=_optional_int(
                    left_normalization, "左侧标准化记录 ID"
                ),
                right_normalization_id=_optional_int(
                    right_normalization, "右侧标准化记录 ID"
                ),
                algorithm_version=algorithm_version.strip() or None,
            )
        except TokenSQLClientError as exc:
            _display_error(st, exc)
        else:
            if result.get("comparable") is False:
                reasons = result.get("reasons") or ["计费维度或条件不同"]
                st.warning("不可直接比较：" + "；".join(map(str, reasons)))
            else:
                st.success("两个价格组件可直接比较")
            st.json(result)


def _parse_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestValidationError(f"{label}不是有效 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise RequestValidationError(f"{label}必须是 JSON 对象")
    return value


def _parse_optional_object(raw: str, label: str) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    return _parse_object(raw, label)


def _optional_int(raw: str, label: str) -> int | None:
    if not raw.strip():
        return None
    return _required_int(raw, label)


def _required_int(raw: str, label: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(f"{label}必须是整数") from exc
    if value < 1:
        raise RequestValidationError(f"{label}必须大于 0")
    return value


def _display_error(st: Any, exc: TokenSQLClientError) -> None:
    if isinstance(exc, APIError) and exc.status_code == 422:
        st.error(f"请求未通过校验：{exc}")
    else:
        st.error(str(exc))


if __name__ == "__main__":
    main()
