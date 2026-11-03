"""风险复核Agent：对一张**待派工单**给出"建议时限"，与派单Agent 定级形成对峙

## 它和 `app/risk.py` 的分工

`app/risk.py` 算的是**已经存在**的工单的风险分（转派次数、超期倍数、
争议未裁），输入是 `WorkOrder` 对象，输出是给人看的风险清单。

这个 Agent 干的是另一件事：在工单**还没建出来之前**，就根据居民历史
和原文里的安全隐患词，对"这张单该按几小时办结"提出异议。它不看工单，
因为那时候还没有工单。

两者不合并的原因：一个要跑在事务外（风险清单是查询）、一个必须跑在
派单之前（要影响定级），生命周期完全不同。

## 为什么派单Agent 自己不看这些

`DispatchAgent` 只看一个字符串（脱敏后的摘要）里的关键词，这是**对**的：
它的契约是"按规范定级"，规范写在知识库《物业报修响应时限与处置规范》里，
规范本身就是按问题类型分的，不看这户报过几次。

风险复核才需要跨表查历史。让 dispatch 去查历史，等于把"规范"和"经验"
揉成一个函数——规范是可以公开质询的（写在文件里），经验不可以。
分开之后答辩能说清："72 小时这个数来自规范第 3 条；升到 24 小时
用的是这户 30 天内第 3 次报漏水，理由是同类问题重复发生说明
首次处理没解决，继续给 72 小时等于把同样的事再拖三天。"

## 三个信号，都能单独解释

    安全词    原文出现「燃气/漏电/困人/明火/滑倒/消防通道」→ 直接 紧急
    重复报    同门牌 30 天内已有工单（本次不算）→ 历史 1 张升一档，2 张再升一档
    推诿史    同门牌历史工单有过跨单位转派      → 升一档

**没有信号就不表态**（返回 level=None），不硬凑理由。这一点很关键：
如果每个 Agent 都必须给结论，"多数一致"会变成常态，冲突消失，
消解机制就成了摆设。

## 为什么"重复报"数的是工单而不是居民诉求

一开始查的是 `resident_ticket`（居民端提交凭据），实测连续报三次一次都没
升级——因为走 FDE 首录台（社区代录）时**根本不建 resident_ticket**，
只有居民自己在小程序提交才建。查错了表，功能就是死的，而且不报错。

改成数 `work_order`：同门牌 30 天内的工单数。这个口径也更准——
工单意味着"这件事已经派出去处理过"，正是"处理没解决"的证据。
同时数诉求表和工单表会双计（一份诉求通常派一张单），所以只数工单。
"""
import re
from datetime import datetime, timedelta

from app.agents.base import BaseAgent
from app.agents.blackboard import SLA_HOURS
from app.models import WorkOrder, WorkOrderLog
from app.agents.fill import extract_room_key

# 安全隐患词：命中即"紧急 4 小时"。这份词表和 dispatch.SLA_RULES 里的
# 紧急档**故意不完全一样**——dispatch 认的是"已经出事"（水管爆裂、困人），
# 这里认的是"可能出事"（燃气味、冒烟、消防通道被堵、地面积水致人滑倒）。
# 两份词表重叠但不相等，这本身就是一处真实的分歧来源：
# 同一句"卫生间漏水"，dispatch 读出来是"漏水=较急 24h"，
# 而原文里如果有"老人滑倒"，它就不是普通漏水了。
SAFETY_WORDS = (
    "燃气", "煤气", "漏气", "焦味", "糊味", "漏电", "触电", "冒烟", "起火",
    "明火", "消防通道", "堵住消防", "堵塞消防", "消防栓", "坍塌", "塌陷",
    "坠落", "滑倒", "摔倒", "漏雨严重",
)

# 升一档：一般→较急→紧急
LADDER = ["紧急", "较急", "一般"]      # 索引越小越严
REPEAT_WINDOW_DAYS = 30
# 升档封顶。历史 1 张 → 升 1 档（"这是第 2 次报"）；2 张及以上 → 升 2 档。
#
# **这里踩过一个不会报错的坑**：初版写成 `{1: 1, 2: 2}.get(n, 0)`，
# n=3 时取到默认值 0 —— 第 4 次报修反而不升级了，而且一路静默。
# "次数越多越宽松" 这种错误没有任何机制能发现，只有手工造到第 4 次才看得见。
# 所以改成 min(n, 2)，单调不减。
REPEAT_UP_CAP = 2

# 门牌键必须归一化，否则判重是空的。
#
# 踩过的坑（两个，都是静默失效）：
#
# ① **同一户的两种写法认不出来**。直接复用 `fill.extract_room_key()` 时，
#    它对 "501" 和 "501室" 会给出两个不同的键（正则 `\d{3,4}室` 把"室"吃进去）。
#    而 subject 是模型重写的，"2栋2单元202卫生间漏水" 与
#    "2栋2单元202室漏水维修" 指同一户，键却不一样 → 重复报修永远数成 0。
#
# ② **真实诉求大多没有门号**。翻演示库 41 张单实测：只有 2 张能提取出
#    "栋+单元+门号"三元组。因为电梯、楼道灯、门禁、垃圾桶都是公共设施，
#    居民只会说"7栋3单元电梯异响"——没有门号可报。
#    只认三元组等于让功能在真实数据上全线失效，而它不报错。
#
# 所以地址分三级识别，并按问题类别决定"精确到哪一级"：
#    住宅类（漏水/噪音/油烟）必须有门号才算同一件事，
#    否则 3栋2单元的漏水会把 501 和 502 合成一件；
#    公共类（电梯/照明/门禁/消防/清运/堆物/路面）用"栋+单元"即可，
#    一栋楼只有一部电梯，"7栋3单元电梯"报三次就是同一部电梯。
ADDR_FULL = re.compile(r"(\d{1,3})\s*栋.*?(\d{1,2})\s*单元.*?(\d{3,4})")
ADDR_MID = re.compile(r"(\d{1,3})\s*栋.*?(\d{1,2})\s*单元")
ADDR_LOW = re.compile(r"(\d{1,3})\s*栋")


def addr_parts(text: str) -> dict:
    """地址三级识别。返回 {level, full, mid, low}

    level 3 = 栋+单元+门号（最准）；2 = 栋+单元；1 = 只有栋；0 = 认不出
    """
    t = text or ""
    m = ADDR_FULL.search(t)
    if m:
        return {"level": 3, "full": f"{m.group(1)}栋{m.group(2)}单元{m.group(3)}",
                "mid": f"{m.group(1)}栋{m.group(2)}单元", "keys": [m.group(3)]}
    m = ADDR_MID.search(t)
    if m:
        return {"level": 2, "full": "", "mid": f"{m.group(1)}栋{m.group(2)}单元",
                "keys": [m.group(2)]}
    m = ADDR_LOW.search(t)
    if m:
        return {"level": 1, "full": "", "mid": "", "keys": [m.group(1)]}
    return {"level": 0, "full": "", "mid": "", "keys": []}


# 问题类别。判重要求"同址 + 同类"——
# 不加类别的话，"2栋2单元202漏水" 会和 "2栋2单元202电梯异响" 算成同一件事，
# 报修三次电梯的居民会被判定"漏水报了三次"，时限升得毫无道理。
#
# 顺序有意义：先具体后宽泛（"楼道灯"必须命中照明，不能被"楼道"带走）。
CATEGORY = [
    ("电梯", ("电梯", "升降梯", "困人")),
    ("供水", ("漏水", "渗水", "水压", "水管", "爆管", "下水道", "反水", "堵塞")),
    ("照明", ("楼道灯", "声控灯", "感应灯", "路灯", "照明", "灯不亮", "灯坏", "灯闪")),
    ("门禁", ("门禁", "单元门", "对讲", "门锁", "门把手")),
    ("消防", ("消防", "灭火器", "烟感", "防火")),
    ("清运", ("垃圾", "清运", "垃圾桶")),
    ("堆物", ("堆物", "杂物", "纸箱", "堆放")),
    ("路面", ("路面", "地砖", "井盖", "台阶", "坡道", "坑洼")),
    ("噪音", ("噪音", "吵", "音响", "装修声")),
    ("油烟", ("油烟", "排烟", "油污")),
]

# 公共设施：这些事一栋楼只有一处，"栋+单元"就足以定位
PUBLIC_CATS = {"电梯", "照明", "门禁", "消防", "清运", "堆物", "路面"}


def category_of(text: str) -> str:
    t = text or ""
    for name, kws in CATEGORY:
        if any(k in t for k in kws):
            return name
    return ""


def same_place(a: dict, b: dict, cat: str) -> bool:
    """两处是不是同一个地址。地址精度按问题类别决定"""
    if a["level"] == 0 or b["level"] == 0:
        return False
    # 住宅类必须有门号：否则 501 和 502 的漏水会合成一件
    if cat not in PUBLIC_CATS:
        return a["level"] == 3 and b["level"] == 3 and a["full"] == b["full"]
    # 公共类：栋+单元相同即可。只有栋的也能比
    if a["mid"] and b["mid"]:
        return a["mid"] == b["mid"]
    if a["level"] == 1 and b["level"] == 1:
        return a["keys"] == b["keys"]
    return False


def _up(level: str, steps: int = 1) -> str:
    """按严重度阶梯升级；已在最严则原地不动"""
    i = LADDER.index(level) if level in LADDER else 2
    return LADDER[max(0, i - steps)]


class RiskReviewAgent(BaseAgent):
    name = "risk"

    def review(self, payload: dict) -> dict:
        """payload: {subject, level, room_key, exclude_fde, order_no}

        `level` 是派单Agent 已给出的定级——复核要有对象才叫复核。
        返回的 `level` 为 None 表示"没有异议，不表态"。
        """
        text = (payload.get("subject") or "").strip()
        base = payload.get("level") or "一般"
        here = addr_parts(text)
        cat = category_of(text)

        signals, level = [], base

        # ① 安全隐患词：不看已定级，直接压到紧急
        hits = [w for w in SAFETY_WORDS if w in text]
        if hits:
            signals.append({"code": "safety", "name": "安全隐患",
                            "detail": f"原文含「{hits[0]}」"})
            level = "紧急"

        # ② 同址同类 30 天内已有工单。本次的工单还没建出来，不必排除
        if here["level"] and cat:
            n, where = self._count_same(text, here, cat)
            steps = min(n, REPEAT_UP_CAP) if n >= 1 else 0
            if steps:
                signals.append({"code": "repeat", "name": "重复报修",
                                "detail": f"{where} {REPEAT_WINDOW_DAYS} 天内已派过 {n} 张同类工单"
                                         f"（本次是第 {n + 1} 次）"})
                level = _up(level, steps)

        # ③ 同址历史推诿（跨单位转派）
        if here["level"]:
            cross = self._cross_org_history(here, cat)
            if cross:
                signals.append({"code": "pingpong", "name": "推诿史",
                                "detail": cross})
                level = _up(level)

        # ④ 该户在人员底册上（残疾人 / 防溺水 / 留守）
        #
        # 这条是底册唯一真正产生业务价值的地方：报修地址命中底册，
        # 说明上门面对的可能是一个行动不便的老人、一个独居的残疾人、
        # 一户孩子独自在家的家庭——同样的漏水，处理优先级不一样。
        #
        # 只有权重 ≥2 才提级（一级/二级残疾、防溺水高/中风险、事实无人抚养）：
        # 底册 2842 人里权重 1 的占 2490 人，全都提级等于没提。
        #
        # 命中结论里**不写姓名**。轨迹和审计是要给人看、会被导出的，
        # 写"2栋2单元202有位一级残疾人"足够派单用，写姓名就是多余泄露。
        if here["level"] == 3:
            try:
                # 阈值只在 personbook 里定义一处（LIFT_MIN_WEIGHT / URGENT_WEIGHT）。
                # 这里手写 2 / 3 就是第二份判据——底册页面显示的结论也要用它，
                # 两处一旦不一致，页面说"会提级"而工单停在 72h，比不显示更糟。
                from app.personbook import (LIFT_MIN_WEIGHT, URGENT_WEIGHT,
                                            match_by_addr, top_weight)
                persons = [p for p, _ in match_by_addr(None, text)]
                weight = top_weight(persons)
                if weight >= LIFT_MIN_WEIGHT:
                    kinds = sorted({t.kind for p in persons for t in p.tags})
                    signals.append({
                        "code": "vulnerable",
                        "name": "重点人员户",
                        "detail": f"{here['mid']}{here['keys'][0]} 在底册上有 "
                                  f"{len(persons)} 名重点人员（{'、'.join(kinds)}）",
                    })
                    level = "紧急" if weight >= URGENT_WEIGHT else _up(level)
            except Exception:                                # noqa: BLE001
                # 底册查不到不该拖垮派单：没人导入过底册、表还没建、
                # 甚至以后底册表结构改了，都只应该让这条规则失效，
                # 而不是让整条工单建不出来。
                pass

        if not signals or level == base:
            # 不表态。注意这里**不写审计**——复核无异议不该产生记录，
            # 否则 audit_log 里全是"检查过了没问题"，真问题被淹没。
            return {"level": None, "signals": [], "base": base,
                    "hours": SLA_HOURS.get(base, 72)}

        self._audit("risk_review", text,
                    f"建议 {level}（原 {base}）：" + "；".join(s["detail"] for s in signals))
        return {
            "level": level,
            "hours": SLA_HOURS.get(level, 72),
            "base": base,
            "signals": signals,
            # 置信度：安全词是硬规则给满，重复报修按次数递增，推诿史最弱
            "confidence": self._confidence(signals),
            "reason": "；".join(s["detail"] for s in signals),
        }

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _candidates(here: dict):
        """把"可能同址同类的历史单"粗筛出来

        粗筛用 LIKE 缩小范围（门牌里最能定位的那一段数字），精筛交给
        `same_place()`。**只靠 LIKE 不够**：`%3栋%` 会把 13栋、23栋一起捞进来，
        而 `%501%` 又会命中 5010。所以粗筛宁宽勿窄，精筛必须严格。
        """
        probes = [p for p in (here["full"], here["mid"]) if p]
        if not probes and here["level"] == 1:
            probes = [f"{here['keys'][0]}栋"]
        if not probes:
            return []
        q = WorkOrder.query
        # 多个 probe 之间是 OR
        from sqlalchemy import or_
        q = q.filter(or_(*[WorkOrder.content.like(f"%{p}%") for p in probes]))
        return q.order_by(WorkOrder.id.desc()).limit(60).all()

    def _count_same(self, text: str, here: dict, cat: str):
        """同址同类的工单数，返回 (n, 人类可读的位置描述)"""
        since = datetime.now() - timedelta(days=REPEAT_WINDOW_DAYS)
        n = 0
        for o in self._candidates(here):
            if o.created_at and o.created_at < since:
                continue
            other = addr_parts(o.content)
            if same_place(here, other, cat) and category_of(o.content) == cat:
                n += 1
        where = here["full"] or here["mid"] or (f"{here['keys'][0]}栋" if here["keys"] else "")
        return n, where

    @staticmethod
    def _same_place_orders(here: dict, cat: str):
        """同址的工单（推诿史用）。推诿史不要求同类：
        这户被推诿过，不管推的是漏水还是电梯，都说明这家单位处理这户的方式有问题。
        """
        out = []
        probes = [p for p in (here["full"], here["mid"]) if p]
        if not probes and here["level"] == 1:
            probes = [f"{here['keys'][0]}栋"]
        if not probes:
            return out
        from sqlalchemy import or_
        for o in (WorkOrder.query
                  .filter(or_(*[WorkOrder.content.like(f"%{p}%") for p in probes]))
                  .order_by(WorkOrder.id.desc()).limit(60).all()):
            if same_place(here, addr_parts(o.content), cat):
                out.append(o)
        return out

    def _cross_org_history(self, here: dict, cat: str) -> str:
        """该地址的历史工单里，相邻两次转派是否换了单位

        只看同地址的工单是有意的：跨单位转派如果在**别的地址**上发生过，
        说明这家物业的流程有问题；但在这里我们关心的是"这户会不会又被推"，
        所以必须同址同源。
        """
        orders = self._same_place_orders(here, cat)
        if not orders:
            return ""
        where = here["full"] or here["mid"] or ""
        ids = [o.id for o in orders]
        logs = (WorkOrderLog.query
                .filter(WorkOrderLog.order_id.in_(ids))
                .filter(WorkOrderLog.action == "transfer")
                .order_by(WorkOrderLog.order_id, WorkOrderLog.created_at).all())
        by = {}
        for lg in logs:
            by.setdefault(lg.order_id, []).append(lg)
        for oid, seq in by.items():
            for a, b in zip(seq, seq[1:]):
                if (a.actor_org or "") and (b.actor_org or "") and a.actor_org != b.actor_org:
                    return f"{where} 历史工单在 {a.actor_org} 与 {b.actor_org} 之间转过手"
        return ""

    @staticmethod
    def _confidence(signals) -> float:
        # 安全词是"已经能闻到/看到"，给 0.95；重复报修 0.8；推诿史是推测 0.6。
        # 置信度目前只用于页面展示和排序，不参与消解——消解只看严重度。
        w = {"safety": 0.95, "repeat": 0.8, "pingpong": 0.6}
        vals = [w.get(s["code"], 0.5) for s in signals]
        return round(max(vals) if vals else 0.5, 2)


def suggest_level(text: str, base_level: str) -> dict:
    """给脚本/测试用的简写"""
    return RiskReviewAgent().review({"subject": text, "level": base_level})
