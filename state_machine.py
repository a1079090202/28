"""客户跟进状态机：唯一允许的前进路径是

    新客 → 带看 → 谈价 → 成交

任意进行中的状态都可以转为「流失」；「成交」和「流失」是终态，不可再变。
不允许跳级（如新客直接成交）、不允许回退、不允许原地不动。
本模块不碰数据库，只做纯规则判断。
"""

STATUS_NEW = "新客"
STATUS_VIEWING = "带看"
STATUS_NEGOTIATING = "谈价"
STATUS_DEAL = "成交"
STATUS_LOST = "流失"

FLOW = [STATUS_NEW, STATUS_VIEWING, STATUS_NEGOTIATING, STATUS_DEAL]
TERMINAL_STATUSES = (STATUS_DEAL, STATUS_LOST)
ALL_STATUSES = FLOW + [STATUS_LOST]


class InvalidTransitionError(ValueError):
    """非法状态流转。"""


def can_transition(from_status, to_status):
    if from_status in TERMINAL_STATUSES:
        return False
    if to_status == STATUS_LOST:
        return True
    if from_status not in FLOW or to_status not in FLOW:
        return False
    return FLOW.index(to_status) == FLOW.index(from_status) + 1


def next_statuses(from_status):
    """当前状态允许流转到的状态列表，供页面渲染可选项。"""
    return [s for s in ALL_STATUSES if can_transition(from_status, s)]


def validate_transition(from_status, to_status):
    """校验一次流转，非法则抛 InvalidTransitionError。"""
    if from_status not in ALL_STATUSES:
        raise InvalidTransitionError(f"未知状态：{from_status}")
    if to_status not in ALL_STATUSES:
        raise InvalidTransitionError(f"未知状态：{to_status}")
    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(
            f"不允许从「{from_status}」直接变为「{to_status}」，"
            "状态只能按 新客→带看→谈价→成交 逐级推进，或转为「流失」"
        )
