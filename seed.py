"""样例数据：8 个经纪人、10 套房源、14 个客户、20 条带看、2 笔成交。

所有日期相对运行当天生成，保证首次启动后当月漏斗、超时反馈、滞销提醒
开箱就能看到数据。全部通过 services 层写入，状态历史与正式使用一致。
"""
from datetime import datetime, timedelta

import repository as repo
import services

OPERATOR = "系统初始化"

AGENTS = [
    ("张伟", "13912340001"), ("李娜", "13912340002"), ("王强", "13912340003"),
    ("赵敏", "13912340004"), ("陈杰", "13912340005"), ("刘洋", "13912340006"),
    ("孙丽", "13912340007"), ("周涛", "13912340008"),
]

# (小区, 户型, 挂牌价(万), 挂牌天数前)
PROPERTIES = [
    ("阳光花园", "两室一厅", 185, 12),
    ("翠湖天地", "三室两厅", 420, 25),
    ("金域蓝湾", "两室两厅", 260, 20),
    ("保利心语", "三室一厅", 310, 60),      # 滞销：超 45 天无带看
    ("万科城市花园", "四室两厅", 550, 75),   # 滞销：带看在 37 天前
    ("恒大绿洲", "两室一厅", 168, 18),
    ("中海国际", "三室两厅", 390, 50),       # 滞销：带看在 30 天前
    ("龙湖原著", "五室及以上", 780, 5),
    ("华润悦府", "三室两厅", 445, 33),
    ("绿城桂语", "两室两厅", 230, 90),       # 滞销：从未带看
]

# (姓名, 电话, 经纪人序号, 录入天数前)
CUSTOMERS = [
    ("刘先生", "13800000001", 0, 2),
    ("王女士", "13800000002", 1, 5),
    ("张先生", "13800000003", 0, 9),
    ("陈女士", "13800000004", 2, 12),
    ("周女士", "13800000005", 3, 10),
    ("吴先生", "13800000006", 4, 6),
    ("郑女士", "13800000007", 5, 8),
    ("孙先生", "13800000008", 6, 35),
    ("钱女士", "13800000009", 7, 3),
    ("冯先生", "13800000010", 1, 40),
    ("何女士", "13800000011", 2, 1),
    ("高先生", "13800000012", 3, 7),
    ("林女士", "13800000013", 4, 11),
    ("罗先生", "13800000014", 6, 4),
]

# (客户序号, 房源序号, 经纪人序号, 几小时前, 反馈)；None = 未补反馈
VIEWINGS = [
    (1, 0, 1, 100, "客户觉得采光好，考虑中"),
    (1, 5, 1, 50, "想再对比一下"),
    (2, 1, 0, 190, "预算够，想看下楼层"),
    (2, 8, 0, 150, "倾向华润悦府，准备谈价"),
    (3, 2, 2, 260, "满意，回去商量"),
    (3, 2, 2, 240, "复看，当场表示要谈价"),
    (4, 1, 3, 220, "看中翠湖天地"),
    (5, 5, 4, 120, "价格略超预算"),
    (5, 0, 4, 70, None),                    # 超时未反馈
    (6, 8, 5, 170, "对装修满意"),
    (6, 1, 5, 130, None),                   # 超时未反馈
    (7, 6, 6, 720, "看完没再联系"),
    (9, 4, 1, 900, "超预算太多"),
    (11, 7, 3, 100, "总价高，犹豫"),
    (11, 2, 3, 55, None),                   # 超时未反馈
    (12, 8, 4, 235, "华润悦府有意向"),
    (12, 1, 4, 100, "二看，准备约房东谈"),
    (2, 1, 0, 5, None),                     # 刚带看，未到 24 小时
    (11, 5, 3, 30, None),                   # 超时未反馈
    (12, 0, 4, 8, None),                    # 刚带看，未到 24 小时
]

# 状态推进计划：(客户序号, 目标状态, 几小时前, 成交信息(房源序号, 成交价万) 或 None)
STATUS_PLAN = [
    (2, "谈价", 48, None),
    (3, "谈价", 200, None),
    (3, "成交", 100, (2, 255)),             # 陈女士 成交金域蓝湾 255 万
    (4, "谈价", 180, None),
    (4, "成交", 96, (1, 405)),              # 周女士 成交翠湖天地 405 万
    (6, "谈价", 100, None),
    (12, "谈价", 60, None),
    (7, "流失", 600, None),
    (9, "流失", 800, None),
]


def ensure_seed(conn):
    """数据库为空时写入样例数据；已有数据则跳过。返回是否执行了写入。

    多人同时首次打开应用时，每个浏览器会话有独立连接，可能并发进入本函数：
    第一个写入的经纪人一旦提交，后来者就会看到 agents 非空而直接返回；
    若双方在首条写入前同时进入，唯一约束会让一方失败——此时重查一次，
    确认是别人在灌库就安静跳过，而不是把启动报错抛给用户。
    """
    if repo.list_agents(conn):
        return False
    base = datetime.now().replace(microsecond=0)
    try:
        agent_ids = [services.add_agent(conn, name, phone, now=base) for name, phone in AGENTS]

        prop_ids = []
        for community, layout, price, days in PROPERTIES:
            list_date = (base - timedelta(days=days)).strftime("%Y-%m-%d")
            prop_ids.append(
                services.add_property(conn, community, layout, price, list_date, OPERATOR, now=base)
            )

        cust_ids = []
        for name, phone, ai, days in CUSTOMERS:
            cust_ids.append(
                services.register_customer(
                    conn, name, phone, agent_ids[ai], OPERATOR, now=base - timedelta(days=days)
                )
            )

        # 按时间从旧到新登记带看，状态自动推进的时间线才合理
        for ci, pi, ai, hours, feedback in sorted(VIEWINGS, key=lambda v: -v[3]):
            vt = base - timedelta(hours=hours)
            vid = services.record_viewing(
                conn, cust_ids[ci], prop_ids[pi], agent_ids[ai], vt, OPERATOR, now=vt
            )
            if feedback:
                services.submit_feedback(conn, vid, feedback, now=vt + timedelta(hours=3))

        for ci, target, hours, deal in sorted(STATUS_PLAN, key=lambda x: -x[2]):
            when = base - timedelta(hours=hours)
            if target == "成交":
                pi, price = deal
                services.close_deal(
                    conn, cust_ids[ci], prop_ids[pi], agent_ids[CUSTOMERS[ci][2]],
                    price, when.strftime("%Y-%m-%d"), OPERATOR, now=when,
                )
            else:
                services.advance_customer(conn, cust_ids[ci], target, OPERATOR, now=when)
        return True
    except (services.ValidationError, services.BusyError):
        # 另一个启动者正在/已经灌库（唯一约束冲突或写锁竞争）：
        # 丢弃本连接的半截事务，重查确认后安静跳过。
        try:
            conn.rollback()
        except Exception:
            pass
        if repo.list_agents(conn):
            return False
        raise
