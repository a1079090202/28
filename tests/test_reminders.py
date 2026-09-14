"""提醒逻辑测试：滞销房源（挂牌超 45 天且近 14 天无带看）+ 超时未补反馈。"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import services
from state_machine import STATUS_NEGOTIATING

NOW = datetime(2026, 9, 14, 12, 0, 0)  # 固定"当前时间"，测试可重复


class TestStaleProperties(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=datetime(2026, 8, 1, 9))
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=datetime(2026, 8, 1, 10)
        )

    def tearDown(self):
        self.conn.close()

    def _property(self, community, listed_days_ago):
        list_date = (NOW - timedelta(days=listed_days_ago)).strftime("%Y-%m-%d")
        return services.add_property(
            self.conn, community, "两室一厅", 200, list_date, "店长", now=NOW
        )

    def _viewing(self, pid, hours_ago):
        vt = NOW - timedelta(hours=hours_ago)
        services.record_viewing(self.conn, self.cid, pid, self.aid, vt, "店长", now=vt)

    def test_stale_rules(self):
        p1 = self._property("挂牌46天-带看在15天前", 46)
        self._viewing(p1, 15 * 24)               # 超过两周没带看 → 滞销
        p2 = self._property("挂牌46天-带看在3天前", 46)
        self._viewing(p2, 3 * 24)                # 近期有带看 → 不滞销
        self._property("挂牌30天-无带看", 30)      # 挂牌未满 45 天 → 不滞销
        p4 = self._property("挂牌60天-无带看", 60)  # 滞销
        p5 = self._property("挂牌50天-带看在13天前", 50)
        self._viewing(p5, 13 * 24)               # 两周内有带看 → 不滞销
        p6 = self._property("挂牌50天-带看在14天零2小时前", 50)
        self._viewing(p6, 14 * 24 + 2)           # 刚好超过两周 → 滞销

        stale_ids = {p["id"] for p in services.stale_properties(self.conn, now=NOW)}
        self.assertEqual(stale_ids, {p1, p4, p6})

    def test_sold_property_not_stale(self):
        pid = self._property("挂牌60天-已成交", 60)
        vt = NOW - timedelta(days=20)
        services.record_viewing(self.conn, self.cid, pid, self.aid, vt, "店长", now=vt)
        services.advance_customer(
            self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=NOW - timedelta(days=19)
        )
        services.close_deal(
            self.conn, self.cid, pid, self.aid, 195,
            (NOW - timedelta(days=18)).strftime("%Y-%m-%d"), "店长",
            now=NOW - timedelta(days=18),
        )
        stale_ids = {p["id"] for p in services.stale_properties(self.conn, now=NOW)}
        self.assertNotIn(pid, stale_ids)


class TestOverdueFeedback(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=datetime(2026, 9, 1, 9))
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 200, "2026-09-01", "店长",
            now=datetime(2026, 9, 1, 9),
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=datetime(2026, 9, 1, 10)
        )

    def tearDown(self):
        self.conn.close()

    def _viewing(self, hours_ago, feedback=None):
        vt = NOW - timedelta(hours=hours_ago)
        vid = services.record_viewing(self.conn, self.cid, self.pid, self.aid, vt, "店长", now=vt)
        if feedback:
            services.submit_feedback(self.conn, vid, feedback, now=vt + timedelta(hours=2))
        return vid

    def test_overdue_rules(self):
        v1 = self._viewing(25)                        # 超 24 小时未反馈 → 超时
        self._viewing(23)                             # 未超 24 小时 → 不超时
        self._viewing(30, feedback="满意，再考虑")     # 已反馈 → 不超时
        overdue_ids = {v["id"] for v in services.overdue_feedback(self.conn, now=NOW)}
        self.assertEqual(overdue_ids, {v1})

    def test_feedback_submitted_after_deadline_clears_overdue(self):
        vid = self._viewing(30)
        self.assertEqual(len(services.overdue_feedback(self.conn, now=NOW)), 1)
        services.submit_feedback(self.conn, vid, "补录：客户嫌楼层低", now=NOW)
        self.assertEqual(services.overdue_feedback(self.conn, now=NOW), [])


if __name__ == "__main__":
    unittest.main()
