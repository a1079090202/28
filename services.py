"""业务逻辑层：状态推进、时间计算、统计与提醒的编排。

不写 SQL（全部在 repository.py），状态规则在 state_machine.py。
页面的所有写操作只能调本层函数，由本层负责事务提交。
时间一律使用本地时间。
"""
from datetime import datetime, timedelta

import repository as repo
from state_machine import (
    STATUS_NEW,
    STATUS_VIEWING,
    STATUS_DEAL,
    validate_transition,
)

FMT = "%Y-%m-%d %H:%M:%S"
FEEDBACK_DEADLINE_HOURS = 24  # 带看后 24 小时内必须补客户反馈
STALE_LIST_DAYS = 45          # 挂牌超过 45 天
STALE_IDLE_DAYS = 14          # 且最近 14 天没有带看 → 提醒催房东调价


def _fmt(dt):
    return dt.strftime(FMT)


def now_str():
    return _fmt(datetime.now())


def month_bounds(year, month):
    """返回 [start, end) 的月份边界（'YYYY-MM-DD'，左闭右开，跨月不串）。"""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


# ---------- 写操作（页面只能调这里） ----------

def add_agent(conn, name, phone="", now=None):
    ts = _fmt(now) if now else now_str()
    agent_id = repo.add_agent(conn, name, phone, ts)
    conn.commit()
    return agent_id


def add_property(conn, community, layout, list_price, list_date, operator, now=None):
    ts = _fmt(now) if now else now_str()
    pid = repo.add_property(conn, community, layout, list_price, list_date, operator, ts)
    conn.commit()
    return pid


def register_customer(conn, name, phone, agent_id, operator, now=None):
    """新增客户，初始状态「新客」，并写入第一条状态历史。"""
    ts = _fmt(now) if now else now_str()
    cid = repo.add_customer(conn, name, phone, agent_id, operator, ts)
    repo.add_status_history(conn, cid, None, STATUS_NEW, operator, ts)
    conn.commit()
    return cid


def advance_customer(conn, customer_id, to_status, operator, now=None):
    """推进客户状态。成交必须走 close_deal（要登记成交房源和价格）。"""
    ts = _fmt(now) if now else now_str()
    customer = repo.get_customer(conn, customer_id)
    if customer is None:
        raise ValueError(f"客户不存在：{customer_id}")
    validate_transition(customer["status"], to_status)
    if to_status == STATUS_DEAL:
        raise ValueError("成交必须通过 close_deal 登记成交房源和成交价")
    repo.update_customer_status(conn, customer_id, to_status, ts)
    repo.add_status_history(conn, customer_id, customer["status"], to_status, operator, ts)
    conn.commit()


def record_viewing(conn, customer_id, property_id, agent_id, viewing_time, operator, now=None):
    """登记带看；如果客户还是「新客」，首次带看后自动推进为「带看」。"""
    ts = _fmt(now) if now else now_str()
    vt = _fmt(viewing_time) if isinstance(viewing_time, datetime) else viewing_time
    vid = repo.add_viewing(conn, customer_id, property_id, agent_id, vt, operator, ts)
    customer = repo.get_customer(conn, customer_id)
    if customer["status"] == STATUS_NEW:
        repo.update_customer_status(conn, customer_id, STATUS_VIEWING, ts)
        repo.add_status_history(conn, customer_id, STATUS_NEW, STATUS_VIEWING, operator, ts)
    conn.commit()
    return vid


def submit_feedback(conn, viewing_id, feedback, now=None):
    ts = _fmt(now) if now else now_str()
    repo.set_feedback(conn, viewing_id, feedback, ts)
    conn.commit()


def close_deal(conn, customer_id, property_id, agent_id, deal_price, deal_date, operator, now=None):
    """客户推进为「成交」并登记成交单，房源同时标记「已成交」。一个事务完成。"""
    ts = _fmt(now) if now else now_str()
    customer = repo.get_customer(conn, customer_id)
    if customer is None:
        raise ValueError(f"客户不存在：{customer_id}")
    validate_transition(customer["status"], STATUS_DEAL)
    repo.update_customer_status(conn, customer_id, STATUS_DEAL, ts)
    repo.add_status_history(conn, customer_id, customer["status"], STATUS_DEAL, operator, ts)
    repo.add_deal(conn, customer_id, property_id, agent_id, deal_price, deal_date, operator, ts)
    repo.update_property_status(conn, property_id, "已成交")
    conn.commit()


# ---------- 统计与提醒 ----------

def funnel_stats(conn, year, month):
    """门店漏斗：本月新客数、带看组数、谈价组数、成交单数。"""
    start, end = month_bounds(year, month)
    return {
        "新客": repo.count_customers_created(conn, start, end),
        "带看": repo.count_viewings(conn, start, end),
        "谈价": repo.count_status_entries(conn, "谈价", start, end),
        "成交": repo.count_status_entries(conn, "成交", start, end),
    }


def stale_properties(conn, now=None):
    """挂牌超 45 天且近 14 天无带看的在售房源（该催房东调价了）。"""
    now = now or datetime.now()
    list_before = (now - timedelta(days=STALE_LIST_DAYS)).strftime("%Y-%m-%d")
    idle_since = _fmt(now - timedelta(days=STALE_IDLE_DAYS))
    return repo.stale_properties(conn, list_before, idle_since)


def overdue_feedback(conn, now=None):
    """超过 24 小时仍未补客户反馈的带看。"""
    now = now or datetime.now()
    deadline = _fmt(now - timedelta(hours=FEEDBACK_DEADLINE_HOURS))
    return repo.overdue_feedback_viewings(conn, deadline)


def monthly_agent_report(conn, year, month):
    """每个经纪人当月的带看组数和成交单数。"""
    start, end = month_bounds(year, month)
    return repo.agent_monthly_report(conn, start, end)


def deals_of_month(conn, year, month):
    start, end = month_bounds(year, month)
    return repo.list_deals_in_range(conn, start, end)
