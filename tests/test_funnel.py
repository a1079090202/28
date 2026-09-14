"""漏斗统计测试：按月统计新客/带看/谈价/成交，跨月不串，月末边界不串。"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import services
from state_machine import STATUS_NEGOTIATING


class TestFunnel(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=datetime(2026, 8, 1, 9))
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-08-01", "店长",
            now=datetime(2026, 8, 1, 9),
        )

    def tearDown(self):
        self.conn.close()

    def _customer(self, name, dt):
        return services.register_customer(self.conn, name, "", self.aid, "店长", now=dt)

    def _viewing(self, cid, dt):
        return services.record_viewing(self.conn, cid, self.pid, self.aid, dt, "店长", now=dt)

    def test_funnel_counts_by_month(self):
        # 9 月新客 3 个，8 月新客 2 个
        c1 = self._customer("c1", datetime(2026, 9, 2, 10))
        c2 = self._customer("c2", datetime(2026, 9, 5, 10))
        c3 = self._customer("c3", datetime(2026, 9, 10, 10))
        c4 = self._customer("c4", datetime(2026, 8, 15, 10))
        c5 = self._customer("c5", datetime(2026, 8, 20, 10))

        # 带看：9 月 4 组，8 月 2 组
        self._viewing(c1, datetime(2026, 9, 3, 10))
        self._viewing(c1, datetime(2026, 9, 8, 10))
        self._viewing(c2, datetime(2026, 9, 11, 10))
        self._viewing(c3, datetime(2026, 9, 12, 10))
        self._viewing(c4, datetime(2026, 8, 16, 10))
        self._viewing(c5, datetime(2026, 8, 25, 10))

        # 谈价：9 月 2 组，8 月 1 组
        services.advance_customer(self.conn, c1, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 9, 10))
        services.advance_customer(self.conn, c2, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 12, 10))
        services.advance_customer(self.conn, c4, STATUS_NEGOTIATING, "店长", now=datetime(2026, 8, 17, 10))

        # 成交：9 月 1 单
        services.close_deal(self.conn, c1, self.pid, self.aid, 180, "2026-09-13", "店长",
                            now=datetime(2026, 9, 13, 10))

        self.assertEqual(
            services.funnel_stats(self.conn, 2026, 9),
            {"新客": 3, "带看": 4, "谈价": 2, "成交": 1},
        )
        self.assertEqual(
            services.funnel_stats(self.conn, 2026, 8),
            {"新客": 2, "带看": 2, "谈价": 1, "成交": 0},
        )

    def test_month_boundary(self):
        # 月末 23:59 与次月 00:00 不能串月
        self._customer("c1", datetime(2026, 9, 30, 23, 59))
        self._customer("c2", datetime(2026, 10, 1, 0, 0))
        self.assertEqual(services.funnel_stats(self.conn, 2026, 9)["新客"], 1)
        self.assertEqual(services.funnel_stats(self.conn, 2026, 10)["新客"], 1)

    def test_empty_month_is_zero(self):
        self.assertEqual(
            services.funnel_stats(self.conn, 2026, 9),
            {"新客": 0, "带看": 0, "谈价": 0, "成交": 0},
        )


class TestAgentMonthlyReport(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.a1 = services.add_agent(self.conn, "张伟", now=datetime(2026, 8, 1, 9))
        self.a2 = services.add_agent(self.conn, "李娜", now=datetime(2026, 8, 1, 9))
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-08-01", "店长",
            now=datetime(2026, 8, 1, 9),
        )

    def tearDown(self):
        self.conn.close()

    def test_report_counts_per_agent(self):
        c1 = services.register_customer(self.conn, "c1", "", self.a1, "店长", now=datetime(2026, 9, 1, 10))
        c2 = services.register_customer(self.conn, "c2", "", self.a2, "店长", now=datetime(2026, 8, 19, 10))

        # 张伟 9 月 2 组带看；李娜 9 月 1 组 + 8 月 1 组（不计入）
        services.record_viewing(self.conn, c1, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长", now=datetime(2026, 9, 2, 10))
        services.record_viewing(self.conn, c1, self.pid, self.a1, datetime(2026, 9, 5, 10), "店长", now=datetime(2026, 9, 5, 10))
        services.record_viewing(self.conn, c2, self.pid, self.a2, datetime(2026, 9, 3, 10), "店长", now=datetime(2026, 9, 3, 10))
        services.record_viewing(self.conn, c2, self.pid, self.a2, datetime(2026, 8, 20, 10), "店长", now=datetime(2026, 8, 20, 10))

        # 李娜成交 1 单
        services.advance_customer(self.conn, c2, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 4, 10))
        services.close_deal(self.conn, c2, self.pid, self.a2, 180, "2026-09-06", "店长",
                            now=datetime(2026, 9, 6, 10))

        report = {
            r["name"]: (r["viewing_count"], r["deal_count"])
            for r in services.monthly_agent_report(self.conn, 2026, 9)
        }
        self.assertEqual(report["张伟"], (2, 0))
        self.assertEqual(report["李娜"], (1, 1))


class TestReportReconciliation(unittest.TestCase):
    """同一笔数据在漏斗 / 经纪人月报 / 成交列表三处必须对得上。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.a1 = services.add_agent(self.conn, "张伟", now=datetime(2026, 8, 1, 9))
        self.a2 = services.add_agent(self.conn, "李娜", now=datetime(2026, 8, 1, 9))
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-08-01", "店长",
            now=datetime(2026, 8, 1, 9),
        )

    def tearDown(self):
        self.conn.close()

    def _deal(self, cid, pid, agent, deal_date, recorded_at):
        """登记一笔成交：业务日期 deal_date，系统录入时刻 recorded_at（可能跨月补录）。

        带看与状态推进的业务时间跟随成交日期（不晚于成交日），录入时刻用 recorded_at。
        """
        deal_dt = datetime.strptime(deal_date, "%Y-%m-%d").replace(hour=10)
        services.record_viewing(self.conn, cid, pid, agent, deal_dt, "店长", now=recorded_at)
        services.advance_customer(self.conn, cid, STATUS_NEGOTIATING, "店长", now=recorded_at)
        services.close_deal(
            self.conn, cid, pid, agent, 180, deal_date, "店长", now=recorded_at
        )

    def _assert_three_reports_agree(self, year, month, expected):
        funnel = services.funnel_stats(self.conn, year, month)["成交"]
        report_total = sum(
            r["deal_count"] for r in services.monthly_agent_report(self.conn, year, month)
        )
        deal_rows = len(services.deals_of_month(self.conn, year, month))
        self.assertEqual((funnel, report_total, deal_rows), (expected, expected, expected))

    def test_backdated_deal_counted_by_business_date_everywhere(self):
        # 9/30 成交，10/1 凌晨才录入：三处都必须算在 9 月
        c = services.register_customer(self.conn, "c1", "", self.a1, "店长", now=datetime(2026, 9, 20, 12))
        self._deal(c, self.pid, self.a1, "2026-09-30", datetime(2026, 10, 1, 0, 30))
        self._assert_three_reports_agree(2026, 9, 1)
        self._assert_three_reports_agree(2026, 10, 0)

    def test_totals_agree_across_months_and_agents(self):
        p2 = services.add_property(
            self.conn, "翠湖天地", "三室两厅", 400, "2026-08-01", "店长", now=datetime(2026, 8, 1)
        )
        c1 = services.register_customer(self.conn, "c1", "", self.a1, "店长", now=datetime(2026, 9, 2))
        c2 = services.register_customer(self.conn, "c2", "", self.a1, "店长", now=datetime(2026, 9, 5))
        self._deal(c1, self.pid, self.a1, "2026-09-10", datetime(2026, 9, 10, 12))
        self._deal(c2, p2, self.a2, "2026-09-20", datetime(2026, 9, 20, 12))
        self._assert_three_reports_agree(2026, 9, 2)
        self._assert_three_reports_agree(2026, 8, 0)

        report = {
            r["name"]: r["deal_count"]
            for r in services.monthly_agent_report(self.conn, 2026, 9)
        }
        self.assertEqual(report["张伟"], 1)
        self.assertEqual(report["李娜"], 1)

    def test_inactive_agents_history_still_in_report(self):
        c = services.register_customer(self.conn, "c1", "", self.a1, "店长", now=datetime(2026, 9, 2))
        self._deal(c, self.pid, self.a1, "2026-09-10", datetime(2026, 9, 10, 12))
        self.conn.execute("UPDATE agents SET active = 0 WHERE id = ?", (self.a1,))
        self.conn.commit()
        # 离职经纪人的历史业绩仍在月报里，合计仍与漏斗、成交列表一致
        self._assert_three_reports_agree(2026, 9, 1)
        report = services.monthly_agent_report(self.conn, 2026, 9)
        self.assertEqual({r["name"]: r["deal_count"] for r in report}, {"张伟": 1, "李娜": 0})


if __name__ == "__main__":
    unittest.main()
