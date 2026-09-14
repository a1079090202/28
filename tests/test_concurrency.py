"""并发与运行模型测试（真实文件库 + 多线程，每个线程独立连接）。

验证：
- 多连接配置（WAL、busy_timeout、显式事务）
- 同一套房并发成交：恰好一个成功，输家得到业务错误且无半截数据
- 互不相关的并发写：各自独立提交，不被别人的 commit/rollback 串台
- 写锁等待超时 → BusyError（而不是底层 OperationalError 500）
- 多个会话同时首启灌种子：恰好灌一次，不重复、不报错
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import repository as repo
import seed
import services
from state_machine import STATUS_NEGOTIATING

T0 = datetime(2026, 9, 1, 9, 0, 0)


class ConcurrencyTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.db")

    def tearDown(self):
        self.dir.cleanup()

    def _bootstrap(self, n_properties=1, n_customers=1):
        """建一个连接灌基础数据后关闭；返回 (agent_id, [property_ids], [customer_ids])。"""
        c = db.connect(self.path)
        db.init_db(c)
        aid = services.add_agent(c, "张伟", now=T0)
        pids, cids = [], []
        for i in range(n_properties):
            pids.append(services.add_property(
                c, f"小区{i}", "两室一厅", 185 + i, "2026-08-01", "店长", now=T0))
        for i in range(n_customers):
            cid = services.register_customer(c, f"客户{i}", "", aid, "店长", now=datetime(2026, 9, 1, 10))
            services.record_viewing(
                c, cid, pids[min(i, len(pids) - 1)], aid, datetime(2026, 9, 2, 10), "店长",
                now=datetime(2026, 9, 2, 10),
            )
            services.advance_customer(
                c, cid, STATUS_NEGOTIATING, "店长", now=datetime(2026, 9, 3, 10))
            cids.append(cid)
        c.close()
        return aid, pids, cids


class TestConnectionConfig(ConcurrencyTestBase):
    def test_file_connection_uses_wal_busy_timeout_and_explicit_tx(self):
        c = db.connect(self.path)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], db.BUSY_TIMEOUT_MS)
        self.assertIsNone(c.isolation_level)  # autocommit：事务由 services 显式控制
        c.close()

    def test_memory_connection_skips_wal(self):
        c = db.connect(":memory:")
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "memory")
        c.close()


class TestConcurrentDealSameProperty(ConcurrencyTestBase):
    def test_exactly_one_winner_no_partial_state(self):
        aid, pids, cids = self._bootstrap(n_properties=1, n_customers=2)
        pid = pids[0]
        outcomes = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def worker(cid):
            c = db.connect(self.path)
            try:
                barrier.wait()
                services.close_deal(
                    c, cid, pid, aid, 180, "2026-09-05", "店长", now=datetime(2026, 9, 5, 10)
                )
                with lock:
                    outcomes.append((cid, "ok"))
            except services.ValidationError as e:
                with lock:
                    outcomes.append((cid, f"rejected:{e}"))
            except Exception as e:  # 不应出现 500 式异常
                with lock:
                    outcomes.append((cid, f"ERROR:{type(e).__name__}"))
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(cid,)) for cid in cids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(1 for _, r in outcomes if r == "ok"), 1, outcomes)
        self.assertTrue(all(not r.startswith("ERROR:") for _, r in outcomes), outcomes)

        check = db.connect(self.path)
        self.assertEqual(
            check.execute("SELECT COUNT(*) FROM deals WHERE property_id=?", (pid,)).fetchone()[0], 1
        )
        self.assertEqual(repo.get_property(check, pid)["status"], "已成交")
        # 输家停在谈价，没有半截成交历史
        loser = [cid for cid, r in outcomes if r != "ok"][0]
        self.assertEqual(repo.get_customer(check, loser)["status"], STATUS_NEGOTIATING)
        self.assertEqual(
            check.execute(
                "SELECT COUNT(*) FROM status_history WHERE customer_id=? AND to_status='成交'", (loser,)
            ).fetchone()[0],
            0,
        )
        check.close()


class TestConcurrentIndependentWrites(ConcurrencyTestBase):
    def test_parallel_deals_on_distinct_resources_all_commit(self):
        n = 6
        aid, pids, cids = self._bootstrap(n_properties=n, n_customers=n)
        errors = []
        barrier = threading.Barrier(n)
        lock = threading.Lock()

        def worker(i):
            c = db.connect(self.path)
            try:
                barrier.wait()
                services.close_deal(
                    c, cids[i], pids[i], aid, 180 + i, "2026-09-05", "店长",
                    now=datetime(2026, 9, 6 + i, 10),
                )
            except Exception as e:
                with lock:
                    errors.append(f"{type(e).__name__}: {e}")
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        check = db.connect(self.path)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM deals").fetchone()[0], n)
        self.assertEqual(
            check.execute("SELECT COUNT(*) FROM properties WHERE status='已成交'").fetchone()[0], n
        )
        check.close()


class TestLockTimeout(ConcurrencyTestBase):
    def test_write_lock_timeout_raises_busy_error(self):
        self._bootstrap()
        holder = db.connect(self.path)
        holder.execute("BEGIN IMMEDIATE")  # 一直持有写锁不提交
        try:
            waiter = db.connect(self.path)
            waiter.execute("PRAGMA busy_timeout = 0")  # 不等，立即失败
            with self.assertRaises(services.BusyError):
                services.add_agent(waiter, "李娜", now=T0)
            waiter.close()
        finally:
            holder.rollback()
            holder.close()

        # 持锁者回滚后，写入恢复正常
        c = db.connect(self.path)
        services.add_agent(c, "李娜", now=T0)
        c.close()


class TestConcurrentSeed(ConcurrencyTestBase):
    def test_first_run_seeded_exactly_once(self):
        # 真实启动顺序：init_schema 先完成一次建表，各会话随后并发 ensure_seed
        setup = db.connect(self.path)
        db.init_db(setup)
        setup.close()

        results = []
        errors = []
        n = 3
        barrier = threading.Barrier(n)
        lock = threading.Lock()

        def worker():
            c = db.connect(self.path)
            try:
                barrier.wait()
                seeded = seed.ensure_seed(c)
                with lock:
                    results.append(seeded)
            except Exception as e:
                with lock:
                    errors.append(f"{type(e).__name__}: {e}")
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in results if r), 1, results)  # 恰好一个会话灌库
        check = db.connect(self.path)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 8)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 14)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 2)
        check.close()

    def test_second_run_is_noop(self):
        c1 = db.connect(self.path)
        db.init_db(c1)
        self.assertTrue(seed.ensure_seed(c1))
        c2 = db.connect(self.path)
        db.init_db(c2)
        self.assertFalse(seed.ensure_seed(c2))
        c1.close()
        c2.close()


if __name__ == "__main__":
    unittest.main()
