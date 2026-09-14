"""选项构造测试：下拉选项的键必须唯一，同名/同字段记录不能互相覆盖，
选中的标签必须映射回它自己那一行（防止带看、成交记到错误对象上）。
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import services
from options import keyed_options


class FakeRow:
    """轻量替身：模拟 sqlite3.Row 的按列取值。"""
    def __init__(self, **kwargs):
        self._data = kwargs

    def __getitem__(self, key):
        return self._data[key]


class TestKeyedOptions(unittest.TestCase):
    def test_keys_include_id_and_are_unique(self):
        rows = [FakeRow(id=1, name="a"), FakeRow(id=2, name="a")]
        opts = keyed_options(rows, lambda r: r["name"])
        self.assertEqual(len(opts), 2)
        self.assertTrue(all(k.startswith("#1 ") or k.startswith("#2 ") for k in opts))

    def test_identical_business_fields_do_not_overwrite(self):
        # 同小区、同户型、同价格、连展示文本都完全一样的两套房，必须仍是两个可选项
        rows = [
            FakeRow(id=10, community="阳光花园", layout="两室一厅", list_price=185.0, list_date="2026-09-01"),
            FakeRow(id=11, community="阳光花园", layout="两室一厅", list_price=185.0, list_date="2026-09-01"),
        ]
        label = lambda p: f"{p['community']} {p['layout']}（{p['list_price']} 万）"
        opts = keyed_options(rows, label)
        self.assertEqual(len(opts), 2)
        # 去掉 id 前缀后两个标签肉眼完全相同，但映射回各自的行
        by_id = {v["id"]: k for k, v in opts.items()}
        self.assertEqual(opts[by_id[10]]["id"], 10)
        self.assertEqual(opts[by_id[11]]["id"], 11)
        self.assertNotEqual(by_id[10], by_id[11])

    def test_empty(self):
        self.assertEqual(keyed_options([], lambda r: ""), {})


class TestPropertySelectionOnRealData(unittest.TestCase):
    """端到端：在真实库里插入两套完全同描述的房源，页面的选项构造必须都能选中并写对。"""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.aid = services.add_agent(self.conn, "张伟", now=datetime(2026, 9, 1))
        self.p1 = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-09-01", "店长", now=datetime(2026, 9, 1)
        )
        self.p2 = services.add_property(
            self.conn, "阳光花园", "两室一厅", 185, "2026-09-01", "店长", now=datetime(2026, 9, 1)
        )
        self.cid = services.register_customer(
            self.conn, "刘先生", "", self.aid, "店长", now=datetime(2026, 9, 1)
        )

    def tearDown(self):
        self.conn.close()

    def test_viewing_can_target_either_identical_property(self):
        import repository as repo
        on_sale = [p for p in repo.list_properties(self.conn) if p["status"] == "在售"]
        opts = keyed_options(
            on_sale,
            lambda p: f"{p['community']} {p['layout']}（{p['list_price']} 万，挂牌 {p['list_date']}）",
        )
        self.assertEqual(len(opts), 2)

        # 模拟用户在下拉框选中第 2 套（旧代码里根本选不到，只能记到第 1 套）
        key2 = next(k for k, v in opts.items() if v["id"] == self.p2)
        chosen = opts[key2]
        vid = services.record_viewing(
            self.conn, self.cid, chosen["id"], self.aid, datetime(2026, 9, 2, 10), "店长",
            now=datetime(2026, 9, 2, 10),
        )
        viewing = repo.get_viewing(self.conn, vid)
        self.assertEqual(viewing["property_id"], self.p2)
        self.assertNotEqual(viewing["property_id"], self.p1)


if __name__ == "__main__":
    unittest.main()
