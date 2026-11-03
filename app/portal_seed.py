"""居民端基础数据：三级管理者公开信息

这里只放**应当公开**的内容：单位名称、职责、对外电话、办公地址、服务时间。
不放任何个人手机号、身份证、住址门牌——居民端是公网可见页面，
公开边界必须比内部端更严。
"""
from app.extensions import db
from app.models import OrgContact

DEMO_ORG_CONTACTS = [
    {
        "level": "street", "sort": 1,
        "name": "幸福里街道办事处",
        "duty": "统筹辖区社区治理与民生政策落实；受理跨社区、需多部门协同的事项；"
                "对社区与物业的办理情况进行督办。",
        "phone": "0571-8601 0001",
        "address": "幸福路 1 号 街道便民服务中心 2 层",
        "service_hours": "工作日 9:00-17:30",
    },
    {
        "level": "community", "sort": 2,
        "name": "幸福里社区居民委员会",
        "duty": "居民日常事务受理；入户走访与特殊群体探视；民政救助、老龄补贴初审；"
                "邻里矛盾纠纷调解；人口信息登记。",
        "phone": "0571-8601 0002",
        "address": "幸福里小区 1 号楼 1 层 社区服务站",
        "service_hours": "工作日 9:00-17:00（周六上午值班）",
    },
    {
        "level": "property", "sort": 3,
        "name": "幸福里物业服务有限公司",
        "duty": "公共设施设备维修养护；楼道照明、电梯、供水供电报修；"
                "保洁绿化与秩序维护；物业类投诉受理。",
        "phone": "0571-8601 0003",
        "address": "幸福里小区 2 号楼东侧 物业服务中心",
        "service_hours": "24 小时值班（夜间急修 0571-8601 0009）",
    },
]


def seed_org_contacts() -> int:
    """幂等写入：表里已有数据就跳过，不覆盖运营中改过的电话"""
    if OrgContact.query.count() > 0:
        return 0
    for d in DEMO_ORG_CONTACTS:
        db.session.add(OrgContact(**d))
    db.session.commit()
    return len(DEMO_ORG_CONTACTS)


def grouped_contacts() -> list[dict]:
    """按街道 → 社区 → 物业分组，供居民端展示"""
    order = [("street", "街道办事处"), ("community", "社区居民委员会"), ("property", "物业服务")]
    rows = OrgContact.query.filter_by(active=True).order_by(OrgContact.sort).all()
    groups = []
    for key, label in order:
        items = [r for r in rows if r.level == key]
        if items:
            groups.append({"level": key, "label": label, "items": items})
    return groups
