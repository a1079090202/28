"""调价留痕与带看价格快照测试。

定死的规则（页面与漏斗同一口径，都读 viewings.list_price_snapshot）：
- 初始挂牌是调价史第一行；每次调价记前后价、生效日期、操作人
- 生效日期不能是未来、不能早于挂牌日、不能早于上一次调价的生效日期；
  新价必须不同于当前价；已成交房源不能调价
- 带看快照 = 登记时按「带看日期」从调价史推算的生效价（生效日当天起算新价），
  冻结后不再变；追溯调价（生效日早于登记日）不改写既有快照，只影响之后新登记的带看
- 分桶：150万以下 / 150-200万 / 200万以上，边界值归下桶（150 整归下桶、200 整归中桶）
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
from state_machine import STATUS_NEGOTIATING

T0 = datetime(2026, 9, 1, 9, 0, 0)


class PriceTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=T0)
        # 3 栋那套挂 158 万的房子，9/1 挂牌
        self.pid = services.add_property(
            self.conn, "滨江华庭3栋", "三室一厅", 158, "2026-09-01", "店长", now=T0
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=T0
        )

    def tearDown(self):
        self.conn.close()

    def _viewing(self, when, cid=None, pid=None):
        return services.record_viewing(
            self.conn, cid or self.cid, pid or self.pid, self.aid, when, "店长", now=when
        )

    def _snapshot(self, vid):
        return repo.get_viewing(self.conn, vid)["list_price_snapshot"]


class TestPriceHistory(PriceTestBase):
    def test_initial_listing_is_first_history_row(self):
        history = repo.list_price_adjustments(self.conn, self.pid)
        self.assertEqual(len(history), 1)
        row = history[0]
        self.assertIsNone(row["old_price"])          # 初始挂牌没有「调价前」
        self.assertEqual(row["new_price"], 158)
        self.assertEqual(row["effective_date"], "2026-09-01")
        self.assertEqual(row["operator"], "店长")

    def test_two_adjustments_recorded_and_list_price_synced(self):
        # 9 月降两次价：158 → 150 → 145
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        services.adjust_price(self.conn, self.pid, 145, "2026-09-15", "店长", now=datetime(2026, 9, 15, 9))

        self.assertEqual(repo.get_property(self.conn, self.pid)["list_price"], 145)
        history = repo.list_price_adjustments(self.conn, self.pid)
        self.assertEqual(len(history), 3)  # 初始挂牌 + 两次调价
        # 最新的在前
        self.assertEqual(
            [(h["old_price"], h["new_price"], h["effective_date"], h["operator"]) for h in history],
            [(150.0, 145.0, "2026-09-15", "店长"),
             (158.0, 150.0, "2026-09-10", "店长"),
             (None, 158.0, "2026-09-01", "店长")],
        )

    def test_same_day_second_adjustment_latest_wins(self):
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        services.adjust_price(self.conn, self.pid, 155, "2026-09-10", "店长", now=datetime(2026, 9, 10, 18))
        self.assertEqual(repo.price_on_date(self.conn, self.pid, "2026-09-10"), 155)
        self.assertEqual(repo.get_property(self.conn, self.pid)["list_price"], 155)


class TestSnapshot(PriceTestBase):
    def test_snapshot_follows_viewing_date_across_adjustments(self):
        """验收场景①：调价两次的房源，三条跨调价日带看的快照分别是 158/150/145。"""
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        services.adjust_price(self.conn, self.pid, 145, "2026-09-15", "店长", now=datetime(2026, 9, 15, 9))

        v1 = self._viewing(datetime(2026, 9, 5, 10))    # 第一次降价前
        v2 = self._viewing(datetime(2026, 9, 12, 10))   # 两次降价之间
        v3 = self._viewing(datetime(2026, 9, 16, 10))   # 第二次降价后
        self.assertEqual(self._snapshot(v1), 158)
        self.assertEqual(self._snapshot(v2), 150)
        self.assertEqual(self._snapshot(v3), 145)

    def test_adjustment_day_itself_uses_new_price(self):
        """带看日期跨调价日的归期：生效日当天及之后归新价（含当天）。"""
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        v_before = self._viewing(datetime(2026, 9, 9, 23, 59))
        v_on = self._viewing(datetime(2026, 9, 10, 0, 0))
        self.assertEqual(self._snapshot(v_before), 158)
        self.assertEqual(self._snapshot(v_on), 150)

    def test_retroactive_adjustment_does_not_rewrite_frozen_snapshot(self):
        """追溯调价：生效日与调价登记日之间已登记的带看，仍按登记时冻结的旧价。

        页面与漏斗读同一快照列，两处必然一致；追溯调价只影响之后新登记的带看。
        """
        # 9/12 登记带看，当时挂牌价 158 → 快照 158
        v_old = self._viewing(datetime(2026, 9, 12, 10))
        self.assertEqual(self._snapshot(v_old), 158)

        # 9/15 房东补录「9/10 起生效」的降价：158 → 150
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 15, 9))

        # 既有带看快照不变（客户当时看到的就是 158，回访口径不打架）
        self.assertEqual(self._snapshot(v_old), 158)
        # 但 9/15 之后新登记的带看，即使带看日期还是 9/12，也按新生效价取快照
        c2 = services.register_customer(self.conn, "王女士", "", self.aid, "店长", now=datetime(2026, 9, 1, 10))
        v_new = self._viewing(datetime(2026, 9, 12, 15), cid=c2)
        self.assertEqual(self._snapshot(v_new), 150)

        # 页面（快照列）与漏斗（分桶）同口径：9 月两条带看分别落中桶、下桶
        buckets = services.funnel_viewing_price_buckets(self.conn, 2026, 9)
        self.assertEqual(buckets, {"150万以下": 1, "150-200万": 1, "200万以上": 0})
        snapshots = [repo.get_viewing(self.conn, v)["list_price_snapshot"] for v in (v_old, v_new)]
        self.assertEqual([services.price_bucket(s) for s in snapshots], ["150-200万", "150万以下"])


class TestPriceBucket(PriceTestBase):
    def test_boundary_values_go_to_lower_bucket(self):
        """边界值归下桶：150 整归「150万以下」，200 整归「150-200万」。"""
        self.assertEqual(services.price_bucket(149.99), "150万以下")
        self.assertEqual(services.price_bucket(150), "150万以下")
        self.assertEqual(services.price_bucket(150.01), "150-200万")
        self.assertEqual(services.price_bucket(200), "150-200万")
        self.assertEqual(services.price_bucket(200.01), "200万以上")

    def test_viewing_at_exactly_150_lands_in_lower_bucket(self):
        """价格刚好 150 万的带看落「150万以下」桶。"""
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        self._viewing(datetime(2026, 9, 11, 10))
        buckets = services.funnel_viewing_price_buckets(self.conn, 2026, 9)
        self.assertEqual(buckets, {"150万以下": 1, "150-200万": 0, "200万以上": 0})

    def test_buckets_sum_equals_funnel_viewing_count(self):
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        self._viewing(datetime(2026, 9, 5, 10))    # 158 → 中桶
        self._viewing(datetime(2026, 9, 12, 10))   # 150 → 下桶
        # 8 月的带看不进 9 月分桶（另备一套 8 月挂牌的房和 8 月建档的客户）
        p2 = services.add_property(
            self.conn, "阳光花园", "两室一厅", 300, "2026-08-01", "店长", now=datetime(2026, 8, 1, 9)
        )
        c2 = services.register_customer(self.conn, "王女士", "", self.aid, "店长", now=datetime(2026, 8, 2, 9))
        self._viewing(datetime(2026, 8, 20, 10), cid=c2, pid=p2)   # 300 → 上桶（8 月）
        buckets = services.funnel_viewing_price_buckets(self.conn, 2026, 9)
        self.assertEqual(sum(buckets.values()), services.funnel_stats(self.conn, 2026, 9)["带看"])
        self.assertEqual(buckets, {"150万以下": 1, "150-200万": 1, "200万以上": 0})
        self.assertEqual(
            services.funnel_viewing_price_buckets(self.conn, 2026, 8),
            {"150万以下": 0, "150-200万": 0, "200万以上": 1},
        )


class TestAdjustValidation(PriceTestBase):
    def test_future_effective_date_rejected(self):
        with self.assertRaises(services.ValidationError):
            services.adjust_price(self.conn, self.pid, 150, "2026-09-02", "店长", now=T0)

    def test_effective_before_list_date_rejected(self):
        with self.assertRaises(services.ValidationError):
            services.adjust_price(self.conn, self.pid, 150, "2026-08-31", "店长", now=T0)

    def test_effective_before_previous_adjustment_rejected(self):
        services.adjust_price(self.conn, self.pid, 150, "2026-09-10", "店长", now=datetime(2026, 9, 10, 9))
        with self.assertRaises(services.ValidationError):
            # 不能插到上一次调价（9/10 生效）之前
            services.adjust_price(self.conn, self.pid, 155, "2026-09-05", "店长", now=datetime(2026, 9, 12, 9))

    def test_same_price_rejected(self):
        with self.assertRaises(services.ValidationError):
            services.adjust_price(self.conn, self.pid, 158, "2026-09-02", "店长", now=T0)

    def test_bad_price_values_rejected(self):
        for bad in (0, -5, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                services.adjust_price(self.conn, self.pid, bad, "2026-09-02", "店长", now=T0)

    def test_sold_property_cannot_adjust(self):
        self._viewing(datetime(2026, 9, 2, 10))
        services.advance_customer(self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 9))
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 155, "2026-09-04", "店长",
                            now=datetime(2026, 9, 4, 9))
        with self.assertRaises(services.ValidationError):
            services.adjust_price(self.conn, self.pid, 150, "2026-09-05", "店长", now=datetime(2026, 9, 5, 9))
        # 拒绝后不留调价记录
        self.assertEqual(len(repo.list_price_adjustments(self.conn, self.pid)), 1)

    def test_nonexistent_property_rejected(self):
        with self.assertRaises(services.ValidationError):
            services.adjust_price(self.conn, 999, 150, "2026-09-02", "店长", now=T0)


class TestDatabaseEnforcement(PriceTestBase):
    """绕过 services 直接写裸 SQL：调价链、挂牌价同步、快照正确性由触发器兜底。"""

    def assert_sql_rejected(self, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, params)
            self.conn.commit()
        self.conn.rollback()

    def _raw_adjust(self, old, new, eff, created="2026-09-10 09:00:00"):
        self.conn.execute(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, ?, ?, ?, '店长', ?)",
            (self.pid, old, new, eff, created),
        )

    def test_wrong_old_price_breaks_chain(self):
        self.assert_sql_rejected(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, 999, 150, '2026-09-10', '店长', '2026-09-10 09:00:00')",
            (self.pid,),
        )

    def test_second_initial_row_rejected(self):
        self.assert_sql_rejected(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, NULL, 150, '2026-09-10', '店长', '2026-09-10 09:00:00')",
            (self.pid,),
        )

    def test_adjust_before_list_date_rejected(self):
        self.assert_sql_rejected(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, 158, 150, '2026-08-31', '店长', '2026-09-01 09:00:00')",
            (self.pid,),
        )

    def test_future_effective_date_rejected_by_trigger(self):
        self.assert_sql_rejected(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, 158, 150, '2026-09-11', '店长', '2026-09-10 09:00:00')",
            (self.pid,),
        )

    def test_raw_adjustment_syncs_list_price_via_trigger(self):
        self._raw_adjust(158, 150, "2026-09-10")
        self.conn.commit()
        self.assertEqual(repo.get_property(self.conn, self.pid)["list_price"], 150)

    def test_direct_list_price_update_blocked(self):
        self.assert_sql_rejected(
            "UPDATE properties SET list_price = 100 WHERE id = ?", (self.pid,)
        )
        # 改成「与最新生效价相同」不算绕过（幂等），但改成别的值必须拦下
        self.assert_sql_rejected(
            "UPDATE properties SET list_price = 150 WHERE id = ?", (self.pid,)
        )

    def test_viewing_with_wrong_snapshot_rejected(self):
        self._raw_adjust(158, 150, "2026-09-10")
        self.conn.commit()
        # 9/12 的带看，生效价是 150，快照写 158 必须被拦
        self.assert_sql_rejected(
            "INSERT INTO viewings (customer_id, property_id, agent_id, viewing_time, list_price_snapshot, created_by, created_at) "
            "VALUES (?, ?, ?, '2026-09-12 10:00:00', 158, '店长', '2026-09-12 10:00:00')",
            (self.cid, self.pid, self.aid),
        )

    def test_sold_property_adjustment_rejected_by_trigger(self):
        self._viewing(datetime(2026, 9, 2, 10))
        services.advance_customer(self.conn, self.cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 9))
        services.close_deal(self.conn, self.cid, self.pid, self.aid, 155, "2026-09-04", "店长",
                            now=datetime(2026, 9, 4, 9))
        self.assert_sql_rejected(
            "INSERT INTO price_adjustments (property_id, old_price, new_price, effective_date, operator, created_at) "
            "VALUES (?, 158, 150, '2026-09-05', '店长', '2026-09-05 09:00:00')",
            (self.pid,),
        )


if __name__ == "__main__":
    unittest.main()
