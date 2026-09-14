"""业务逻辑层：状态推进、时间计算、统计与提醒的编排。

不写 SQL（全部在 repository.py），状态规则在 state_machine.py。
页面的所有写操作只能调本层函数，由本层负责事务提交。
业务规则在本层强制执行（不是靠页面藏选项）：任何入口（页面、脚本、未来的 API）
绕过页面直接调用本层，非法数据一样会被拒绝；数据库层的约束是最后一道防线。
时间一律使用本地时间。
"""
import math
import sqlite3
from datetime import datetime, timedelta

import repository as repo
from db import (
    LAYOUT_VALUES,
    MAX_COMMUNITY,
    MAX_FEEDBACK,
    MAX_NAME,
    MAX_PHONE,
    MAX_PRICE,
)
from state_machine import (
    ALL_STATUSES,
    STATUS_NEW,
    STATUS_VIEWING,
    STATUS_DEAL,
    TERMINAL_STATUSES,
    validate_transition,
)

FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"
FEEDBACK_DEADLINE_HOURS = 24  # 带看后 24 小时内必须补客户反馈
STALE_LIST_DAYS = 45          # 挂牌超过 45 天
STALE_IDLE_DAYS = 14          # 且最近 14 天没有带看 → 提醒催房东调价

PROPERTY_ON_SALE = "在售"
PROPERTY_SOLD = "已成交"


class ValidationError(ValueError):
    """业务规则校验失败（页面可直接展示消息）。"""


class BusyError(RuntimeError):
    """多人并发写冲突且等待超时（页面可提示稍后重试）。"""


def _fmt(dt):
    return dt.strftime(FMT)


def now_str():
    return _fmt(datetime.now())


def _begin(conn):
    """开启立即拿写锁的事务。

    SQLite 默认延迟事务在首次写时才加锁，多人同时提交会在升级瞬间报 SQLITE_BUSY，
    且"先读后写"的校验存在竞态。BEGIN IMMEDIATE 一进来就持有唯一写锁：
    后来的写事务排队等待，本事务内读到的状态在提交前不会被别人改动。
    """
    conn.execute("BEGIN IMMEDIATE")


def _rollback(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _is_lock_error(exc):
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _resolve_ts(now):
    """统一处理可选的 now 参数：接受 datetime 或 FMT 字符串，拒绝其他类型。"""
    if now is None:
        return now_str()
    if isinstance(now, datetime):
        return _fmt(now)
    if isinstance(now, str):
        try:
            datetime.strptime(now, FMT)
        except ValueError:
            raise ValidationError(f"时间必须是 {FMT} 格式或 datetime")
        return now
    raise ValidationError("时间必须是 datetime 或时间字符串")


def _require_text(value, label, max_len):
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文本")
    value = value.strip()
    if not value:
        raise ValidationError(f"{label}不能为空")
    if len(value) > max_len:
        raise ValidationError(f"{label}不能超过 {max_len} 个字符")
    return value


def _require_phone(value, label="联系电话"):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文本")
    if len(value) > MAX_PHONE:
        raise ValidationError(f"{label}不能超过 {MAX_PHONE} 个字符")
    return value.strip()


def _require_positive(value, label, max_value=MAX_PRICE):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}必须是数字")
    if not math.isfinite(value):  # 拒绝 NaN / Inf
        raise ValidationError(f"{label}必须是有限数字")
    if value <= 0:
        raise ValidationError(f"{label}必须大于 0")
    if value > max_value:
        raise ValidationError(f"{label}不能超过 {max_value:g}")
    return float(value)


def _parse_date(value, label):
    try:
        return datetime.strptime(value, DATE_FMT)
    except (TypeError, ValueError):
        raise ValidationError(f"{label}必须是真实存在的日期（YYYY-MM-DD）")


def _require_date(value, label, *, not_future=False, now=None):
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是 YYYY-MM-DD 格式的日期")
    parsed = _parse_date(value, label).date()
    if not_future and parsed > (now or datetime.now()).date():
        raise ValidationError(f"{label}不能是未来日期")
    return value


def _require_timestamp(value, label, *, not_future_against=None):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.strptime(value, FMT)
        except ValueError:
            parsed = None
        if parsed is None:
            raise ValidationError(f"{label}必须是 {FMT} 格式的时间")
    else:
        raise ValidationError(f"{label}必须是时间")
    if not_future_against is not None and parsed > not_future_against:
        raise ValidationError(f"{label}不能晚于登记时间")
    return _fmt(parsed)


def _get_or_raise(getter, id_, label):
    if not isinstance(id_, int) or isinstance(id_, bool):
        raise ValidationError(f"{label}编号必须是整数")
    row = getter(id_)
    if row is None:
        raise ValidationError(f"{label}不存在：{id_}")
    return row


def _require_int_id(id_, label):
    if not isinstance(id_, int) or isinstance(id_, bool):
        raise ValidationError(f"{label}编号必须是整数")


def validate_transition_value(to_status):
    """不碰库的值域预检：目标状态必须是已知状态（具体能否流转在事务内判定）。"""
    if to_status not in ALL_STATUSES:
        raise ValidationError(f"未知状态：{to_status}")


def _get_customer(conn, customer_id):
    return _get_or_raise(lambda i: repo.get_customer(conn, i), customer_id, "客户")


def _get_property(conn, property_id):
    return _get_or_raise(lambda i: repo.get_property(conn, i), property_id, "房源")


def _get_active_agent(conn, agent_id):
    agent = _get_or_raise(lambda i: repo.get_agent(conn, i), agent_id, "经纪人")
    if not agent["active"]:
        raise ValidationError(f"经纪人已停用：{agent['name']}")
    return agent


def month_bounds(year, month):
    """返回 [start, end) 的月份边界（'YYYY-MM-DD'，左闭右开，跨月不串）。"""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


# ---------- 写操作（页面只能调这里） ----------
#
# 统一并发模型：参数自检（不碰库）→ BEGIN IMMEDIATE（拿唯一写锁）→ 在事务内
# 读最新状态并做依赖数据的校验 → 多步写入 → commit。这样两个用户同时操作时，
# 后到者在 BEGIN 处排队，事务内读到的状态在提交前不会被别人改动，没有 TOCTOU；
# 等待超过 busy_timeout 抛 BusyError（提示重试），约束冲突抛 ValidationError。

def add_agent(conn, name, phone="", now=None):
    name = _require_text(name, "经纪人姓名", MAX_NAME)
    phone = _require_phone(phone)
    ts = _resolve_ts(now)
    try:
        _begin(conn)
        agent_id = repo.add_agent(conn, name, phone, ts)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except sqlite3.IntegrityError:
        _rollback(conn)
        raise ValidationError(f"经纪人已存在：{name}")
    return agent_id


def add_property(conn, community, layout, list_price, list_date, operator, now=None):
    community = _require_text(community, "小区名称", MAX_COMMUNITY)
    operator = _require_text(operator, "操作人", MAX_NAME)
    if layout not in LAYOUT_VALUES:
        raise ValidationError(f"户型不合法：{layout}")
    list_price = _require_positive(list_price, "挂牌价")
    ts_dt = datetime.strptime(_resolve_ts(now), FMT)
    list_date = _require_date(list_date, "挂牌日期", not_future=True, now=ts_dt)
    ts = _fmt(ts_dt)
    try:
        _begin(conn)
        pid = repo.add_property(conn, community, layout, list_price, list_date, operator, ts)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"房源数据不合法：{e}") from e
    return pid


def register_customer(conn, name, phone, agent_id, operator, now=None):
    """新增客户，初始状态「新客」，并写入第一条状态历史。"""
    name = _require_text(name, "客户姓名", MAX_NAME)
    phone = _require_phone(phone)
    operator = _require_text(operator, "操作人", MAX_NAME)
    _require_int_id(agent_id, "经纪人")
    ts = _resolve_ts(now)
    try:
        _begin(conn)
        _get_active_agent(conn, agent_id)
        cid = repo.add_customer(conn, name, phone, agent_id, operator, ts)
        repo.add_status_history(conn, cid, None, STATUS_NEW, operator, ts)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except ValueError:
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"客户数据不合法：{e}") from e
    return cid


def advance_customer(conn, customer_id, to_status, operator, now=None):
    """推进客户状态。成交必须走 close_deal（要登记成交房源和价格）。"""
    operator = _require_text(operator, "操作人", MAX_NAME)
    _require_int_id(customer_id, "客户")
    validate_transition_value(to_status)
    ts = _resolve_ts(now)
    try:
        _begin(conn)
        customer = _get_customer(conn, customer_id)
        validate_transition(customer["status"], to_status)
        if to_status == STATUS_DEAL:
            raise ValidationError("成交必须通过 close_deal 登记成交房源和成交价")
        if to_status == STATUS_VIEWING and not repo.has_viewing(conn, customer_id):
            # 「带看」状态只能由登记带看触发，避免客户从没被带看过却凭空进入带看阶段
            raise ValidationError("推进到「带看」前必须先登记一条该客户的带看记录")
        # 先写历史再改状态：数据库触发器据此校验每次状态变更都有留痕
        repo.add_status_history(conn, customer_id, customer["status"], to_status, operator, ts)
        repo.update_customer_status(conn, customer_id, to_status, ts)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except ValueError:
        # ValidationError / InvalidTransitionError 都是业务拒绝：回滚后原样抛出
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"状态推进被数据库拒绝：{e}") from e


def record_viewing(conn, customer_id, property_id, agent_id, viewing_time, operator, now=None):
    """登记带看；如果客户还是「新客」，首次带看后自动推进为「带看」。

    规则（服务层强制，不靠页面过滤）：
    - 客户必须存在且不在终态（成交/流失客户不能再登记带看）
    - 房源必须在售（已成交房源不能带看）
    - 经纪人必须在职
    - 带看时间不能是未来、不能晚于登记时间、不能早于客户建档或房源挂牌
    """
    operator = _require_text(operator, "操作人", MAX_NAME)
    _require_int_id(customer_id, "客户")
    _require_int_id(property_id, "房源")
    _require_int_id(agent_id, "经纪人")
    ts = _resolve_ts(now)
    ts_dt = datetime.strptime(ts, FMT)
    vt = _require_timestamp(viewing_time, "带看时间", not_future_against=ts_dt)
    vt_dt = datetime.strptime(vt, FMT)
    try:
        _begin(conn)
        customer = _get_customer(conn, customer_id)
        if customer["status"] in TERMINAL_STATUSES:
            raise ValidationError(f"客户当前为「{customer['status']}」，不能登记带看")
        prop = _get_property(conn, property_id)
        if prop["status"] != PROPERTY_ON_SALE:
            raise ValidationError(f"房源「{prop['community']}」已成交，不能登记带看")
        _get_active_agent(conn, agent_id)
        customer_created = datetime.strptime(customer["created_at"], FMT)
        if vt_dt < customer_created:
            raise ValidationError("带看时间不能早于客户建档时间")
        if vt_dt.date() < datetime.strptime(prop["list_date"], DATE_FMT).date():
            raise ValidationError("带看时间不能早于房源挂牌日期")
        vid = repo.add_viewing(conn, customer_id, property_id, agent_id, vt, operator, ts)
        if customer["status"] == STATUS_NEW:
            # 带看先入库（触发器此时看到的客户仍是「新客」），再自动推进状态
            repo.add_status_history(conn, customer_id, STATUS_NEW, STATUS_VIEWING, operator, ts)
            repo.update_customer_status(conn, customer_id, STATUS_VIEWING, ts)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except ValueError:
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"带看登记被数据库拒绝：{e}") from e
    return vid


def submit_feedback(conn, viewing_id, feedback, now=None):
    feedback = _require_text(feedback, "客户反馈", MAX_FEEDBACK)
    _require_int_id(viewing_id, "带看记录")
    ts = _resolve_ts(now)
    try:
        _begin(conn)
        viewing = repo.get_viewing(conn, viewing_id)
        if viewing is None:
            raise ValidationError(f"带看记录不存在：{viewing_id}")
        if viewing["feedback"]:
            raise ValidationError("该带看已补过反馈，不能重复提交")
        changed = repo.set_feedback(conn, viewing_id, feedback, ts)
        if not changed:
            raise ValidationError(f"带看记录不存在：{viewing_id}")
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except ValueError:
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"反馈保存失败：{e}") from e


def close_deal(conn, customer_id, property_id, agent_id, deal_price, deal_date, operator, now=None):
    """客户推进为「成交」并登记成交单，房源同时标记「已成交」。一个事务完成。

    规则（服务层强制）：
    - 客户状态必须能合法流转到「成交」（即当前为谈价，不能跳级、不能是终态）
    - 房源必须仍在售（一套房只能成交一次）
    - 经纪人必须在职；成交价必须为正的有限数字
    - 成交日期必须真实存在、不能晚于登记时间、不能早于挂牌日或该客户首次带看
    """
    operator = _require_text(operator, "操作人", MAX_NAME)
    _require_int_id(customer_id, "客户")
    _require_int_id(property_id, "房源")
    _require_int_id(agent_id, "经纪人")
    deal_price = _require_positive(deal_price, "成交价")
    ts = _resolve_ts(now)
    ts_dt = datetime.strptime(ts, FMT)
    deal_d = _parse_date(deal_date, "成交日期").date()
    if deal_d > ts_dt.date():
        raise ValidationError("成交日期不能晚于登记日期")
    try:
        _begin(conn)
        customer = _get_customer(conn, customer_id)
        prop = _get_property(conn, property_id)
        _get_active_agent(conn, agent_id)
        validate_transition(customer["status"], STATUS_DEAL)
        if prop["status"] != PROPERTY_ON_SALE:
            raise ValidationError(
                f"房源「{prop['community']}」不是在售状态（可能刚被他人成交），请刷新后重试"
            )
        list_d = datetime.strptime(prop["list_date"], DATE_FMT).date()
        if deal_d < list_d:
            raise ValidationError("成交日期不能早于房源挂牌日期")
        first_viewing = repo.first_viewing_date(conn, customer_id)
        if first_viewing is not None and deal_d < first_viewing:
            raise ValidationError("成交日期不能早于该客户的首次带看日期")
        # 顺序：留痕 → 客户成交 → 成交单（要求客户已成交、房源仍在售）→ 房源已成交
        repo.add_status_history(conn, customer_id, customer["status"], STATUS_DEAL, operator, ts)
        repo.update_customer_status(conn, customer_id, STATUS_DEAL, ts)
        repo.add_deal(conn, customer_id, property_id, agent_id, deal_price, deal_date, operator, ts)
        repo.update_property_status(conn, property_id, PROPERTY_SOLD)
        conn.commit()
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if _is_lock_error(e):
            raise BusyError("系统繁忙，请稍后重试") from e
        raise
    except ValueError:
        _rollback(conn)
        raise
    except sqlite3.IntegrityError as e:
        _rollback(conn)
        raise ValidationError(f"成交登记被数据库拒绝：{e}") from e


# ---------- 统计与提醒 ----------

def funnel_stats(conn, year, month):
    """门店漏斗：本月新客数、带看组数、谈价组数、成交单数。

    时间口径统一为「业务实际发生时间」：新客按登记时间、带看按带看时间、
    成交按成交日期 deals.deal_date（不是系统录入时间），因此本函数的成交数
    与 monthly_agent_report 的成交合计、deals_of_month 的列表条数必然一致。
    """
    start, end = month_bounds(year, month)
    return {
        "新客": repo.count_customers_created(conn, start, end),
        "带看": repo.count_viewings(conn, start, end),
        "谈价": repo.count_status_entries(conn, "谈价", start, end),
        "成交": repo.count_deals(conn, start, end),
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
