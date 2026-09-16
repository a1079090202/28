"""协同带看测试：一条带看记录挂 2-3 名经纪人（1 主带 + 1-2 协同）。

定死的规则：
- 同一经纪人在一条带看中只能出现一次（主带/协同去重，登记前当场拦下）
- 一条带看最多 3 名经纪人，主带恰好 1 人
- 终态（成交/流失）客户不能登记协同带看（既有终态规则，拒绝且不留废数据）
- 新客首次带看自动转「带看」只推一次（协同带看也是一条带看记录）
- 反馈按人录入：每位参与人各录各的，全部提交才算已反馈
- 月报带看积分：单独带看 1 分；协同带看主带 0.7 分、协同平分 0.3 分
- 房源带看次数与漏斗带看数按带看记录去重：一条协同记录算一次
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
from state_machine import STATUS_LOST, STATUS_VIEWING

T0 = datetime(2026, 9, 1, 9, 0, 0)


class CollabTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.a1 = services.add_agent(self.conn, "张伟", now=T0)
        self.a2 = services.add_agent(self.conn, "李娜", now=T0)
        self.a3 = services.add_agent(self.conn, "王强", now=T0)
        self.a4 = services.add_agent(self.conn, "赵敏", now=T0)
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-09-01", "店长", now=T0
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.a1, "店长", now=T0
        )

    def tearDown(self):
        self.conn.close()

    def _counts(self):
        v = self.conn.execute("SELECT COUNT(*) FROM viewings").fetchone()[0]
        va = self.conn.execute("SELECT COUNT(*) FROM viewing_agents").fetchone()[0]
        return v, va


class TestCollabRecording(CollabTestBase):
    def test_three_agent_viewing_advances_status_exactly_once(self):
        """验收场景②：三人拼团带看一次，新客状态只推一次。"""
        vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2, self.a3],
        )
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_VIEWING)
        advances = self.conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE customer_id = ? AND to_status = '带看'",
            (self.cid,),
        ).fetchone()[0]
        self.assertEqual(advances, 1)  # 只推一次，不是每个经纪人推一次

        parts = [p for p in repo.list_all_participants(self.conn) if p["viewing_id"] == vid]
        self.assertEqual(len(parts), 3)
        self.assertEqual([p["role"] for p in parts], ["主带", "协同", "协同"])
        self.assertEqual(parts[0]["agent_id"], self.a1)

    def test_solo_viewing_still_works(self):
        vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10),
        )
        parts = [p for p in repo.list_all_participants(self.conn) if p["viewing_id"] == vid]
        self.assertEqual([(p["agent_id"], p["role"]) for p in parts], [(self.a1, "主带")])

    def test_same_agent_twice_rejected_immediately(self):
        """同一经纪人出现两次（含主带兼任协同）当场拦下，不留任何数据。"""
        for bad_assists in ([self.a1], [self.a2, self.a2], [self.a1, self.a1]):
            with self.assertRaises(services.ValidationError):
                services.record_viewing(
                    self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
                    now=datetime(2026, 9, 2, 10), assist_agent_ids=bad_assists,
                )
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], "新客")

    def test_more_than_three_agents_rejected(self):
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
                now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2, self.a3, self.a4],
            )
        self.assertEqual(self._counts(), (0, 0))

    def test_lost_customer_collab_rejected_no_junk_data(self):
        """已流失客户被拉进协同带看：按既有终态规则拒绝，不静默记废数据。"""
        services.advance_customer(self.conn, self.cid, STATUS_LOST, "店长", now=datetime(2026, 9, 2, 9))
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
                now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2],
            )
        self.assertEqual(self._counts(), (0, 0))

    def test_inactive_assist_rejected(self):
        self.conn.execute("UPDATE agents SET active = 0 WHERE id = ?", (self.a2,))
        self.conn.commit()
        with self.assertRaises(services.ValidationError):
            services.record_viewing(
                self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
                now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2],
            )
        self.assertEqual(self._counts(), (0, 0))


class TestPerAgentFeedback(CollabTestBase):
    def _collab_viewing(self):
        return services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2],
        )

    def test_each_participant_submits_own_feedback(self):
        vid = self._collab_viewing()
        services.submit_feedback(self.conn, vid, self.a1, "主带：客户有意向", now=datetime(2026, 9, 2, 12))
        services.submit_feedback(self.conn, vid, self.a2, "协同：客户问了学区", now=datetime(2026, 9, 2, 13))
        parts = [p for p in repo.list_all_participants(self.conn) if p["viewing_id"] == vid]
        self.assertEqual(
            {p["agent_id"]: p["feedback"] for p in parts},
            {self.a1: "主带：客户有意向", self.a2: "协同：客户问了学区"},
        )

    def test_duplicate_and_nonparticipant_feedback_rejected(self):
        vid = self._collab_viewing()
        services.submit_feedback(self.conn, vid, self.a1, "主带反馈", now=datetime(2026, 9, 2, 12))
        with self.assertRaises(services.ValidationError):
            services.submit_feedback(self.conn, vid, self.a1, "再补一条", now=datetime(2026, 9, 2, 13))
        with self.assertRaises(services.ValidationError):
            services.submit_feedback(self.conn, vid, self.a3, "没参与的人", now=datetime(2026, 9, 2, 13))

    def test_overdue_until_all_participants_submit(self):
        vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 1, 10), "店长",
            now=datetime(2026, 9, 1, 10), assist_agent_ids=[self.a2],
        )
        now = datetime(2026, 9, 2, 12)  # 已超过 24 小时
        overdue = services.overdue_feedback(self.conn, now=now)
        self.assertEqual([v["id"] for v in overdue], [vid])
        self.assertEqual(overdue[0]["pending_agents"], "张伟、李娜")

        # 主带补了，协同没补：仍超时，待补人只剩协同
        services.submit_feedback(self.conn, vid, self.a1, "主带反馈", now=now)
        overdue = services.overdue_feedback(self.conn, now=now)
        self.assertEqual([v["id"] for v in overdue], [vid])
        self.assertEqual(overdue[0]["pending_agents"], "李娜")

        services.submit_feedback(self.conn, vid, self.a2, "协同反馈", now=now)
        self.assertEqual(services.overdue_feedback(self.conn, now=now), [])


class TestCollabReport(CollabTestBase):
    def test_points_split_and_reconciliation(self):
        """验收场景③：月报带看积分与房源带看数跟手算一致。

        9 月：张伟单独带看 1 次（1 分）；
        张伟主带 + 李娜协同 1 次（0.7 / 0.3）；
        张伟主带 + 李娜、王强协同 1 次（0.7 / 0.15 / 0.15）。
        """
        c2 = services.register_customer(self.conn, "王女士", "", self.a2, "店长", now=T0)
        c3 = services.register_customer(self.conn, "张先生", "", self.a3, "店长", now=T0)
        services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10),
        )
        services.record_viewing(
            self.conn, c2, self.pid, self.a1, datetime(2026, 9, 3, 10), "店长",
            now=datetime(2026, 9, 3, 10), assist_agent_ids=[self.a2],
        )
        services.record_viewing(
            self.conn, c3, self.pid, self.a1, datetime(2026, 9, 4, 10), "店长",
            now=datetime(2026, 9, 4, 10), assist_agent_ids=[self.a2, self.a3],
        )

        report = {r["name"]: r for r in services.monthly_agent_report(self.conn, 2026, 9)}
        self.assertEqual(report["张伟"]["viewing_count"], 3)      # 组数按主带计
        self.assertEqual(report["李娜"]["viewing_count"], 0)
        self.assertAlmostEqual(report["张伟"]["viewing_points"], 1 + 0.7 + 0.7)
        self.assertAlmostEqual(report["李娜"]["viewing_points"], 0.3 + 0.15)
        self.assertAlmostEqual(report["王强"]["viewing_points"], 0.15)
        self.assertAlmostEqual(report["赵敏"]["viewing_points"], 0.0)

        # 对账：全员积分合计 = 全员组数合计 = 漏斗带看数 = 3（每条带看算一次）
        total_points = sum(r["viewing_points"] for r in report.values())
        total_count = sum(r["viewing_count"] for r in report.values())
        self.assertAlmostEqual(total_points, 3.0)
        self.assertEqual(total_count, 3)
        self.assertEqual(services.funnel_stats(self.conn, 2026, 9)["带看"], 3)

        # 房源带看次数去重：三人拼团的那条只算一次
        prop = [p for p in repo.list_properties(self.conn) if p["id"] == self.pid][0]
        self.assertEqual(prop["viewing_count"], 3)

    def test_property_viewing_count_deduped_for_collab(self):
        """一条三人协同记录，房源带看次数和漏斗带看数都只算一次。"""
        services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2, self.a3],
        )
        prop = [p for p in repo.list_properties(self.conn) if p["id"] == self.pid][0]
        self.assertEqual(prop["viewing_count"], 1)
        self.assertEqual(services.funnel_stats(self.conn, 2026, 9)["带看"], 1)
        report = {r["name"]: r for r in services.monthly_agent_report(self.conn, 2026, 9)}
        self.assertAlmostEqual(
            sum(r["viewing_points"] for r in report.values()), 1.0
        )

    def test_points_do_not_cross_months(self):
        # 8 月的协同带看不进 9 月积分（另备 8 月挂牌的房和 8 月建档的客户）
        p_aug = services.add_property(
            self.conn, "翠湖天地", "三室两厅", 400, "2026-08-01", "店长", now=datetime(2026, 8, 1, 9)
        )
        c_aug = services.register_customer(self.conn, "老客户", "", self.a1, "店长", now=datetime(2026, 8, 2, 9))
        services.record_viewing(
            self.conn, c_aug, p_aug, self.a1, datetime(2026, 8, 20, 10), "店长",
            now=datetime(2026, 8, 20, 10), assist_agent_ids=[self.a2],
        )
        sep = {r["name"]: r for r in services.monthly_agent_report(self.conn, 2026, 9)}
        self.assertAlmostEqual(sep["张伟"]["viewing_points"], 0.0)
        aug = {r["name"]: r for r in services.monthly_agent_report(self.conn, 2026, 8)}
        self.assertAlmostEqual(aug["张伟"]["viewing_points"], 0.7)
        self.assertAlmostEqual(aug["李娜"]["viewing_points"], 0.3)


class TestCollabDatabaseEnforcement(CollabTestBase):
    """绕过 services 直接写裸 SQL：参与人约束由数据库兜底。"""

    def setUp(self):
        super().setUp()
        self.vid = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10), assist_agent_ids=[self.a2],
        )

    def assert_sql_rejected(self, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, params)
            self.conn.commit()
        self.conn.rollback()

    def _insert_participant(self, agent_id, role):
        self.conn.execute(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) VALUES (?, ?, ?, ?)",
            (self.vid, agent_id, role, "2026-09-02 10:00:00"),
        )

    def test_duplicate_participant_rejected(self):
        self.assert_sql_rejected(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) "
            "VALUES (?, ?, '协同', '2026-09-02 10:00:00')",
            (self.vid, self.a2),
        )

    def test_second_lead_rejected(self):
        self.assert_sql_rejected(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) "
            "VALUES (?, ?, '主带', '2026-09-02 10:00:00')",
            (self.vid, self.a3),
        )

    def test_fourth_participant_rejected(self):
        self._insert_participant(self.a3, "协同")
        self.conn.commit()
        self.assert_sql_rejected(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) "
            "VALUES (?, ?, '协同', '2026-09-02 10:00:00')",
            (self.vid, self.a4),
        )

    def test_assist_before_lead_rejected(self):
        vid2 = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 3, 10), "店长",
            now=datetime(2026, 9, 3, 10),
        )
        # 直接删掉主带行再插协同：必须先有主带
        self.conn.execute(
            "DELETE FROM viewing_agents WHERE viewing_id = ? AND role = '主带'", (vid2,)
        )
        self.assert_sql_rejected(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) "
            "VALUES (?, ?, '协同', '2026-09-03 10:00:00')",
            (vid2, self.a2),
        )

    def test_lead_must_match_viewing(self):
        vid2 = services.record_viewing(
            self.conn, self.cid, self.pid, self.a1, datetime(2026, 9, 3, 10), "店长",
            now=datetime(2026, 9, 3, 10),
        )
        self.conn.execute(
            "DELETE FROM viewing_agents WHERE viewing_id = ? AND role = '主带'", (vid2,)
        )
        # 带看记录上的主带是张伟，插入李娜为主带：不一致，拒绝
        self.assert_sql_rejected(
            "INSERT INTO viewing_agents (viewing_id, agent_id, role, created_at) "
            "VALUES (?, ?, '主带', '2026-09-03 10:00:00')",
            (vid2, self.a2),
        )


if __name__ == "__main__":
    unittest.main()
