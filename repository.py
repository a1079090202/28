"""数据访问层：全部 SQL 集中在这里，页面代码不出现任何 SQL。

约定：本层函数只执行 SQL，不 commit；写操作的提交由 services 层统一负责，
保证多步写入（如成交 = 改状态 + 写历史 + 写成交单）在一个事务里。
时间一律为本地时间字符串：日期 'YYYY-MM-DD'，时间 'YYYY-MM-DD HH:MM:SS'。
"""
from datetime import datetime


# ---------- 经纪人 ----------

def add_agent(conn, name, phone, now):
    cur = conn.execute(
        "INSERT INTO agents (name, phone, created_at) VALUES (?, ?, ?)",
        (name, phone, now),
    )
    return cur.lastrowid


def list_agents(conn, only_active=True):
    sql = "SELECT * FROM agents"
    if only_active:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY id").fetchall()


def get_agent(conn, agent_id):
    return conn.execute(
        "SELECT * FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()


# ---------- 房源 ----------

def add_property(conn, community, layout, list_price, list_date, operator, now):
    cur = conn.execute(
        """INSERT INTO properties (community, layout, list_price, list_date, status, created_by, created_at)
           VALUES (?, ?, ?, ?, '在售', ?, ?)""",
        (community, layout, list_price, list_date, operator, now),
    )
    return cur.lastrowid


def get_property(conn, property_id):
    return conn.execute(
        "SELECT * FROM properties WHERE id = ?", (property_id,)
    ).fetchone()


def list_properties(conn):
    """全部房源；viewing_count 是该房源的带看次数（按带看记录计，协同带看只算一次）。"""
    return conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM viewings v WHERE v.property_id = p.id) AS viewing_count
           FROM properties p ORDER BY list_date DESC, id DESC"""
    ).fetchall()


def update_property_status(conn, property_id, status):
    conn.execute("UPDATE properties SET status = ? WHERE id = ?", (status, property_id))


# ---------- 调价留痕 ----------

def add_price_adjustment(conn, property_id, old_price, new_price, effective_date, operator, now):
    """写入一条调价记录（初始挂牌行 old_price 为 None）。

    properties.list_price 由数据库触发器同步为最新生效价，本层不重复写。
    """
    cur = conn.execute(
        """INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (property_id, old_price, new_price, effective_date, operator, now),
    )
    return cur.lastrowid


def list_price_adjustments(conn, property_id):
    """某房源的调价历史（含初始挂牌行），最新的在前。"""
    return conn.execute(
        """SELECT * FROM price_adjustments
           WHERE property_id = ? ORDER BY effective_date DESC, id DESC""",
        (property_id,),
    ).fetchall()


def latest_price_adjustment(conn, property_id):
    """当前最新一条调价记录（时间线末尾），没有则 None。"""
    return conn.execute(
        """SELECT * FROM price_adjustments
           WHERE property_id = ? ORDER BY effective_date DESC, id DESC LIMIT 1""",
        (property_id,),
    ).fetchone()


def price_on_date(conn, property_id, date_str):
    """date_str（'YYYY-MM-DD'）当天生效的挂牌价：生效日期不晚于该日的最新一条记录。

    房源必有初始挂牌行（生效日期 = 挂牌日期），因此 date_str >= 挂牌日期时必有结果；
    返回 None 仅意味着该日早于挂牌日。
    """
    row = conn.execute(
        """SELECT new_price FROM price_adjustments
           WHERE property_id = ? AND effective_date <= ?
           ORDER BY effective_date DESC, id DESC LIMIT 1""",
        (property_id, date_str),
    ).fetchone()
    return row[0] if row else None


# ---------- 客户 ----------

def add_customer(conn, name, phone, agent_id, operator, now):
    cur = conn.execute(
        """INSERT INTO customers (name, phone, status, agent_id, created_by, created_at, updated_at)
           VALUES (?, ?, '新客', ?, ?, ?, ?)""",
        (name, phone, agent_id, operator, now, now),
    )
    return cur.lastrowid


def get_customer(conn, customer_id):
    return conn.execute(
        "SELECT * FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()


def list_customers(conn):
    return conn.execute(
        """SELECT c.*, a.name AS agent_name
           FROM customers c JOIN agents a ON a.id = c.agent_id
           ORDER BY c.id DESC"""
    ).fetchall()


def update_customer_status(conn, customer_id, to_status, now):
    conn.execute(
        "UPDATE customers SET status = ?, updated_at = ? WHERE id = ?",
        (to_status, now, customer_id),
    )


def add_status_history(conn, customer_id, from_status, to_status, operator, now):
    conn.execute(
        """INSERT INTO status_history (customer_id, from_status, to_status, operator, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (customer_id, from_status, to_status, operator, now),
    )


def list_status_history(conn, customer_id):
    return conn.execute(
        "SELECT * FROM status_history WHERE customer_id = ? ORDER BY created_at, id",
        (customer_id,),
    ).fetchall()


# ---------- 带看 ----------

def add_viewing(conn, customer_id, property_id, agent_id, viewing_time, list_price_snapshot, operator, now):
    cur = conn.execute(
        """INSERT INTO viewings (customer_id, property_id, agent_id, viewing_time, list_price_snapshot, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, property_id, agent_id, viewing_time, list_price_snapshot, operator, now),
    )
    return cur.lastrowid


def list_viewings(conn):
    """全部带看记录（主带经纪人来自 viewings.agent_id；参与人明细用 list_all_participants）。"""
    return conn.execute(
        """SELECT v.id, v.viewing_time, v.customer_id, v.property_id, v.list_price_snapshot,
                  c.name AS customer_name, p.community, p.layout,
                  a.name AS agent_name, v.created_by, v.created_at
           FROM viewings v
           JOIN customers c ON c.id = v.customer_id
           JOIN properties p ON p.id = v.property_id
           JOIN agents a ON a.id = v.agent_id
           ORDER BY v.viewing_time DESC, v.id DESC"""
    ).fetchall()


def get_viewing(conn, viewing_id):
    return conn.execute(
        "SELECT * FROM viewings WHERE id = ?", (viewing_id,)
    ).fetchone()


def has_viewing(conn, customer_id):
    """该客户是否登记过任意一条带看。"""
    return conn.execute(
        "SELECT EXISTS(SELECT 1 FROM viewings WHERE customer_id = ?)", (customer_id,)
    ).fetchone()[0] == 1


def first_viewing_date(conn, customer_id):
    """该客户首次带看的日期（date 对象），没有带看则 None。"""
    row = conn.execute(
        "SELECT MIN(viewing_time) FROM viewings WHERE customer_id = ?", (customer_id,)
    ).fetchone()[0]
    return datetime.strptime(row, "%Y-%m-%d %H:%M:%S").date() if row else None


# ---------- 带看参与人（主带/协同）与按人反馈 ----------

def add_viewing_agent(conn, viewing_id, agent_id, role, now):
    cur = conn.execute(
        """INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at)
           VALUES (?, ?, ?, ?)""",
        (viewing_id, agent_id, role, now),
    )
    return cur.lastrowid


def get_participant(conn, viewing_id, agent_id):
    return conn.execute(
        "SELECT * FROM viewing_agents WHERE viewing_id = ? AND agent_id = ?",
        (viewing_id, agent_id),
    ).fetchone()


def list_all_participants(conn):
    """全部带看参与人（含各自反馈），按带看分组、主带在前。"""
    return conn.execute(
        """SELECT va.viewing_id, va.agent_id, a.name AS agent_name,
                  va.role, va.feedback, va.feedback_at
           FROM viewing_agents va
           JOIN agents a ON a.id = va.agent_id
           ORDER BY va.viewing_id, CASE va.role WHEN '主带' THEN 0 ELSE 1 END, va.id"""
    ).fetchall()


def list_viewing_participations(conn, start, end):
    """[start, end) 内带看的参与人行（含该条带看的参与人总数），供月报折算带看积分。"""
    return conn.execute(
        """SELECT va.agent_id, va.role,
                  (SELECT COUNT(*) FROM viewing_agents x WHERE x.viewing_id = va.viewing_id)
                      AS n_participants
           FROM viewing_agents va
           JOIN viewings v ON v.id = va.viewing_id
           WHERE v.viewing_time >= ? AND v.viewing_time < ?""",
        (start, end),
    ).fetchall()


def set_feedback(conn, viewing_id, agent_id, feedback, feedback_at):
    """给某位参与人补反馈；已提交过的不覆盖（rowcount = 0）。"""
    cur = conn.execute(
        """UPDATE viewing_agents SET feedback = ?, feedback_at = ?
           WHERE viewing_id = ? AND agent_id = ? AND feedback IS NULL""",
        (feedback, feedback_at, viewing_id, agent_id),
    )
    return cur.rowcount


# ---------- 成交 ----------

def add_deal(conn, customer_id, property_id, agent_id, deal_price, deal_date, operator, now):
    cur = conn.execute(
        """INSERT INTO deals (customer_id, property_id, agent_id, deal_price, deal_date, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, property_id, agent_id, deal_price, deal_date, operator, now),
    )
    return cur.lastrowid


def list_deals_in_range(conn, start, end):
    return conn.execute(
        """SELECT d.*, c.name AS customer_name, p.community, a.name AS agent_name
           FROM deals d
           JOIN customers c ON c.id = d.customer_id
           JOIN properties p ON p.id = d.property_id
           JOIN agents a ON a.id = d.agent_id
           WHERE d.deal_date >= ? AND d.deal_date < ?
           ORDER BY d.deal_date DESC, d.id DESC""",
        (start, end),
    ).fetchall()


# ---------- 统计查询 ----------

def _count(conn, sql, params):
    return conn.execute(sql, params).fetchone()[0]


def count_customers_created(conn, start, end):
    return _count(
        conn,
        "SELECT COUNT(*) FROM customers WHERE created_at >= ? AND created_at < ?",
        (start, end),
    )


def count_viewings(conn, start, end):
    return _count(
        conn,
        "SELECT COUNT(*) FROM viewings WHERE viewing_time >= ? AND viewing_time < ?",
        (start, end),
    )


def count_status_entries(conn, to_status, start, end):
    """本月内状态推进到 to_status 的次数（按状态历史的操作时间算，跨月不串）。

    「成交」不走这里：成交有独立业务日期 deals.deal_date，用 count_deals，
    保证漏斗/月报/成交列表三处成交数用同一个口径。
    """
    return _count(
        conn,
        "SELECT COUNT(*) FROM status_history WHERE to_status = ? AND created_at >= ? AND created_at < ?",
        (to_status, start, end),
    )


def count_deals(conn, start, end):
    """[start, end) 内按成交日期 deals.deal_date 统计的成交单数。"""
    return _count(
        conn,
        "SELECT COUNT(*) FROM deals WHERE deal_date >= ? AND deal_date < ?",
        (start, end),
    )


def stale_properties(conn, list_date_before, idle_since):
    """在售、挂牌日期早于 list_date_before，且最近一条带看早于 idle_since（或从未带看）。"""
    return conn.execute(
        """SELECT p.id, p.community, p.layout, p.list_price, p.list_date,
                  MAX(v.viewing_time) AS last_viewing
           FROM properties p
           LEFT JOIN viewings v ON v.property_id = p.id
           WHERE p.status = '在售' AND p.list_date <= ?
           GROUP BY p.id
           HAVING last_viewing IS NULL OR last_viewing < ?
           ORDER BY p.list_date""",
        (list_date_before, idle_since),
    ).fetchall()


def overdue_feedback_viewings(conn, deadline):
    """带看时间早于 deadline 且仍有参与人未补反馈的记录（pending_agents 列出待补人）。"""
    return conn.execute(
        """SELECT v.id, v.viewing_time, c.name AS customer_name, p.community,
                  a.name AS agent_name, v.created_by,
                  (SELECT GROUP_CONCAT(name, '、') FROM (
                      SELECT a2.name AS name
                      FROM viewing_agents va JOIN agents a2 ON a2.id = va.agent_id
                      WHERE va.viewing_id = v.id AND va.feedback IS NULL
                      ORDER BY va.id)) AS pending_agents
           FROM viewings v
           JOIN customers c ON c.id = v.customer_id
           JOIN properties p ON p.id = v.property_id
           JOIN agents a ON a.id = v.agent_id
           WHERE v.viewing_time < ?
             AND EXISTS(SELECT 1 FROM viewing_agents va
                        WHERE va.viewing_id = v.id AND va.feedback IS NULL)
           ORDER BY v.viewing_time""",
        (deadline,),
    ).fetchall()


def list_viewing_snapshots_in_range(conn, start, end):
    """[start, end) 内全部带看的价格快照（漏斗按价格区间分桶用，与带看记录页同一列）。"""
    return conn.execute(
        """SELECT list_price_snapshot FROM viewings
           WHERE viewing_time >= ? AND viewing_time < ?""",
        (start, end),
    ).fetchall()


def agent_monthly_report(conn, start, end):
    """每个经纪人在 [start, end) 内的带看组数和成交单数。

    历史月报包含已停用经纪人（否则离职人员的历史业绩会从合计里消失，
    与门店漏斗对不上）；停用经纪人排在末尾。
    """
    return conn.execute(
        """SELECT a.id, a.name,
                  (SELECT COUNT(*) FROM viewings v
                    WHERE v.agent_id = a.id AND v.viewing_time >= ? AND v.viewing_time < ?) AS viewing_count,
                  (SELECT COUNT(*) FROM deals d
                    WHERE d.agent_id = a.id AND d.deal_date >= ? AND d.deal_date < ?) AS deal_count
           FROM agents a
           ORDER BY a.active DESC, viewing_count DESC, deal_count DESC, a.id""",
        (start, end, start, end),
    ).fetchall()
