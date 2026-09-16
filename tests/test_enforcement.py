"""纵深防御测试：关键业务规则必须在服务层和数据库层都真正生效，
而不是只靠页面把入口藏起来。

- TestServiceValidation：绕过页面直接调 services，非法操作必须被拒绝且不留脏数据
- TestDatabaseEnforcement：绕过 services 直接写 SQL，CHECK/UNIQUE/触发器必须拦住
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import repository as repo
import services
from state_machine import (
    STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING, STATUS_DEAL, STATUS_LOST,
    InvalidTransitionError,
)

T0 = datetime(2026, 9, 1, 9, 0, 0)


class ServiceValidationTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=T0)
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-09-01", "店长", now=T0
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=T0
        )

    def tearDown(self):
        self.conn.close()

    def _to_negotiating(self, cid=None):
        cid = self.cid if cid is None else cid
        # 「带看」状态只能由登记带看触发；带看后自动进入「带看」，再推进到谈价
        services.record_viewing(
            self.conn, cid, self.pid, self.aid,
            datetime(2026, 9, 2, 9), "店长", now=datetime(2026, 9, 2, 9),
        )
        services.advance_customer(self.conn, cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 9))

    def assert_rollback_clean(self, table):
        """服务层抛错后不得留下脏数据。"""
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestServiceValidation(ServiceValidationTestBase):
    """绕过页面直接调服务层：每条规则都要挡得住。"""

    def test_viewing_blocked_for_terminal_customers(self):
        services.advance_customer(self.conn, self.cid, STATUS_LOST, "店长", now=datetime(2026, 9, 2, 9))
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.aid, datetime(2026, 9, 3), "店长"
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM viewings").fetchone()[0], 0)

        # 成交客户同样不能登记带看
        c2 = services.register_customer(self.conn, "王女士", "", self.aid, "店长", now=T0)
        p2 = services.add_property(self.conn, "翠湖天地", "三室两厅", 400, "2026-09-01", "店长", now=T0)
        self._to_negotiating(c2)
        services.close_deal(self.conn, c2, p2, self.aid, 395, "2026-09-05", "店长", now=datetime(2026, 9, 5))
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, c2, p2, self.aid, datetime(2026, 9, 6), "店长"
            )

    def test_viewing_blocked_for_sold_property(self):
        self._to_negotiating()
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长", now=datetime(2026, 9, 4))
        c2 = services.register_customer(self.conn, "王女士", "", self.aid, "店长", now=T0)
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, c2, self.pid, self.aid, datetime(2026, 9, 5), "店长"
            )

    def test_property_cannot_be_sold_twice(self):
        c2 = services.register_customer(self.conn, "王女士", "", self.aid, "店长", now=T0)
        self._to_negotiating()
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长", now=datetime(2026, 9, 4))

        # 第二个客户走完全部合法流程，成交同一套房仍必须被拒
        p_other = services.add_property(self.conn, "别的小区", "两室一厅", 200, "2026-09-01", "店长", now=T0)
        services.record_viewing(self.conn, c2, p_other, self.aid, datetime(2026, 9, 2), "店长")
        services.advance_customer(self.conn, c2, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3))
        with self.assertRaises(services.ValidationError):
            services.close_deal(self.conn, c2, self.pid, self.aid, 181, "2026-09-05", "店长", now=datetime(2026, 9, 5))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM deals WHERE property_id=?", (self.pid,)).fetchone()[0], 1
        )
        self.assertEqual(repo.get_customer(self.conn, c2)["status"], STATUS_NEGOTIATING)

    def test_customer_cannot_have_two_deals(self):
        p2 = services.add_property(self.conn, "翠湖天地", "三室两厅", 400, "2026-09-01", "店长", now=T0)
        self._to_negotiating()
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长", now=datetime(2026, 9, 4))
        # 服务层：成交是终态，状态机直接拒绝第二次成交
        with self.assertRaises(InvalidTransitionError):
            services.close_deal(self.conn, self.cid, p2, self.aid, 190, "2026-09-05", "店长", now=datetime(2026, 9, 5))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM deals WHERE customer_id=?", (self.cid,)).fetchone()[0], 1
        )

    def test_deal_price_must_be_positive(self):
        self._to_negotiating()
        for bad in (0, -10):
            with self.assertRaises(services.ValidationError):
                services.close_deal(
                    self.conn, self.cid, self.pid, self.aid, bad, "2026-09-04", "店长", now=datetime(2026, 9, 4)
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 0)

    def test_list_price_must_be_positive(self):
        with self.assertRaises(services.ValidationError):
            services.add_property(self.conn, "测试小区", "两室一厅", 0, "2026-09-01", "店长")
        with self.assertRaises(services.ValidationError):
            services.add_property(self.conn, "测试小区", "两室一厅", -5, "2026-09-01", "店长")

    def test_required_text_fields(self):
        with self.assertRaises(services.ValidationError):
            services.add_property(self.conn, "   ", "两室一厅", 100, "2026-09-01", "店长")
        with self.assertRaises(services.ValidationError):
            services.add_property(self.conn, "小区", "两室一厅", 100, "2026-09-01", "  ")
        with self.assertRaises(services.ValidationError):
            services.register_customer(self.conn, "", "", self.aid, "店长")
        with self.assertRaises(services.ValidationError):
            services.register_customer(self.conn, "钱女士", "", self.aid, "   ")
        with self.assertRaises(services.ValidationError):
            services.advance_customer(self.conn, self.cid, STATUS_VIEWING, "  ")

    def test_layout_must_be_known(self):
        with self.assertRaises(services.ValidationError):
            services.add_property(self.conn, "小区", "城堡户型", 100, "2026-09-01", "店长")

    def test_nonexistent_references(self):
        with self.assertRaises(services.ValidationError):
            services.register_customer(self.conn, "钱女士", "", 999, "店长")
        with self.assertRaises(services.ValidationError):
            services.record_viewing(self.conn, 999, self.pid, self.aid, datetime(2026, 9, 2), "店长")
        with self.assertRaises(services.ValidationError):
            services.record_viewing(self.conn, self.cid, 999, self.aid, datetime(2026, 9, 2), "店长")
        with self.assertRaises(services.ValidationError):
            services.record_viewing(self.conn, self.cid, self.pid, 999, datetime(2026, 9, 2), "店长")
        with self.assertRaises(services.ValidationError):
            services.submit_feedback(self.conn, 999, self.aid, "反馈")

    def test_feedback_must_be_nonblank_and_not_duplicated(self):
        vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.aid, datetime(2026, 9, 2, 10), "店长", now=datetime(2026, 9, 2, 10)
        )
        with self.assertRaises(services.ValidationError):
            services.submit_feedback(self.conn, vid, self.aid, "   ")
        services.submit_feedback(self.conn, vid, self.aid, "客户有意向")
        with self.assertRaises(services.ValidationError):
            services.submit_feedback(self.conn, vid, self.aid, "再补一条")

    def test_inactive_agent_rejected(self):
        self.conn.execute("UPDATE agents SET active = 0 WHERE id = ?", (self.aid,))
        self.conn.commit()
        with self.assertRaises(services.ValidationError):
            services.register_customer(self.conn, "钱女士", "", self.aid, "店长")
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.aid, datetime(2026, 9, 2), "店长"
            )

    def test_failed_deal_leaves_no_partial_writes(self):
        self._to_negotiating()
        # 成交一套不存在的房源：整个事务回滚，客户状态不能停在半截
        with self.assertRaises(services.ValidationError):
            services.close_deal(self.conn, self.cid, 999, self.aid, 180, "2026-09-04", "店长")
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_NEGOTIATING)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 0)
        latest = repo.list_status_history(self.conn, self.cid)[-1]
        self.assertEqual(latest["to_status"], STATUS_NEGOTIATING)

    def test_legal_path_still_works_end_to_end(self):
        """规则下沉不能误伤合法操作。"""
        vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.aid, datetime(2026, 9, 2, 10), "店长", now=datetime(2026, 9, 2, 10)
        )
        services.submit_feedback(self.conn, vid, self.aid, "采光好，有意向", now=datetime(2026, 9, 2, 12))
        services.advance_customer(self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3))
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 178, "2026-09-04", "店长", now=datetime(2026, 9, 4))
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_DEAL)
        self.assertEqual(repo.get_property(self.conn, self.pid)["status"], "已成交")


class TestDatabaseEnforcement(ServiceValidationTestBase):
    """绕过 services 直接写裸 SQL：CHECK / UNIQUE / 触发器是最后防线。"""

    def _raw(self, sql, params=()):
        return self.conn.execute(sql, params)

    def assert_sql_rejected(self, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            self._raw(sql, params)
            self.conn.commit()
        self.conn.rollback()

    def test_unknown_status_rejected(self):
        self.assert_sql_rejected(
            "UPDATE customers SET status = '火星人', updated_at = ? WHERE id = ?",
            ("2026-09-02 09:00:00", self.cid),
        )

    def test_illegal_transition_via_raw_sql_rejected(self):
        # 新客直接成交，跳过历史
        self.assert_sql_rejected(
            "UPDATE customers SET status = '成交', updated_at = ? WHERE id = ?",
            ("2026-09-02 09:00:00", self.cid),
        )
        # 即使伪造了历史，跳级也不允许
        self.assert_sql_rejected(
            "INSERT INTO status_history (customer_id, from_status, to_status, operator, created_at) "
            "VALUES (?, '新客', '成交', 'x', ?)",
            (self.cid, "2026-09-02 09:00:00"),
        )

    def test_status_update_without_history_rejected(self):
        # 合法流转（新客→带看）但跳过留痕：触发器拒绝
        self.assert_sql_rejected(
            "UPDATE customers SET status = '带看', updated_at = ? WHERE id = ?",
            ("2026-09-02 09:00:00", self.cid),
        )

    def test_history_from_status_must_match_current(self):
        # 当前是新客，却伪造一条 带看→谈价 的历史
        self.assert_sql_rejected(
            "INSERT INTO status_history (customer_id, from_status, to_status, operator, created_at) "
            "VALUES (?, '带看', '谈价', 'x', ?)",
            (self.cid, "2026-09-02 09:00:00"),
        )

    def test_terminal_customer_viewing_rejected(self):
        self.conn.execute(
            "INSERT INTO status_history (customer_id, from_status, to_status, operator, created_at) "
            "VALUES (?, '新客', '流失', '店长', ?)",
            (self.cid, "2026-09-02 09:00:00"),
        )
        self.conn.execute(
            "UPDATE customers SET status = '流失', updated_at = ? WHERE id = ?",
            ("2026-09-02 09:00:00", self.cid),
        )
        self.conn.commit()
        self.assert_sql_rejected(
            "INSERT INTO viewings (customer_id, property_id, agent_id, viewing_time, list_price_snapshot, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 185, 'x', ?)",
            (self.cid, self.pid, self.aid, "2026-09-03 10:00:00", "2026-09-03 10:00:00"),
        )

    def test_zero_price_deal_rejected_by_check(self):
        self._to_negotiating()
        self.conn.execute(
            "INSERT INTO status_history (customer_id, from_status, to_status, operator, created_at) "
            "VALUES (?, '谈价', '成交', '店长', ?)",
            (self.cid, "2026-09-04 09:00:00"),
        )
        self.conn.execute(
            "UPDATE customers SET status = '成交', updated_at = ? WHERE id = ?",
            ("2026-09-04 09:00:00", self.cid),
        )
        self.conn.commit()
        self.assert_sql_rejected(
            "INSERT INTO deals (customer_id, property_id, agent_id, deal_price, deal_date, created_by, created_at) "
            "VALUES (?, ?, ?, 0, '2026-09-04', 'x', ?)",
            (self.cid, self.pid, self.aid, "2026-09-04 09:00:00"),
        )

    def test_duplicate_deal_unique_index(self):
        self._to_negotiating()
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长", now=datetime(2026, 9, 4))
        # 同一客户再挂一笔成交到另一套「在售」房：
        # 触发器条件（客户=成交、房源=在售）都满足，靠 UNIQUE(customer_id) 兜底拦截
        p2 = services.add_property(self.conn, "翠湖天地", "三室两厅", 400, "2026-09-01", "店长", now=T0)
        self.assert_sql_rejected(
            "INSERT INTO deals (customer_id, property_id, agent_id, deal_price, deal_date, created_by, created_at) "
            "VALUES (?, ?, ?, 190, '2026-09-05', 'x', ?)",
            (self.cid, p2, self.aid, "2026-09-05 09:00:00"),
        )

    def test_sold_property_status_cannot_revert(self):
        self._to_negotiating()
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长", now=datetime(2026, 9, 4))
        self.assert_sql_rejected(
            "UPDATE properties SET status = '在售' WHERE id = ?", (self.pid,)
        )

    def test_blank_text_and_bad_layout_rejected_by_check(self):
        self.assert_sql_rejected(
            "INSERT INTO properties (community, layout, list_price, list_date, status, created_by, created_at) "
            "VALUES ('  ', '两室一厅', 100, '2026-09-01', '在售', '店长', ?)",
            ("2026-09-01 09:00:00",),
        )
        self.assert_sql_rejected(
            "INSERT INTO properties (community, layout, list_price, list_date, status, created_by, created_at) "
            "VALUES ('小区', '城堡户型', 100, '2026-09-01', '在售', '店长', ?)",
            ("2026-09-01 09:00:00",),
        )
        self.assert_sql_rejected(
            "INSERT INTO properties (community, layout, list_price, list_date, status, created_by, created_at) "
            "VALUES ('小区', '两室一厅', -5, '2026-09-01', '在售', '店长', ?)",
            ("2026-09-01 09:00:00",),
        )

    def test_customer_insert_must_start_at_new(self):
        self.assert_sql_rejected(
            "INSERT INTO customers (name, phone, status, agent_id, created_by, created_at, updated_at) "
            "VALUES ('钱女士', '', '谈价', ?, '店长', ?, ?)",
            (self.aid, "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
        )


if __name__ == "__main__":
    unittest.main()
