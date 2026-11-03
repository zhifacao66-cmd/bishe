"""黑板（Blackboard）：Agent 之间的共享结论区 + 冲突消解

## 为什么需要它

改造前 `Orchestrator.handle_fde()` 是一条单向管道：

    security.run() → parser.run() → for item: dispatch.run() / fill.run()

后一个 Agent 只拿前一个的输出当输入，**谁也不能影响谁的结论**。
这样叫"多 Agent"其实站不住——把 dispatch 和 fill 写成函数直接调用，
系统行为一行都不会变。答辩问"你的多 Agent 比单次调模型强在哪"，
管道架构答不上来。

真正的多主体至少要满足两条：
  ① 多个 Agent 对**同一件事**各自表态；
  ② 表态不一致时，有一个明确的消解规则和可追溯的消解记录。

这个文件补的就是这两条。

## 为什么消解规则是"取更严"而不是投票

系统里 7 个 Agent，只有 parser / knowledge 调模型，另外 5 个是规则。
把它们做成多数表决没有意义：规则 Agent 会互相印证同一次词表命中，
票数只反映谁的词表更长，不反映谁更对。

真实的冲突只有一类：**这张工单该按几小时办结**。
在这件事上正确的做法不是表决，是**单向妥协**：

    合规与安全的要求只能加严，不能放宽。

所以 `resolve()` 取 `sla_level` 里最严的那个。反过来做会出事——
两张 72 小时的宽松结论，压过一张"这户已经报过三次"的紧急结论，
而制度上恰恰是后者该说了算。

这条规则也解释了为什么 `security` 的表态永远不被推翻：
密级判断是对外发内容的**否决权**，不是可以和别人比大小的意见。

## 消解记录长什么样

    {
      "topic": "sla",
      "rule": "更严者胜（合规只能加严不能放宽）",
      "value": ("较急", 24),
      "winner": "risk",
      "losers": ["dispatch"],
      "claims": [
        {"agent": "dispatch", "value": ("一般", 72), "reason": "词表命中「漏水」"},
        {"agent": "risk",     "value": ("较急", 24), "reason": "3 栋 2 单元 30 天内第 3 次报漏水"}
      ],
      "reason": "派单Agent 定「一般 72h」，风险复核定「较急 24h」，采纳较严者"
    }

页面直接渲染 claims 和 reason，不需要再加工——**演示时能一句话讲完的
结构，比精确但讲不清的结构更有用**。
"""
from datetime import datetime
import random
import string

# 严重度：数字越小越严。判"更严"就是比这个数。
SEVERITY = {"紧急": 0, "较急": 1, "一般": 2}

# 与 dispatch.SLA_RULES 的时限保持同源。这里不 import dispatch，
# 是为了让黑板不依赖具体 Agent——回环依赖会让单元测试互相牵连。
SLA_HOURS = {"紧急": 4, "较急": 24, "一般": 72}

# 议题 → 消解策略。目前只有一个议题真正需要消解：工单该按几小时办结。
#
# 只登记一个议题不是偷懒，是**没有第二个真冲突**。反思一下曾经想加的两个：
#   · "密级"：security 判 level=3 时 dispatch 仍可外发（只带脱敏文本），
#     两者不矛盾，做成冲突是伪造分歧；
#   · "是否督办"：现在没有任何 Agent 会表态"该督办"，加了就是自说自话。
# 议题表里只写真实存在的分歧，答辩时才敢说"每一次消解都对应一次真冲突"。
TOPIC_RULE = {
    "sla": ("strict", "更严者胜（合规要求只能加严，不能放宽）"),
}


def new_run_id() -> str:
    """8 位运行号：日期 + 4 位随机，够用且能看出是哪天跑的"""
    day = datetime.now().strftime("%m%d")
    tail = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"R{day}-{tail}"


def severity_of(level: str) -> int:
    """未知等级按最宽处理（返回 9），这样它永远不会赢过任何已知等级"""
    return SEVERITY.get(level, 9)


class Blackboard:
    """一次诉求处理对应一块黑板，处理完即丢弃（落库的是 AgentStep 行）"""

    def __init__(self, run_id: str = ""):
        self.run_id = run_id or new_run_id()
        self.steps = []        # 过程记录（不参与消解）
        self.claims = []       # 结论表态（参与消解）
        self.resolutions = []  # 消解结果
        self._seq = 0

    # ---------------------------------------------------------------- 记录

    def step(self, agent, action, decision="", reason="", took_ms=0,
             degraded=False, fde_id=None, work_item_id=None, order_no="",
             topic=""):
        """记一条轨迹。topic 非空时，这条同时算一次表态。

        这样设计是因为"表态"和"留痕"本质是同一件事：Agent 说了什么、
        什么时候说的、花了多久。分成两个方法调用，接口层迟早漏掉一个
        （漏掉留痕不影响功能，所以不会有人发现）。
        """
        self._seq += 1
        rec = {
            "seq": self._seq, "agent": agent, "action": action,
            "decision": (decision or "")[:200], "reason": (reason or "")[:400],
            "took_ms": int(took_ms or 0), "degraded": bool(degraded),
            "fde_id": fde_id, "work_item_id": work_item_id,
            "order_no": order_no or "", "topic": topic or "",
            "at": datetime.now().strftime("%H:%M:%S"),
        }
        self.steps.append(rec)
        if topic:
            self.claims.append(rec)
        return rec

    def post(self, agent, topic, value, reason="", **refs):
        """提交一个结论（不产生新序号之外的东西，仍走 step 保证一条一行）"""
        decision = value if isinstance(value, str) else repr(value)
        if isinstance(value, tuple):
            # ("较急", 24) 显示成 "较急 24h"，免得页面自己拼
            decision = f"{value[0]} {value[1]}h"
        return self.step(agent, "claim", decision=decision, reason=reason,
                         topic=topic, **refs)

    # ---------------------------------------------------------------- 消解

    def claims_on(self, topic):
        return [c for c in self.claims if c["topic"] == topic]

    def conflicts(self):
        """有分歧的议题：同一 topic 下出现两种以上不同的 decision"""
        out = []
        for topic in {c["topic"] for c in self.claims}:
            items = self.claims_on(topic)
            if len({c["decision"] for c in items}) > 1:
                out.append({
                    "topic": topic,
                    "agents": [c["agent"] for c in items],
                    "values": [c["decision"] for c in items],
                })
        return out

    def resolve(self, topic, **refs):
        """消解一个议题，返回结果字典；没有表态或没有分歧都返回 None

        **没有分歧时返回 None 而不是"消解成功"**：把"两个 Agent 意见一致"
        和"两个 Agent 吵架后被裁定"显示成同一件事，等于伪造协作。页面
        只渲染真分歧，答辩时说的每一句都能对应到一次真实冲突。
        """
        items = self.claims_on(topic)
        # **无论有没有分歧，都把该议题移出待议区**。
        # 不清理的后果实测过：一条诉求拆出 3 个业务项时，第 2 项的
        # `resolve("sla")` 会把第 1 项的表态一起拿来比大小，于是第 2 项
        # 明明该升到紧急，却被第 1 项里"较急"那条旧表态挡住，winner 停在
        # 第 1 轮的 dispatch 上——而且不报错，只是等级上不去。
        # 表态本身仍留在 steps 里（页面的时间线就是读那份），
        # 只是不再参与后面几轮的消解。
        self.claims = [c for c in self.claims if c["topic"] != topic]

        if len({c["decision"] for c in items}) < 2:
            return None

        policy, rule = TOPIC_RULE.get(topic, ("strict", "更严者胜"))

        # 更严者胜。同严时取先表态的（调度顺序即优先级，
        # 免得同一份输入两次跑出不同结果——测试要能复现）
        winner = items[0]
        for c in items[1:]:
            if severity_of(_level_of(c)) < severity_of(_level_of(winner)):
                winner = c

        losers = [c for c in items if c is not winner]
        res = {
            "topic": topic, "rule": rule, "policy": policy,
            "winner": winner["agent"], "value": winner["decision"],
            "losers": [c["agent"] for c in losers],
            "claims": [{"agent": c["agent"], "value": c["decision"],
                        "reason": c["reason"]} for c in items],
            "reason": "；".join(
                f"{c['agent']}Agent 定「{c['decision']}」"
                + (f"（{c['reason']}）" if c["reason"] else "")
                for c in items
            ) + f" → 采纳「{winner['decision']}」",
        }
        self.resolutions.append(res)
        # 消解本身也记一步，这样时间线上能看到"这一刀是谁落的"
        self.step("resolve", "resolve", decision=f"采纳 {winner['agent']} 的「{winner['decision']}」",
                  reason=res["reason"], topic="", **refs)
        return res

    def resolution(self, topic):
        for r in self.resolutions:
            if r["topic"] == topic:
                return r
        return None

    def pending_rows(self):
        """可直接落库的 AgentStep 行（调用方负责 add + commit）"""
        from app.models import AgentStep
        return [AgentStep(run_id=self.run_id, **{
            k: v for k, v in s.items() if k in (
                "seq", "agent", "action", "decision", "reason", "topic",
                "took_ms", "degraded", "fde_id", "work_item_id", "order_no")
        }) for s in self.steps]


def _level_of(claim):
    """从 "较急 24h" / ("较急", 24) 里取出等级名"""
    v = claim.get("decision") or ""
    for name in SEVERITY:
        if v.startswith(name):
            return name
    return ""
