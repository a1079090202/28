"""门店带看与转化管理 —— Streamlit 页面层。

只做展示和输入收集：
- SQL 全部在 repository.py
- 业务规则在 services.py / state_machine.py
- 写操作一律走 services 层
"""
import csv
import io
from datetime import date, datetime

import pandas as pd
import streamlit as st

import db
import repository as repo
import seed
import services
from options import keyed_options
from db import LAYOUT_VALUES as LAYOUTS
from state_machine import (
    STATUS_DEAL,
    TERMINAL_STATUSES,
    InvalidTransitionError,
    next_statuses,
)

st.set_page_config(page_title="门店带看管理", page_icon="🏠", layout="wide")


@st.cache_resource
def init_schema():
    """进程级一次性建表（cache_resource 跨会话只执行一次且自带锁，避免并发首启竞态）。"""
    setup = db.connect(db.DEFAULT_DB_PATH)
    try:
        db.init_db(setup)
    finally:
        setup.close()
    return True


def get_conn():
    """每个浏览器会话一个独立连接，绝不跨用户共享。

    多人同时用时，共享连接会让 A 的多步事务被 B 的 commit 连带提交、被 B 的
    rollback 连带回滚（事务边界串台）。session_state 按会话隔离连接，
    写事务再由 services 层用 BEGIN IMMEDIATE 串行化，两者配合才安全。
    """
    if "db_conn" not in st.session_state:
        init_schema()  # 确保表已建好（幂等）
        st.session_state.db_conn = db.connect(db.DEFAULT_DB_PATH)
    return st.session_state.db_conn


conn = get_conn()
seed.ensure_seed(conn)  # 首次启动写入样例数据；并发首启时后来者安静跳过

st.sidebar.title("🏠 门店管理")
operator = st.sidebar.text_input("当前操作人", value="店长").strip() or "店长"
page = st.sidebar.radio(
    "功能菜单", ["门店大盘", "房源管理", "客户管理", "带看登记", "经纪人月报"]
)


# ---------- 公共小组件 ----------

def month_selector(key):
    """最近 12 个月下拉框，返回 (year, month)，默认当月。"""
    today = date.today()
    options, y, m = [], today.year, today.month
    for _ in range(12):
        options.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    idx = st.selectbox(
        "统计月份",
        range(len(options)),
        format_func=lambda i: f"{options[i][0]} 年 {options[i][1]} 月",
        key=key,
    )
    return options[idx]


def csv_download(label, rows, headers, filename):
    """rows: list[dict]，headers: 中文表头（与 dict 键一致）。utf-8-sig 保证 Excel 打开不乱码。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    st.download_button(label, buf.getvalue().encode("utf-8-sig"), filename, "text/csv")


# 户型选项复用 db.LAYOUT_VALUES：数据库 CHECK 枚举与页面下拉框是同一份清单


# ---------- 门店大盘 ----------

def page_dashboard():
    st.header("📊 门店大盘")
    year, month = month_selector("dash_month")
    funnel = services.funnel_stats(conn, year, month)

    cols = st.columns(4)
    for col, (name, value) in zip(cols, funnel.items()):
        col.metric(name, value)

    chart = pd.DataFrame(
        {"阶段": list(funnel.keys()), "数量": list(funnel.values())}
    ).set_index("阶段")
    st.bar_chart(chart)

    st.subheader("本月带看 · 按挂牌价快照分桶")
    buckets = services.funnel_viewing_price_buckets(conn, year, month)
    cols = st.columns(3)
    for col, (label, n) in zip(cols, buckets.items()):
        col.metric(label, n)
    st.caption(
        "按带看登记时冻结的挂牌价快照分桶（与带看记录页同一口径）；"
        "边界值归下桶：150 万整归「150万以下」，200 万整归「150-200万」。"
    )

    st.subheader("本月成交记录")
    deals = services.deals_of_month(conn, year, month)
    if deals:
        rows = [
            {
                "成交日期": d["deal_date"],
                "客户": d["customer_name"],
                "小区": d["community"],
                "成交价(万)": d["deal_price"],
                "经纪人": d["agent_name"],
                "操作人": d["created_by"],
            }
            for d in deals
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("本月暂无成交")

    st.subheader("⏰ 超过 24 小时未补反馈的带看")
    overdue = services.overdue_feedback(conn)
    if overdue:
        rows = [
            {
                "带看时间": v["viewing_time"],
                "客户": v["customer_name"],
                "小区": v["community"],
                "主带经纪人": v["agent_name"],
                "待补反馈": v["pending_agents"],
                "登记人": v["created_by"],
            }
            for v in overdue
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.caption("请到「带看登记」页补齐客户反馈（协同带看需每位参与人各自提交）。")
    else:
        st.success("没有超时未反馈的带看 👍")

    st.subheader("📉 滞销房源（挂牌超 45 天且近两周无带看，该催房东调价了）")
    stale = services.stale_properties(conn)
    if stale:
        today = date.today()
        rows = [
            {
                "小区": p["community"],
                "户型": p["layout"],
                "挂牌价(万)": p["list_price"],
                "挂牌日期": p["list_date"],
                "挂牌天数": (today - date.fromisoformat(p["list_date"])).days,
                "最近带看": p["last_viewing"] or "从未带看",
            }
            for p in stale
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.success("没有滞销房源 👍")


# ---------- 房源管理 ----------

def page_properties():
    st.header("🏘️ 房源管理")
    with st.form("add_property", clear_on_submit=True):
        st.subheader("录入房源")
        c1, c2 = st.columns(2)
        community = c1.text_input("小区名称")
        layout = c2.selectbox("户型", LAYOUTS)
        c3, c4 = st.columns(2)
        price = c3.number_input("挂牌价（万）", min_value=0.0, step=1.0, format="%.1f")
        list_date = c4.date_input("挂牌日期", value=date.today())
        if st.form_submit_button("录入房源", type="primary"):
            if not community.strip():
                st.error("小区名称不能为空")
            elif price <= 0:
                st.error("挂牌价必须大于 0")
            else:
                try:
                    services.add_property(
                        conn, community.strip(), layout, price, list_date.isoformat(), operator
                    )
                    st.success("房源已录入")
                except (ValueError, services.BusyError) as e:
                    st.error(str(e))

    props = repo.list_properties(conn)

    with st.form("adjust_price", clear_on_submit=True):
        st.subheader("房源调价")
        on_sale = [p for p in props if p["status"] == "在售"]
        if not on_sale:
            st.info("没有在售房源，无法调价")
        else:
            p_opts = keyed_options(
                on_sale,
                lambda p: f"{p['community']} {p['layout']}（当前 {p['list_price']:g} 万）",
            )
            prop = p_opts[st.selectbox("调价房源", list(p_opts.keys()))]
            c1, c2 = st.columns(2)
            new_price = c1.number_input("新挂牌价（万）", min_value=0.0, step=1.0, format="%.1f")
            eff_date = c2.date_input("生效日期", value=date.today(), key="adj_eff_date")
            st.caption(
                "生效日期可填过去某天（追溯调价）：已登记的带看仍按登记时冻结的快照价，"
                "之后新登记的带看按新生效价取快照；每次调价都会留痕（见下方调价历史）。"
            )
            if st.form_submit_button("确认调价", type="primary"):
                try:
                    services.adjust_price(
                        conn, prop["id"], new_price, eff_date.isoformat(), operator
                    )
                    st.success(f"已调价：{prop['list_price']:g} 万 → {new_price:g} 万（{eff_date} 起生效）")
                except (ValueError, services.BusyError) as e:
                    st.error(str(e))

    st.subheader("全部房源")
    today = date.today()
    rows = [
        {
            "小区": p["community"],
            "户型": p["layout"],
            "挂牌价(万)": p["list_price"],
            "挂牌日期": p["list_date"],
            "挂牌天数": (today - date.fromisoformat(p["list_date"])).days,
            "带看次数": p["viewing_count"],
            "状态": p["status"],
            "录入人": p["created_by"],
            "录入时间": p["created_at"],
        }
        for p in props
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    csv_download(
        "导出房源 CSV",
        rows,
        ["小区", "户型", "挂牌价(万)", "挂牌日期", "挂牌天数", "带看次数", "状态", "录入人", "录入时间"],
        "房源.csv",
    )

    st.subheader("调价历史")
    if not props:
        st.info("暂无房源")
    else:
        h_opts = keyed_options(props, lambda p: f"{p['community']} {p['layout']}")
        h_prop = h_opts[st.selectbox("查看房源", list(h_opts.keys()))]
        history = repo.list_price_adjustments(conn, h_prop["id"])
        st.dataframe(
            [
                {
                    "登记时间": h["created_at"],
                    "生效日期": h["effective_date"],
                    "调价前(万)": h["old_price"] if h["old_price"] is not None else "—（初始挂牌）",
                    "调价后(万)": h["new_price"],
                    "操作人": h["operator"],
                }
                for h in history
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption("带看的价格快照按「带看日期」从本表推算：生效日期不晚于带看日期的最新一条记录。")


# ---------- 客户管理 ----------

def page_customers():
    st.header("👥 客户管理")

    agents = repo.list_agents(conn)

    with st.form("add_customer", clear_on_submit=True):
        st.subheader("新增客户")
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("客户姓名")
        phone = c2.text_input("联系电话")
        agent = agents[c3.selectbox(
            "负责经纪人", range(len(agents)), format_func=lambda i: agents[i]["name"]
        )]
        if st.form_submit_button("新增客户", type="primary"):
            if not name.strip():
                st.error("客户姓名不能为空")
            else:
                try:
                    services.register_customer(
                        conn, name.strip(), phone.strip(), agent["id"], operator
                    )
                    st.success("客户已新增，初始状态：新客")
                except (ValueError, services.BusyError) as e:
                    st.error(str(e))

    st.subheader("推进客户状态")
    customers = repo.list_customers(conn)
    active = [c for c in customers if c["status"] not in TERMINAL_STATUSES]
    if not active:
        st.info("当前没有进行中的客户")
    else:
        options = keyed_options(active, lambda c: f"{c['name']}（当前：{c['status']}）")
        customer = options[st.selectbox("选择客户", list(options.keys()))]
        targets = next_statuses(customer["status"])
        st.write(f"当前状态：**{customer['status']}**，只能推进到：{' / '.join(targets)}")
        target = st.radio("目标状态", targets, horizontal=True)

        deal = None
        can_submit = True
        if target == STATUS_DEAL:
            on_sale = [p for p in repo.list_properties(conn) if p["status"] == "在售"]
            if not on_sale:
                st.warning("没有在售房源，无法登记成交")
                can_submit = False
            else:
                p_opts = keyed_options(
                    on_sale,
                    lambda p: f"{p['community']} {p['layout']}（挂牌 {p['list_price']} 万，{p['list_date']}）",
                )
                prop = p_opts[st.selectbox("成交房源", list(p_opts.keys()))]
                d1, d2 = st.columns(2)
                deal_price = d1.number_input("成交价（万）", min_value=0.0, step=1.0, format="%.1f")
                deal_date = d2.date_input("成交日期", value=date.today())
                deal_agent_idx = next(
                    (i for i, ag in enumerate(agents) if ag["id"] == customer["agent_id"]), 0
                )
                deal_agent = agents[st.selectbox(
                    "成交经纪人（业绩归属，默认负责经纪人）",
                    range(len(agents)), index=deal_agent_idx,
                    format_func=lambda i: agents[i]["name"],
                )]
                deal = (prop, deal_price, deal_date, deal_agent)

        if can_submit and st.button("确认推进", type="primary"):
            try:
                if target == STATUS_DEAL:
                    prop, deal_price, deal_date, deal_agent = deal
                    services.close_deal(
                        conn, customer["id"], prop["id"], deal_agent["id"],
                        deal_price, deal_date.isoformat(), operator,
                    )
                    st.success(
                        f"已成交：{customer['name']} × {prop['community']}（归属：{deal_agent['name']}）"
                    )
                else:
                    services.advance_customer(conn, customer["id"], target, operator)
                    st.success(f"已推进：{customer['status']} → {target}")
            except (InvalidTransitionError, ValueError, services.BusyError) as e:
                st.error(str(e))

        st.caption("该客户跟进记录")
        history = repo.list_status_history(conn, customer["id"])
        st.dataframe(
            [
                {
                    "时间": h["created_at"],
                    "从": h["from_status"] or "—",
                    "到": h["to_status"],
                    "操作人": h["operator"],
                }
                for h in history
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("全部客户")
    rows = [
        {
            "姓名": c["name"],
            "电话": c["phone"],
            "状态": c["status"],
            "负责经纪人": c["agent_name"],
            "录入人": c["created_by"],
            "录入时间": c["created_at"],
            "最后更新": c["updated_at"],
        }
        for c in customers
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    csv_download(
        "导出客户 CSV",
        rows,
        ["姓名", "电话", "状态", "负责经纪人", "录入人", "录入时间", "最后更新"],
        "客户.csv",
    )


# ---------- 带看登记 ----------

def page_viewings():
    st.header("🚗 带看登记")

    customers = [c for c in repo.list_customers(conn) if c["status"] not in TERMINAL_STATUSES]
    props = [p for p in repo.list_properties(conn) if p["status"] == "在售"]
    agents = repo.list_agents(conn)

    if not customers or not props:
        st.warning("需要先有进行中的客户和在售房源，才能登记带看")
    else:
        c_opts = keyed_options(customers, lambda c: f"{c['name']}（{c['status']}）")
        customer = c_opts[st.selectbox("客户", list(c_opts.keys()))]
        p_opts = keyed_options(
            props,
            lambda p: f"{p['community']} {p['layout']}（{p['list_price']:g} 万，挂牌 {p['list_date']}）",
        )
        prop = p_opts[st.selectbox("房源", list(p_opts.keys()))]
        default_idx = next(
            (i for i, a in enumerate(agents) if a["id"] == customer["agent_id"]), 0
        )
        agent = agents[st.selectbox(
            "主带经纪人", range(len(agents)),
            index=default_idx, format_func=lambda i: agents[i]["name"],
        )]
        assist_pool = [a for a in agents if a["id"] != agent["id"]]
        assist_names = st.multiselect(
            "协同经纪人（可选，最多 2 人；业绩按主带 70%、协同平分 30% 折算带看积分）",
            [a["name"] for a in assist_pool],
            max_selections=2,
        )
        assist_ids = [a["id"] for a in assist_pool if a["name"] in assist_names]
        d1, d2 = st.columns(2)
        v_date = d1.date_input("带看日期", value=date.today())
        v_time = d2.time_input(
            "带看时间", value=datetime.now().time().replace(second=0, microsecond=0)
        )
        st.caption(
            "登记时自动按带看日期取当时生效的挂牌价快照（之后调价不改写）；"
            "新客首次带看后，状态会自动推进为「带看」（协同带看也只推一次）。"
        )
        if st.button("登记带看", type="primary"):
            try:
                vid = services.record_viewing(
                    conn, customer["id"], prop["id"], agent["id"],
                    datetime.combine(v_date, v_time), operator,
                    assist_agent_ids=assist_ids,
                )
                snapshot = repo.get_viewing(conn, vid)["list_price_snapshot"]
                st.success(f"带看已登记（挂牌价快照 {snapshot:g} 万），记得 24 小时内补客户反馈")
            except (InvalidTransitionError, ValueError, services.BusyError) as e:
                st.error(str(e))

    all_viewings = repo.list_viewings(conn)
    participants = repo.list_all_participants(conn)
    by_viewing = {}
    for p in participants:
        by_viewing.setdefault(p["viewing_id"], []).append(p)

    st.subheader("补客户反馈")
    pending_pairs = [
        (v, p)
        for v in all_viewings
        for p in by_viewing.get(v["id"], [])
        if not p["feedback"]
    ]
    if not pending_pairs:
        st.info("没有待补反馈的带看")
    else:
        p_opts = {
            f"#{v['id']}-{p['agent_id']} {v['viewing_time']}　{v['customer_name']} @ "
            f"{v['community']}（待 {p['agent_name']}·{p['role']}）": (v, p)
            for v, p in pending_pairs
        }
        viewing, participant = p_opts[st.selectbox("选择待反馈记录", list(p_opts.keys()))]
        feedback = st.text_area("客户反馈", placeholder="客户看完怎么说？意向、顾虑、还价……")
        if st.button("提交反馈", type="primary"):
            if not feedback.strip():
                st.error("反馈内容不能为空")
            else:
                try:
                    services.submit_feedback(
                        conn, viewing["id"], participant["agent_id"], feedback.strip()
                    )
                    st.success(f"已保存（{participant['agent_name']} 的反馈）")
                except (ValueError, services.BusyError) as e:
                    st.error(str(e))

    st.subheader("全部带看记录")
    now = datetime.now()
    rows = []
    for v in all_viewings:
        parts = by_viewing.get(v["id"], [])
        lead = next((p for p in parts if p["role"] == "主带"), None)
        assists = [p for p in parts if p["role"] == "协同"]
        missing = [p for p in parts if not p["feedback"]]
        vt = datetime.strptime(v["viewing_time"], services.FMT)
        if parts and not missing:
            fb_status = "已反馈"
        elif (now - vt).total_seconds() > services.FEEDBACK_DEADLINE_HOURS * 3600:
            fb_status = "⚠️ 已超时"
        else:
            fb_status = f"待反馈（缺 {len(missing)} 人）" if parts else "待反馈"
        rows.append(
            {
                "带看时间": v["viewing_time"],
                "客户": v["customer_name"],
                "小区": v["community"],
                "户型": v["layout"],
                "挂牌价快照(万)": v["list_price_snapshot"],
                "价格区间": services.price_bucket(v["list_price_snapshot"]),
                "主带": lead["agent_name"] if lead else v["agent_name"],
                "协同": "、".join(p["agent_name"] for p in assists) or "—",
                "反馈状态": fb_status,
                "客户反馈": "；".join(
                    f"{p['agent_name']}：{p['feedback']}" for p in parts if p["feedback"]
                ),
                "登记人": v["created_by"],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    csv_download(
        "导出带看 CSV",
        rows,
        ["带看时间", "客户", "小区", "户型", "挂牌价快照(万)", "价格区间",
         "主带", "协同", "反馈状态", "客户反馈", "登记人"],
        "带看记录.csv",
    )


# ---------- 经纪人月报 ----------

def page_report():
    st.header("📈 经纪人月报")
    year, month = month_selector("report_month")
    rows = [
        {
            "经纪人": r["name"],
            "带看组数(主带)": r["viewing_count"],
            "带看积分": r["viewing_points"],
            "成交单数": r["deal_count"],
        }
        for r in services.monthly_agent_report(conn, year, month)
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(
        "带看积分：单独带看 1 分；协同带看主带 0.7 分、协同经纪人平分 0.3 分。"
        "带看组数按主带计，与门店漏斗同口径（一条协同带看只算一次）。"
    )
    csv_download(
        "导出月报 CSV",
        rows,
        ["经纪人", "带看组数(主带)", "带看积分", "成交单数"],
        f"经纪人月报-{year}-{month:02d}.csv",
    )


if page == "门店大盘":
    page_dashboard()
elif page == "房源管理":
    page_properties()
elif page == "客户管理":
    page_customers()
elif page == "带看登记":
    page_viewings()
else:
    page_report()
