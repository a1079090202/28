"""数据访问层：全部 SQL 集中在这里，页面代码不出现任何 SQL。

约定：本层函数只执行 SQL，不 commit；写操作的提交由 services 层统一负责，
保证多步写入（如成交 = 改状态 + 写历史 + 写成交单）在一个事务里。
时间一律为本地时间字符串：日期 'YYYY-MM-DD'，时间 'YYYY-MM-DD HH:MM:SS'。
"""


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


# ---------- 房源 ----------

def add_property(conn, community, layout, list_price, list_date, operator, now):
    cur = conn.execute(
        """INSERT INTO properties (community, layout, list_price, list_date, status, created_by, created_at)
           VALUES (?, ?, ?, ?, '在售', ?, ?)""",
        (community, layout, list_price, list_date, operator, now),
    )
    return cur.lastrowid


def list_properties(conn):
    return conn.execute(
        "SELECT * FROM properties ORDER BY list_date DESC, id DESC"
    ).fetchall()


def update_property_status(conn, property_id, status):
    conn.execute("UPDATE properties SET status = ? WHERE id = ?", (status, property_id))


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

def add_viewing(conn, customer_id, property_id, agent_id, viewing_time, operator, now):
    cur = conn.execute(
        """INSERT INTO viewings (customer_id, property_id, agent_id, viewing_time, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (customer_id, property_id, agent_id, viewing_time, operator, now),
    )
    return cur.lastrowid


def list_viewings(conn):
    return conn.execute(
        """SELECT v.id, v.viewing_time, v.customer_id, v.property_id,
                  c.name AS customer_name, p.community, p.layout,
                  a.name AS agent_name, v.feedback, v.feedback_at,
                  v.created_by, v.created_at
           FROM viewings v
           JOIN customers c ON c.id = v.customer_id
           JOIN properties p ON p.id = v.property_id
           JOIN agents a ON a.id = v.agent_id
           ORDER BY v.viewing_time DESC, v.id DESC"""
    ).fetchall()


def set_feedback(conn, viewing_id, feedback, feedback_at):
    conn.execute(
        "UPDATE viewings SET feedback = ?, feedback_at = ? WHERE id = ?",
        (feedback, feedback_at, viewing_id),
    )


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
    """本月内状态推进到 to_status 的次数（按状态历史算，跨月不串）。"""
    return _count(
        conn,
        "SELECT COUNT(*) FROM status_history WHERE to_status = ? AND created_at >= ? AND created_at < ?",
        (to_status, start, end),
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
    """带看时间早于 deadline 仍未补反馈的记录。"""
    return conn.execute(
        """SELECT v.id, v.viewing_time, c.name AS customer_name, p.community,
                  a.name AS agent_name, v.created_by
           FROM viewings v
           JOIN customers c ON c.id = v.customer_id
           JOIN properties p ON p.id = v.property_id
           JOIN agents a ON a.id = v.agent_id
           WHERE v.feedback IS NULL AND v.viewing_time < ?
           ORDER BY v.viewing_time""",
        (deadline,),
    ).fetchall()


def agent_monthly_report(conn, start, end):
    """每个经纪人在 [start, end) 内的带看组数和成交单数。"""
    return conn.execute(
        """SELECT a.id, a.name,
                  (SELECT COUNT(*) FROM viewings v
                    WHERE v.agent_id = a.id AND v.viewing_time >= ? AND v.viewing_time < ?) AS viewing_count,
                  (SELECT COUNT(*) FROM deals d
                    WHERE d.agent_id = a.id AND d.deal_date >= ? AND d.deal_date < ?) AS deal_count
           FROM agents a
           WHERE a.active = 1
           ORDER BY viewing_count DESC, deal_count DESC, a.id""",
        (start, end, start, end),
    ).fetchall()
