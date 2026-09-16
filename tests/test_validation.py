"""异常输入校验测试：非法值必须在服务层就被友好拒绝，数据库层 CHECK/触发器兜底。

覆盖：NaN/Inf、长度上限、类型错误、真实日历、未来时间、业务时间线一致性。
"""
import math
import os
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import services
from db import MAX_FEEDBACK
from state_machine import STATUS_NEGOTIATING

T0 = datetime(2026, 9, 1, 9, 0, 0)


class ValidationTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=T0)
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-08-01", "店长", now=T0
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=datetime(2026, 9, 1, 10)
        )

    def tearDown(self):
        self.conn.close()

    def _viewing(self, when=None):
        when = when or datetime(2026, 9, 2, 10)
        return services.record_viewing(
            self.conn, self.cid, self.pid, self.aid, when, "店长", now=when
        )

    def _to_negotiating(self):
        self._viewing()
        services.advance_customer(
            self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 10)
        )


class TestServiceValueValidation(ValidationTestBase):
    """服务层：异常值得到 ValidationError（ValueError 子类），不能抛底层 AttributeError。"""

    def test_nan_and_inf_prices_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                services.add_property(self.conn, "小区", "两室一厅", bad, "2026-09-01", "店长")
        self._to_negotiating()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                services.close_deal(
                    self.conn, self.cid, self.pid, self.aid, bad, "2026-09-04", "店长"
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 0)

    def test_zero_and_negative_rejected(self):
        for bad in (0, -1, -0.01):
            with self.assertRaises(ValueError):
                services.add_property(self.conn, "小区", "两室一厅", bad, "2026-09-01", "店长")

    def test_bool_is_not_a_price(self):
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "小区", "两室一厅", True, "2026-09-01", "店长")

    def test_text_fields_reject_non_string(self):
        with self.assertRaises(ValueError):
            services.add_agent(self.conn, 123)
        with self.assertRaises(ValueError):
            services.register_customer(self.conn, "钱女士", 13800000000, self.aid, "店长")
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "小区", "两室一厅", 100, "2026-09-01", None)

    def test_text_length_limits(self):
        with self.assertRaises(ValueError):
            services.add_agent(self.conn, "张" * 51)
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "花" * 101, "两室一厅", 100, "2026-09-01", "店长")
        vid = self._viewing()
        with self.assertRaises(ValueError):
            services.submit_feedback(self.conn, vid, self.aid, "好" * (MAX_FEEDBACK + 1))
        # 合法长度可以通过
        services.submit_feedback(self.conn, vid, self.aid, "好" * MAX_FEEDBACK)

    def test_real_calendar_dates(self):
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "小区", "两室一厅", 100, "2026-02-31", "店长")
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "小区", "两室一厅", 100, "2026-13-01", "店长")
        with self.assertRaises(ValueError):
            services.add_property(self.conn, "小区", "两室一厅", 100, "9/1/2026", "店长")

    def test_future_list_date_rejected(self):
        # now 固定为 T0=9/1，挂牌 9/2 是未来
        with self.assertRaises(ValueError):
            services.add_property(
                self.conn, "小区", "两室一厅", 100, "2026-09-02", "店长", now=T0
            )

    def test_bad_now_param_type(self):
        with self.assertRaises(ValueError):
            services.add_agent(self.conn, "李娜", now="昨天下午")
        with self.assertRaises(ValueError):
            services.add_agent(self.conn, "李娜", now=12345)

    def test_future_viewing_time_rejected(self):
        with self.assertRaises(ValueError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.aid,
                datetime(2026, 9, 2, 11), "店长", now=datetime(2026, 9, 2, 10),
            )

    def test_viewing_before_customer_created_rejected(self):
        with self.assertRaises(ValueError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.aid,
                datetime(2026, 9, 1, 9), "店长", now=datetime(2026, 9, 1, 11),
            )

    def test_viewing_before_property_listed_rejected(self):
        # 房源 9/3 才挂牌，带看却登记在 9/2（晚于客户建档 9/1，但仍非法）
        pid = services.add_property(
            self.conn, "新盘", "两室一厅", 200, "2026-09-03", "店长", now=datetime(2026, 9, 5)
        )
        with self.assertRaises(ValueError):
            services.record_viewing(
                self.conn, self.cid, pid, self.aid,
                datetime(2026, 9, 2, 12), "店长", now=datetime(2026, 9, 6),
            )

    def test_advance_to_viewing_without_viewing_rejected(self):
        # 没有任何带看记录，不能空推进到「带看」
        with self.assertRaises(ValueError):
            services.advance_customer(self.conn, self.cid, "带看", "店长", now=datetime(2026, 9, 2))
        self.assertEqual(
            self.conn.execute("SELECT status FROM customers WHERE id=?", (self.cid,)).fetchone()[0],
            "新客",
        )

    def test_deal_date_not_before_listing(self):
        self._to_negotiating()
        with self.assertRaises(ValueError):
            services.close_deal(
                self.conn, self.cid, self.pid, self.aid, 180, "2026-07-01", "店长", now=datetime(2026, 9, 4)
            )

    def test_deal_date_not_before_first_viewing(self):
        self._to_negotiating()  # 首次带看 9/2
        with self.assertRaises(ValueError):
            services.close_deal(
                self.conn, self.cid, self.pid, self.aid, 180, "2026-09-01", "店长", now=datetime(2026, 9, 4)
            )

    def test_deal_date_not_in_future_relative_to_entry(self):
        self._to_negotiating()
        # 9/4 录入，成交日期填 9/10（未来）
        with self.assertRaises(ValueError):
            services.close_deal(
                self.conn, self.cid, self.pid, self.aid, 180, "2026-09-10", "店长", now=datetime(2026, 9, 4)
            )

    def test_backdated_entry_with_consistent_timeline_allowed(self):
        # 10/1 补录 9/30 成交：各业务时间自洽，应被允许
        self._to_negotiating()
        services.close_deal(
            self.conn, self.cid, self.pid, self.aid, 180, "2026-09-30", "店长",
            now=datetime(2026, 10, 1, 0, 30),
        )
        self.assertEqual(
            self.conn.execute("SELECT deal_date FROM deals").fetchone()[0], "2026-09-30"
        )

    def test_non_integer_id_rejected(self):
        with self.assertRaises(ValueError):
            services.record_viewing(
                self.conn, "1", self.pid, self.aid, datetime(2026, 9, 2), "店长"
            )
        with self.assertRaises(ValueError):
            services.submit_feedback(self.conn, True, self.aid, "反馈")


class TestDatabaseChecks(ValidationTestBase):
    """绕过 services 写裸 SQL：CHECK 与触发器是最后防线。"""

    def raw_rejected(self, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, params)
            self.conn.commit()
        self.conn.rollback()

    def test_calendar_functions_reject_fake_dates(self):
        self.raw_rejected(
            "INSERT INTO properties(community,layout,list_price,list_date,created_by,created_at) "
            "VALUES('小区','两室一厅',100,'2026-02-31','店长','2026-09-01 09:00:00')"
        )
        self.raw_rejected(
            "INSERT INTO agents(name,created_at) VALUES('李娜','2026-09-01 99:99:99')"
        )

    def test_nan_inf_rejected_at_storage(self):
        # 直接绑定浮点 NaN/Inf（Python 绑定层会把 NaN 转 NULL，同样撞约束）
        for bad in (float("inf"), float("-inf")):
            self.raw_rejected(
                "INSERT INTO properties(community,layout,list_price,list_date,created_by,created_at) "
                "VALUES('小区','两室一厅',?,'2026-09-01','店长','2026-09-01 09:00:00')",
                (bad,),
            )

    def test_length_limits_at_storage(self):
        self.raw_rejected(
            "INSERT INTO agents(name,created_at) VALUES(?,'2026-09-01 09:00:00')",
            ("张" * 51,),
        )
        self.raw_rejected(
            "INSERT INTO properties(community,layout,list_price,list_date,created_by,created_at) "
            "VALUES(?,'两室一厅',100,'2026-09-01','店长','2026-09-01 09:00:00')",
            ("花" * 101,),
        )

    def test_viewing_before_listing_trigger(self):
        self.raw_rejected(
            "INSERT INTO viewings(customer_id,property_id,agent_id,viewing_time,list_price_snapshot,created_by,created_at) "
            "VALUES(?,?,?,'2026-07-01 10:00:00',185,'店长','2026-09-02 10:00:00')",
            (self.cid, self.pid, self.aid),
        )

    def test_viewing_later_than_entry_trigger(self):
        self.raw_rejected(
            "INSERT INTO viewings(customer_id,property_id,agent_id,viewing_time,list_price_snapshot,created_by,created_at) "
            "VALUES(?,?,?,'2026-09-02 11:00:00',185,'店长','2026-09-02 10:00:00')",
            (self.cid, self.pid, self.aid),
        )

    def test_deal_before_first_viewing_trigger(self):
        self._to_negotiating()
        # 手工把客户推到成交，再插一张早于首次带看的成交单：触发器拒绝
        self.conn.execute(
            "INSERT INTO status_history(customer_id,from_status,to_status,operator,created_at) "
            "VALUES(?,'谈价','成交','店长','2026-09-04 10:00:00')", (self.cid,))
        self.conn.execute(
            "UPDATE customers SET status='成交',updated_at='2026-09-04 10:00:00' WHERE id=?", (self.cid,))
        self.conn.commit()
        self.raw_rejected(
            "INSERT INTO deals(customer_id,property_id,agent_id,deal_price,deal_date,created_by,created_at) "
            "VALUES(?,?,?,180,'2026-09-01','店长','2026-09-04 10:00:00')",
            (self.cid, self.pid, self.aid),
        )

    def test_viewing_status_requires_viewing_trigger(self):
        # 伪造一条 新客→带看 的历史但库里没有带看记录：AFTER 触发器回滚整条
        self.raw_rejected(
            "INSERT INTO status_history(customer_id,from_status,to_status,operator,created_at) "
            "VALUES(?,'新客','带看','店长','2026-09-02 10:00:00')",
            (self.cid,),
        )


if __name__ == "__main__":
    unittest.main()
