"""状态流转拦截测试：状态机规则 + 服务层真正拦得住。"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import repository as repo
import services
from state_machine import (
    STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING, STATUS_DEAL, STATUS_LOST,
    InvalidTransitionError, can_transition, next_statuses, validate_transition,
)


class TestCanTransition(unittest.TestCase):
    def test_forward_one_step_allowed(self):
        self.assertTrue(can_transition(STATUS_NEW, STATUS_VIEWING))
        self.assertTrue(can_transition(STATUS_VIEWING, STATUS_NEGOTIATING))
        self.assertTrue(can_transition(STATUS_NEGOTIATING, STATUS_DEAL))

    def test_jump_from_new_to_deal_blocked(self):
        self.assertFalse(can_transition(STATUS_NEW, STATUS_DEAL))

    def test_skip_steps_blocked(self):
        self.assertFalse(can_transition(STATUS_NEW, STATUS_NEGOTIATING))
        self.assertFalse(can_transition(STATUS_VIEWING, STATUS_DEAL))

    def test_backwards_blocked(self):
        self.assertFalse(can_transition(STATUS_VIEWING, STATUS_NEW))
        self.assertFalse(can_transition(STATUS_NEGOTIATING, STATUS_VIEWING))

    def test_same_status_blocked(self):
        for s in (STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING, STATUS_DEAL, STATUS_LOST):
            self.assertFalse(can_transition(s, s))

    def test_lost_allowed_from_any_active(self):
        for s in (STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING):
            self.assertTrue(can_transition(s, STATUS_LOST))

    def test_terminal_states_locked(self):
        for target in (STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING, STATUS_DEAL, STATUS_LOST):
            self.assertFalse(can_transition(STATUS_DEAL, target))
            self.assertFalse(can_transition(STATUS_LOST, target))

    def test_validate_raises(self):
        with self.assertRaises(InvalidTransitionError):
            validate_transition(STATUS_NEW, STATUS_DEAL)

    def test_unknown_status_raises(self):
        with self.assertRaises(InvalidTransitionError):
            validate_transition("不存在", STATUS_VIEWING)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(STATUS_NEW, "不存在")

    def test_next_statuses(self):
        self.assertEqual(next_statuses(STATUS_NEW), [STATUS_VIEWING, STATUS_LOST])
        self.assertEqual(next_statuses(STATUS_VIEWING), [STATUS_NEGOTIATING, STATUS_LOST])
        self.assertEqual(next_statuses(STATUS_NEGOTIATING), [STATUS_DEAL, STATUS_LOST])
        self.assertEqual(next_statuses(STATUS_DEAL), [])
        self.assertEqual(next_statuses(STATUS_LOST), [])


class TestServiceEnforcement(unittest.TestCase):
    """服务层必须真正拦截非法流转，而不是只在页面上藏选项。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=datetime(2026, 9, 1, 9))
        self.pid = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-09-01", "店长",
            now=datetime(2026, 9, 1, 9),
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=datetime(2026, 9, 1, 10)
        )

    def tearDown(self):
        self.conn.close()

    def test_illegal_jump_blocked_and_status_unchanged(self):
        with self.assertRaises(InvalidTransitionError):
            services.advance_customer(
                self.conn, self.cid, STATUS_DEAL, "店长", now=datetime(2026, 9, 2, 10)
            )
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_NEW)

    def test_deal_must_go_through_close_deal(self):
        services.record_viewing(
            self.conn, self.cid, self.pid, self.aid,
            datetime(2026, 9, 2, 10), "店长", now=datetime(2026, 9, 2, 10),
        )  # 登记带看后自动进入「带看」
        services.advance_customer(self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 10))
        with self.assertRaises(ValueError):
            services.advance_customer(self.conn, self.cid, STATUS_DEAL, "店长", now=datetime(2026, 9, 4, 10))

    def test_close_deal_requires_negotiating(self):
        # 新客直接成交，走 close_deal 也一样被状态机拦下
        with self.assertRaises(InvalidTransitionError):
            services.close_deal(
                self.conn, self.cid, self.pid, self.aid, 200, "2026-09-05", "店长",
                now=datetime(2026, 9, 5, 10),
            )

    def test_full_legal_path_then_locked(self):
        services.record_viewing(
            self.conn, self.cid, self.pid, self.aid,
            datetime(2026, 9, 2, 10), "店长", now=datetime(2026, 9, 2, 10),
        )
        services.advance_customer(self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 10))
        services.close_deal(
            self.conn, self.cid, self.pid, self.aid, 180, "2026-09-04", "店长",
            now=datetime(2026, 9, 4, 10),
        )
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_DEAL)
        # 成交后任何流转都被拦截
        with self.assertRaises(InvalidTransitionError):
            services.advance_customer(self.conn, self.cid, STATUS_LOST, "店长", now=datetime(2026, 9, 5, 10))

    def test_first_viewing_auto_advances_new_customer(self):
        services.record_viewing(
            self.conn, self.cid, self.pid, self.aid,
            datetime(2026, 9, 2, 15), "店长", now=datetime(2026, 9, 2, 15),
        )
        self.assertEqual(repo.get_customer(self.conn, self.cid)["status"], STATUS_VIEWING)


if __name__ == "__main__":
    unittest.main()
