"""数据库连接与建表。只负责连接和 schema，不含业务 SQL。

本层是业务规则的最后一道防线：即使有人绕过 services 直接写 SQL，
CHECK 约束、唯一索引和触发器也要把非法数据挡在库外。
规则的单一事实来源仍是 state_machine.py，这里的触发器是同一条规则的冗余落地。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "store.db")

SCHEMA_VERSION = 3

# 多人并发：拿不到写锁时最多等这么久，再久才报"系统繁忙"
BUSY_TIMEOUT_MS = 5000

# 户型枚举（页面下拉框直接复用，避免两处维护）
LAYOUT_VALUES = (
    "一室一厅", "两室一厅", "两室两厅", "三室一厅", "三室两厅",
    "四室两厅", "五室及以上", "其他",
)

# 文本长度上限（防止异常超长输入入库；单位：字符）
MAX_NAME = 50         # 经纪人 / 客户姓名、操作人
MAX_PHONE = 30
MAX_COMMUNITY = 100   # 小区名
MAX_LAYOUT = 20
MAX_FEEDBACK = 2000
MAX_PRICE = 1e12      # 价格上限（万），把 Inf 挡在门外；NaN 靠 x = x 自不等拦截

# 状态枚举（与 state_machine.py 保持一致）
_S = ("新客", "带看", "谈价", "成交", "流失")
_FLOW_CHECK = """
        (OLD.status='新客' AND NEW.status IN ('带看','流失'))
        OR (OLD.status='带看' AND NEW.status IN ('谈价','流失'))
        OR (OLD.status='谈价' AND NEW.status IN ('成交','流失'))"""


# 真实日历校验：纯格式 CHECK（GLOB）会放过 2026-02-31、99:99:99，
# 这两个函数交给 Python 的 strptime 按真实日历判断，注册为 SQL 函数后可在 CHECK 中使用。
def _is_valid_date(s):
    if not isinstance(s, str):
        return 0
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return 1
    except ValueError:
        return 0


def _is_valid_ts(s):
    if not isinstance(s, str):
        return 0
    try:
        datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return 1
    except ValueError:
        return 0


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
        CHECK(length(trim(name)) BETWEEN 1 AND {MAX_NAME}),
    phone TEXT NOT NULL DEFAULT '' CHECK(length(phone) <= {MAX_PHONE}),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1)
);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community TEXT NOT NULL CHECK(length(trim(community)) BETWEEN 1 AND {MAX_COMMUNITY}),  -- 小区
    layout TEXT NOT NULL CHECK(layout IN {LAYOUT_VALUES!r}),     -- 户型
    list_price REAL NOT NULL
        CHECK(list_price = list_price AND list_price > 0 AND list_price <= {MAX_PRICE}),  -- 挂牌价（万）
    list_date TEXT NOT NULL CHECK(is_valid_date(list_date) = 1),    -- 真实日历日期
    status TEXT NOT NULL DEFAULT '在售' CHECK(status IN ('在售', '已成交')),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND {MAX_NAME}),
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1)
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND {MAX_NAME}),
    phone TEXT NOT NULL DEFAULT '' CHECK(length(phone) <= {MAX_PHONE}),
    status TEXT NOT NULL DEFAULT '新客' CHECK(status IN {_S!r}),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND {MAX_NAME}),
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1),
    updated_at TEXT NOT NULL CHECK(is_valid_ts(updated_at) = 1)
);

CREATE TABLE IF NOT EXISTS viewings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    viewing_time TEXT NOT NULL CHECK(is_valid_ts(viewing_time) = 1),  -- 带看时间，本地时间
    feedback TEXT CHECK(feedback IS NULL OR length(trim(feedback)) BETWEEN 1 AND {MAX_FEEDBACK}),
    feedback_at TEXT CHECK(feedback_at IS NULL OR is_valid_ts(feedback_at) = 1),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND {MAX_NAME}),
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1)
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    deal_price REAL NOT NULL
        CHECK(deal_price = deal_price AND deal_price > 0 AND deal_price <= {MAX_PRICE}),  -- 成交价（万）
    deal_date TEXT NOT NULL CHECK(is_valid_date(deal_date) = 1),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND {MAX_NAME}),
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1)
);

-- 一个客户至多一笔成交、一套房源至多卖一次
CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_customer ON deals(customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_property ON deals(property_id);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    from_status TEXT CHECK(from_status IS NULL OR from_status IN {_S!r}),
    to_status TEXT NOT NULL CHECK(to_status IN {_S!r}),
    operator TEXT NOT NULL CHECK(length(trim(operator)) BETWEEN 1 AND {MAX_NAME}),  -- 操作人
    created_at TEXT NOT NULL CHECK(is_valid_ts(created_at) = 1)
);

CREATE INDEX IF NOT EXISTS idx_viewings_time ON viewings(viewing_time);
CREATE INDEX IF NOT EXISTS idx_viewings_property ON viewings(property_id);
CREATE INDEX IF NOT EXISTS idx_history_customer ON status_history(customer_id);
CREATE INDEX IF NOT EXISTS idx_history_to ON status_history(to_status, created_at);

-- 新客户入库时状态必须是「新客」，不能凭空造一个谈价/成交客户
CREATE TRIGGER IF NOT EXISTS trg_customer_validate_insert
BEFORE INSERT ON customers
BEGIN
    SELECT CASE WHEN NEW.status <> '新客'
        THEN RAISE(ABORT, '新客户初始状态必须是「新客」')
    END;
END;

-- 房源入库必须是「在售」，已成交只能经由成交流程到达
CREATE TRIGGER IF NOT EXISTS trg_property_validate_insert
BEFORE INSERT ON properties
BEGIN
    SELECT CASE WHEN NEW.status <> '在售'
        THEN RAISE(ABORT, '新录入房源状态必须是「在售」')
    END;
END;

-- 状态历史本身必须合法：只能 新客→带看→谈价→成交 逐级推进或转流失，
-- 且历史声称的原状态必须等于客户当前状态（防伪造/预写历史）。
-- 写入顺序约定：先插 history，再 UPDATE customers.status。
CREATE TRIGGER IF NOT EXISTS trg_history_validate_insert
BEFORE INSERT ON status_history
BEGIN
    SELECT CASE
        WHEN NEW.from_status IS NULL AND (
                NEW.to_status <> '新客'
                OR (SELECT status FROM customers WHERE id = NEW.customer_id) <> '新客')
            THEN RAISE(ABORT, '初始状态历史只能登记为「新客」')
        WHEN NEW.from_status IS NOT NULL AND NOT (
                (NEW.from_status='新客' AND NEW.to_status IN ('带看','流失'))
                OR (NEW.from_status='带看' AND NEW.to_status IN ('谈价','流失'))
                OR (NEW.from_status='谈价' AND NEW.to_status IN ('成交','流失')))
            THEN RAISE(ABORT, '非法状态流转：只能按 新客→带看→谈价→成交 逐级推进或转为流失')
        WHEN NEW.from_status IS NOT NULL
                AND (SELECT status FROM customers WHERE id = NEW.customer_id) <> NEW.from_status
            THEN RAISE(ABORT, '状态历史的原状态与客户当前状态不一致')
    END;
END;

-- 改 customers.status 时：流转必须合法，且必须存在一条与本次变更匹配的最新历史，
-- 直接 UPDATE 改状态（跳过留痕）会被拦下。
CREATE TRIGGER IF NOT EXISTS trg_customer_validate_status_update
BEFORE UPDATE OF status ON customers
BEGIN
    SELECT CASE
        WHEN NOT ({_FLOW_CHECK})
            THEN RAISE(ABORT, '非法状态流转：只能按 新客→带看→谈价→成交 逐级推进或转为流失，终态不可再变')
        WHEN NOT EXISTS(
                SELECT 1 FROM status_history h
                WHERE h.customer_id = NEW.id
                  AND h.to_status = NEW.status
                  AND h.from_status IS OLD.status
                  AND h.id = (SELECT MAX(id) FROM status_history WHERE customer_id = NEW.id))
            THEN RAISE(ABORT, '状态变更必须先登记一条与当前状态匹配的状态历史，不能跳过留痕')
    END;
END;

-- 客户推进到「带看」必须存在该客户的带看记录（该状态只能由登记带看触发）
CREATE TRIGGER IF NOT EXISTS trg_history_viewing_requires_viewing
AFTER INSERT ON status_history
WHEN NEW.to_status = '带看'
BEGIN
    SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM viewings v
            WHERE v.customer_id = NEW.customer_id
              AND substr(v.viewing_time, 1, 10) <= substr(NEW.created_at, 1, 10))
        THEN RAISE(ABORT, '推进到「带看」前必须先登记一条该客户的带看记录')
    END;
END;

-- 带看登记的时间线与状态一致性：
-- 终态客户不能带看；只能对在售房源、由在职经纪人登记；
-- 带看时间不能晚于登记时刻、不能早于客户建档或房源挂牌。
CREATE TRIGGER IF NOT EXISTS trg_viewing_validate_insert
BEFORE INSERT ON viewings
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM customers WHERE id = NEW.customer_id) IN ('成交', '流失')
            THEN RAISE(ABORT, '成交/流失客户不能登记带看')
        WHEN (SELECT status FROM properties WHERE id = NEW.property_id) <> '在售'
            THEN RAISE(ABORT, '只能对在售房源登记带看')
        WHEN (SELECT active FROM agents WHERE id = NEW.agent_id) <> 1
            THEN RAISE(ABORT, '经纪人不存在或已停用')
        WHEN NEW.viewing_time > NEW.created_at
            THEN RAISE(ABORT, '带看时间不能晚于登记时间')
        WHEN NEW.viewing_time < (SELECT created_at FROM customers WHERE id = NEW.customer_id)
            THEN RAISE(ABORT, '带看时间不能早于客户建档时间')
        WHEN substr(NEW.viewing_time, 1, 10) < (SELECT list_date FROM properties WHERE id = NEW.property_id)
            THEN RAISE(ABORT, '带看时间不能早于房源挂牌日期')
    END;
END;

-- 成交单：客户必须已处于「成交」（先留痕+改状态），房源必须仍在售（防一房二卖），
-- 成交日期不能早于挂牌日，也不能早于该客户的首次带看。
CREATE TRIGGER IF NOT EXISTS trg_deal_validate_insert
BEFORE INSERT ON deals
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM customers WHERE id = NEW.customer_id) <> '成交'
            THEN RAISE(ABORT, '只有「成交」状态的客户才能登记成交单')
        WHEN (SELECT status FROM properties WHERE id = NEW.property_id) <> '在售'
            THEN RAISE(ABORT, '该房源不是在售状态（可能已成交），不能重复登记成交')
        WHEN (SELECT active FROM agents WHERE id = NEW.agent_id) <> 1
            THEN RAISE(ABORT, '经纪人不存在或已停用')
        WHEN NEW.deal_date < (SELECT list_date FROM properties WHERE id = NEW.property_id)
            THEN RAISE(ABORT, '成交日期不能早于房源挂牌日期')
        WHEN NOT EXISTS(
                SELECT 1 FROM viewings v
                WHERE v.customer_id = NEW.customer_id
                  AND substr(v.viewing_time, 1, 10) <= NEW.deal_date)
            THEN RAISE(ABORT, '成交日期不能早于该客户的首次带看日期')
    END;
END;

-- 房源状态只能 在售→已成交，且标记已成交时必须已有成交单；已成交不可回退
CREATE TRIGGER IF NOT EXISTS trg_property_validate_status_update
BEFORE UPDATE OF status ON properties
BEGIN
    SELECT CASE
        WHEN OLD.status = '已成交'
            THEN RAISE(ABORT, '已成交房源状态不能再变更')
        WHEN NEW.status <> '已成交'
            THEN RAISE(ABORT, '房源状态只能从「在售」变为「已成交」')
        WHEN NOT EXISTS(SELECT 1 FROM deals WHERE property_id = NEW.id)
            THEN RAISE(ABORT, '把房源标记为已成交之前必须先登记成交单')
    END;
END;
"""


class SchemaVersionError(RuntimeError):
    """旧版数据库缺少约束，需要重建。"""


def connect(db_path=DEFAULT_DB_PATH):
    # check_same_thread=False：Streamlit 脚本运行可能被调度到不同线程
    # isolation_level=None（autocommit）：事务由 services 层显式 BEGIN IMMEDIATE 控制，
    # 不能依赖驱动隐式开事务——否则多步写入会在错误的时机被别的会话连带提交。
    conn = sqlite3.connect(
        db_path, check_same_thread=False, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if db_path != ":memory:":
        # WAL：多人同时用时读不阻塞写、写不阻塞读；NORMAL 同步在 WAL 下安全且更快
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    # 注册真实日历校验 SQL 函数（每个连接一份）
    conn.create_function("is_valid_date", 1, _is_valid_date, deterministic=True)
    conn.create_function("is_valid_ts", 1, _is_valid_ts, deterministic=True)
    return conn


def init_db(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers'"
    ).fetchone()
    if version < SCHEMA_VERSION and has_tables:
        # 旧库的表是在加约束之前建的，CREATE TABLE IF NOT EXISTS 不会补 CHECK/触发器，
        # 必须重建（本工具的重置方式就是删除 store.db）。
        # 全新空库 version=0 且无表，属正常初始化，不在此列。
        raise SchemaVersionError(
            f"数据库结构版本过旧（v{version}，当前需要 v{SCHEMA_VERSION}），"
            "请停止应用后删除 store.db 再重启，样例数据会自动重建。"
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
