"""logistics.py — 物流承运商信息

小程序端物流模块按产品要求只展示：物流公司名称、运单号、官方查询电话，
不返回任何物流轨迹节点。这里维护常见承运商的官方客服电话映射。
"""

# (公司名关键词, 官方查询电话)；按顺序匹配，命中即返回
_PHONE_RULES: list[tuple[str, str]] = [
    ("顺丰", "95338"),
    ("中通", "95311"),
    ("圆通", "95554"),
    ("韵达", "95546"),
    ("申通", "95543"),
    ("京东", "950616"),
    ("邮政", "11183"),
    ("EMS", "11183"),
    ("极兔", "956025"),
    ("德邦", "95353"),
    ("百世", "95320"),
    ("天天", "4001888888"),
    ("宅急送", "4006789000"),
]


def phone_of(company: str | None) -> str:
    """根据物流公司名称返回官方查询电话；未知承运商返回空串（前端展示缺省值）。"""
    name = (company or "").strip()
    if not name:
        return ""
    upper = name.upper()
    for keyword, phone in _PHONE_RULES:
        if keyword.upper() in upper:
            return phone
    return ""
