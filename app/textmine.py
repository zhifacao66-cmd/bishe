"""投诉文本热点：主题归并 + 新词发现

## 为什么这里不用 KMeans / DBSCAN

用户提的是"对投诉文本聚类发现热点"。这个方法本身没错，但在这份数据上
跑不起来，原因不是技术，是**样本与文本形态**：

1. **样本**：居民诉求目前个位数。任何聚类算法在 <20 条上都会给出
   "每个点一个簇"或"全在一个簇"，两种都不可用。KMeans 还要先定 k，
   而 k 是从数据里看的——先定 k 再聚类就是拿结论倒推方法。
2. **文本形态**：一条诉求 50–200 字，是"一段话"不是"一篇文档"。
   TF-IDF 在这么短的文本上方差极大，两句话只有一个字之差就可能
   距离很远，而语义上它们是同一件事（"3 栋楼道堆纸箱" vs "3栋2楼堆物"）。
   这种短文本，词表主题归并比向量距离可靠得多。
3. **可解释性**：聚类出 5 个簇，还得人工给每个簇起名、核对。
   而在社区场景里，主题本来就是**可枚举的**（漏水、噪音、停车、垃圾…），
   直接按主题归并，省掉"命名"这一步，且结果能直接进督办清单。

所以这里的做法是：
- `hotspots()`：主题词表归并，可解释、可增补、可复现
- `ngrams()`：**真正数据驱动的部分**——统计高频字串，用来发现
  词表里没有的新热点（例：突然一堆人提"充电桩"）。词表靠人工会漏，
  这一层补上
- `cluster()`：接口保留，样本不够时**明确返回不可用并说明原因**，
  而不是给一个看起来像结果的假结论

攒够 50 条诉求后，把 `cluster()` 换成真正的向量聚类是可行的，
到那天 `hotspots()` 可以直接拿来给簇命名——这正是它现在该有的形态。
"""
import re
from collections import Counter

from app.extensions import db
from app.models import ResidentTicket

# 聚类的最低样本门槛。低于此数不跑。
MIN_N_FOR_CLUSTER = 20

# 主题词表。**一个主题可以有多个词，命中任一即归入该主题**。
#
# ## 这张表是调出来的，不是想出来的
#
# 第一版可以跑，但把它对着 25 条真实诉求跑一遍，误归率高得没法看：
#   · 「卫生间」里的「卫生」把漏水归到了垃圾清运
#   · 「1单元门口」里的「单元门」把消防通道归到了门禁
#   · 「楼道」把楼道灯、门禁、感应灯全归成了"楼道堆物"
#   · 「邻居撬开门」把电梯故障归成了邻里纠纷
#   · 「下水道臭」里的「臭」把反水归成了垃圾
#
# 归纳出三条硬规则，改词表时照着办：
#
# **一、地点词、身份词不能当主题词。** "楼道""单元门""小区"是**在哪儿**，
#    "老人""业主""孩子"是**谁**，都不是**什么事**。楼道里可能装灯、装门禁、
#    堆杂物、贴小广告，用地点归并等于把四件事混成一件；而"老人"会出现在
#    漏水（"老人滑倒"）、垃圾（"老人拎着垃圾"）、电梯（"老人被困"）里，
#    用它归并等于把三个不同的问题全算成"关注老年人"。
#    主题词必须是"问题"本身，或者是**状态词**（独居、孤寡、低保）——
#    状态词出现时主题必然成立，身份词不是。
#
# **二、宽泛字与常用词不算词。** "卫生"在"卫生间"里、"臭"在"下水道"里、
#    "邻居"在"邻居帮我撬门"里——它们出现频率高，但与主题无关。
#    一个词只有在**本主题语境下**才高概率出现的，才能进表。
#
# **三、宁可漏，不要错。** 漏了的靠 `ngrams()` 那座桥补（发现新词候选），
#    错了没有任何机制能发现——它会安静地把统计结论带偏，
#    而页面上的数字看着完全正常。所以拿不准的词先不放。
TOPICS = {
    "房屋漏水": ["漏水", "渗水", "积水", "墙面发霉", "天花板", "防水", "滴水", "泡起来"],
    "下水管道": ["下水道", "下水管", "排污", "堵塞", "反水", "污水", "化粪池", "地漏", "冒污水"],
    "电梯运行": ["电梯", "升降", "困人", "卡在", "关不严"],
    "楼道堆物": ["堆物", "堆放", "杂物", "占用公共", "堵塞通道", "飞线", "堆满", "堆了"],
    "噪音扰民": ["噪音", "噪声", "扰民", "装修", "吵", "大声", "深夜",
               "影响休息", "跑来跑去", "拖家具", "半夜"],
    "停车管理": ["停车", "车位", "乱停", "占道", "堵车", "车辆", "车满"],
    "宠物管理": ["宠物", "养犬", "遛狗", "狗叫", "犬只", "拴绳", "没拴"],
    "垃圾清运": ["垃圾", "清运", "垃圾桶", "异味", "乱扔", "苍蝇", "垃圾房"],
    "绿化树木": ["绿化带", "树木", "修剪", "枯死", "草坪", "遮光", "砍树", "砍了三棵"],
    "消防安全": ["消防", "防火", "灭火器", "安全出口", "充电桩", "燃气", "着火"],
    "路灯照明": ["路灯", "照明", "灯不亮", "灯泡", "走廊灯", "感应灯", "黑漆漆", "灯坏"],
    "水电暖气": ["停水", "停电", "供暖", "暖气", "水压", "水表", "电表", "供气"],
    "收费争议": ["物业费", "收费", "费用", "涨价", "账目", "物管费", "两毛一平"],
    "违章搭建": ["违建", "私搭", "乱建", "改建", "破坏承重", "封阳台", "铁皮棚", "阳光房"],
    "门禁对讲": ["门禁", "对讲", "门锁", "道闸", "监控", "刷卡", "门禁卡"],
    "安保秩序": ["保安", "门卫", "巡逻", "外来人员", "失窃", "被盗"],
    "邻里纠纷": ["邻里", "相邻", "纠纷", "调解", "矛盾"],
    # 「老人」不能进来——它是**身份词**，和「楼道」一样属于"谁/哪儿"而不是"什么事"。
    # 实测：漏水那条写"老人踩到积水滑倒"、垃圾那条写"老人拎着垃圾走十分钟"、
    # 电梯那条写"有位老人被关在里面"，三条全被"老人"吸进特殊群体，
    # 于是热点榜首变成"特殊群体 4 条"，而真正的问题是漏水、垃圾和电梯。
    # 只用状态词：独居/孤寡/空巢/高龄/低保/残疾——这些词出现时，
    # 主题**必然**是该关注的人，不会被别的话题捎带。
    "特殊群体": ["独居", "孤寡", "空巢", "高龄", "低保", "残疾", "空巢老人",
               "困难家庭", "失能"],
}

# 高频虚词与无信息字串，从 n-gram 里剔掉。不剔的话 Top 榜全是"我们小区"。
STOP = set("""
我们 你们 他们 这个 那个 什么 怎么 可以 已经 一直 非常 特别 现在 但是 因为 所以
如果 就是 还是 不知 知道 请问 希望 麻烦 帮忙 反映 反应 投诉 问题 情况 时候 每天
每天 晚上 早上 白天 上午 下午 几点 一次 两次 好多 有点 一下 一个 我们小区
小区 社区 物业 居委 居委会 街道 领导 部门 人员 工作人员 电话 联系 处理 解决
要求 需要 应该 不能 没有 就是 这样 那样 这种 那种 地方 东西 事情
""".split())

_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")


def _text(t):
    """取一条诉求里所有可分析文本：原文 + 地址 + 业务线。

    地址要进来：居民写"3 栋 2 单元楼道堆物"，主题词在原文里，
    但如果是"3栋"这类地址独有的信息，在聚类时要能区分位置热点。
    不过主题归并**只看原文**——地址里的"水"字会把"供水公司宿舍"
    误归到漏水主题。所以分开两个函数，不混用。
    """
    return (t.raw_text or "")


def _hit_topics(text):
    """命中哪些主题。返回排序后的主题名列表。

    一条文本可以命中多个主题（"楼道堆物还漏水"），这是对的——
    一个诉求本来就可能是复合矛盾，强行归一类会丢信息。
    统计时按"命中次数"算，所以各主题数之和会大于诉求数，
    页面必须标注这一点，否则会被当成数据矛盾。
    """
    hits = []
    for name, words in TOPICS.items():
        if any(w in text for w in words):
            hits.append(name)
    return hits


def hotspots():
    """按主题聚合。每个主题给：诉求数、未办结数、占比、样例。

    排序用"未办结数"优先而不是"总数"：办了 10 件漏水只说明漏水多，
    有 6 件没办完才是要动手的地方。
    """
    tiles = ResidentTicket.query.order_by(ResidentTicket.created_at.desc()).all()
    total = len(tiles)
    buckets = {k: [] for k in TOPICS}
    unmatched = []
    for t in tiles:
        hits = _hit_topics(_text(t))
        if not hits:
            unmatched.append(t)
            continue
        for h in hits:
            buckets[h].append(t)

    rows = []
    for name, group in buckets.items():
        if not group:
            continue
        rows.append({
            "topic": name,
            "count": len(group),
            # 占比的分母是"诉求总数"，不是"命中总数"：一条诉求命中两个主题时，
            # 两个主题各记一次，占比之和可能超过 100%。这是可接受的，
            # 页面标注"可重复计数"即可；用命中总数当分母会算出超过 100% 的占比。
            "ratio": round(len(group) / total, 4) if total else 0,
            "open": sum(1 for t in group if (t.status or "") != "done"),
            "people": len({(t.phone or "") for t in group}),
            "first_at": min(t.created_at for t in group).strftime("%Y-%m-%d")
                        if group[0].created_at else "",
            "last_at": max(t.created_at for t in group).strftime("%Y-%m-%d")
                       if group[-1].created_at else "",
            "samples": [{
                "id": t.id, "code": t.code,
                "text": (t.raw_text or "")[:80],
                "status": t.status or "",
                "at": t.created_at.strftime("%Y-%m-%d") if t.created_at else "",
            } for t in group[:3]],
        })
    rows.sort(key=lambda x: (-x["open"], -x["count"]))
    return {
        "total": total,
        "matched": total - len(unmatched),
        "unmatched": len(unmatched),
        "topics": rows,
        # 命中次数之和 > 诉求数是正常的（一条诉求可命中多个主题）
        "hit_total": sum(x["count"] for x in rows),
        "note": "一条诉求可命中多个主题，各主题数量之和大于诉求总数属正常。",
    }


def ngrams(n=2, top=30, min_count=2):
    """高频字串。**这是词表之外唯一真正"从数据里长出来"的东西**。

    做法：把每条诉求切成连续汉字串，取所有 n 字窗口，统计出现次数。
    不做分词（没有 jieba，也不该为这个装一个），因为 n-gram 在
    "发现新词"这个目的上够用：一个词反复出现，它的 n-gram 一定高频。

    局限要写清：n-gram 会切出"栋楼""楼道堆"这种半截词。
    所以输出里同时给"覆盖了几条诉求"（`docs`），
    `docs` 高而 `count` 也高的，才是真热点；只有 `count` 高的是同一条
    诉求里重复说的字。
    """
    tiles = ResidentTicket.query.all()
    cnt = Counter()
    docs = Counter()
    for t in tiles:
        seen = set()
        for run in _CJK_RUN.findall(_text(t)):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if len(g) < n or g in STOP:
                    continue
                cnt[g] += 1
                seen.add(g)
        for g in seen:
            docs[g] += 1
    rows = []
    for w, c in cnt.items():
        if c < min_count:
            continue
        # `docs` 是"出现在几条不同诉求里"。**它才是热点的真指标**：
        # 同一条诉求里把"漏水"说五遍，count=5 但 docs=1，那不是热点，
        # 是一个人在着急。按 count 排会把这类单条诉求顶到榜首。
        # 早先这里误写成了未定义的 `d`（NameError），一跑就炸；
        # 现在改成显式取，顺便把"为什么按 docs 排"记在原地。
        d = docs.get(w, 0)
        rows.append({
            "word": w, "count": c, "docs": d,
            "ratio": round(d / len(tiles), 4) if tiles else 0,
        })
    rows.sort(key=lambda x: (-x["docs"], -x["count"]))
    return rows[:top]


def _tokens(text, n=2):
    """切 n-gram（只留连续汉字串，去停用词）。"""
    c = Counter()
    for run in _CJK_RUN.findall(text or ""):
        for i in range(len(run) - n + 1):
            g = run[i:i + n]
            if len(g) == n and g not in STOP:
                c[g] += 1
    return c


def _vectorize(texts, n=2, min_docs=2, max_df=0.5):
    """TF-IDF 矩阵。返回 (矩阵, 词表)。

    **为什么自己写而不装 sklearn**：这套要到 Windows Server 上跑，
    为了一个 25×600 的矩阵引入 scipy+sklearn（编译依赖、上百 MB），
    不如三十行 numpy。规模不会长到需要它的程度——
    真到几万条诉求时，该换的是整个存储方案，不是聚类库。

    idf 用 `log((1+N)/(1+df)) + 1`（平滑版）：不加 1 的话，
    出现在全部诉求里的词 df=N，log(N/N)=0，那一维直接归零；
    而"漏水""停车"这类词恰好可能出现在很多条里，把它们抹掉不合理。

    `max_df` 是**自动停用词**：出现频率超过这个比例的字串没有区分度，
    留着只会把不相干的文本拉近。它不是可选的调优项——
    "单元""楼道"这类词在全社区诉求里必然高频，靠人工往 STOP 里加是
    加不完的，得让数据自己筛。
    """
    import numpy as np

    toks = [_tokens(t, n) for t in texts]
    df = Counter()
    for tk in toks:
        for g in tk:
            df[g] += 1
    N = len(texts)
    # 至少留 1 个特征：max_df 设得过小时（比如 0.05），
    # int(N*0.05) 会是 0，vocab 直接空掉，聚类静默返回"无法构造特征"。
    cap = max(1, int(N * max_df)) if max_df else None
    vocab = sorted(g for g, c in df.items()
                   if c >= min_docs and (cap is None or c <= cap))
    if not vocab:
        return None, []
    idx = {g: i for i, g in enumerate(vocab)}
    mat = np.zeros((N, len(vocab)), dtype=np.float64)
    for r, tk in enumerate(toks):
        for g, c in tk.items():
            j = idx.get(g)
            if j is None:
                continue
            # 次线性 tf：一条诉求里"漏水"出现 5 次不比出现 1 次重要 5 倍
            mat[r, j] = (1.0 + np.log(c)) * (np.log((1.0 + N) / (1.0 + df[g])) + 1.0)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms, vocab


def _purity(groups, texts):
    """聚类纯度（严格版）：簇内**全部成员共有一个主题**，这个簇才算纯。

    ## 为什么不用宽松版

    一开始用的是"取簇内最常见主题，算命中人数占比"。它在 25 条数据上
    给出 69.6%，看着不错——但那是假的：2 条成员的簇里只要有 1 条命中，
    宽松版立刻给 50%；而**随机分组也能拿到接近 50%**。
    一个随机基线都刷得到的指标，没有诊断价值，只会让人误以为聚类有效。

    严格版要求成员主题集合的交集非空：`{漏水} ∩ {漏水} ∩ {漏水}` = 纯；
    `{消防} ∩ {下水道}` = ∅ = 不纯。同一份数据上它给 **39.1%**。
    这个数字不好看，但它和人工逐簇核对的结果一致（9 个多成员簇里
    约 3.5 个是对的）——**能对上人工判断的指标才有用**。

    ## 它还有循环论证的成分（实证）

    纯度拿 `_hit_topics()`（本文件的人工词表）当标准答案，
    衡量的是"聚类与词表的一致程度"，不是"聚类绝对正确"。
    两边都错的情况它发现不了。所以页面上的措辞是
    "聚类纯度"，不是"聚类准确率"。

    这一点有实测证据：词表里去掉「老人」这个身份词之后，
    同一份 25 条数据、同一个阈值 0.16、同样的 9 个多成员簇 / 23 条成员，
    纯度从 39.1% 掉到 **30.4%**（分子 9 → 7）。
    聚类算法一行没改，分数变了——因为标准答案变了。
    所以**任何纯度数字都必须连同词表版本一起报**，
    单独说"纯度 30%"没有意义。
    """
    tot = hit = 0
    for g in groups:
        if len(g) < 2:
            continue
        sets = [set(_hit_topics(texts[i])) for i in g]
        tot += len(g)
        if all(sets) and set.intersection(*sets):
            hit += len(g)
    return round(hit / tot, 4) if tot else 0.0


def _hac(sim, threshold):
    """平均链接层次聚类。返回簇列表（每簇是行号列表）。

    为什么用**平均链接**而不是单链接：单链接会把 A-B、B-C 串起来，
    哪怕 A 和 C 毫无关系（链式效应），结果是一个巨大的簇把什么都吞进去。
    平均链接要求"两簇所有成员之间的平均相似度"够高，簇更紧凑。

    复杂度 O(n³)，25 条诉求是 1.5 万次比较，毫秒级。
    n 上千时才需要考虑换成向量化或 KMeans。
    """
    n = sim.shape[0]
    clusters = [[i] for i in range(n)]
    while len(clusters) > 1:
        bi = bj = -1
        best = -2.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                s = sum(sim[x][y] for x in a for y in b) / (len(a) * len(b))
                if s > best:
                    best, bi, bj = s, i, j
        if best < threshold:
            break
        clusters[bi] = clusters[bi] + clusters[bj]
        del clusters[bj]
    clusters.sort(key=len, reverse=True)
    return clusters


def cluster(n=2, min_docs=2, threshold=0.16, max_df=0.5):
    """主题聚类。样本够才算，不够就如实说。

    ## 方法

    TF-IDF(2-gram) → L2 归一化 → 余弦相似度 → 平均链接 HAC。

    不用 KMeans 的理由：**k 得先定，而定 k 靠看的**。
    先聚类再挑一个"看着顺眼"的 k，是把结论倒推成参数。
    HAC 不需要预设簇数，切在哪儿由相似度阈值决定，而阈值是一个
    可以公开、可以复算、可以说"我调过"的量。

    ## 阈值是量出来的，不是推出来的

    在 25 条真实诉求上扫了一遍（`n=2, min_docs=2`，两两相似度中位数 **0.000**、
    P95 = 0.290、max = 0.505）：

        阈值 0.30 → 17 簇（25 条散成 17 簇，等于没聚）
        阈值 0.22 → 14 簇
        阈值 0.16 → 11 簇（规模 5/3/3/2/2/2/2/2/1/1/1）
        阈值 0.12 → 10 簇
        阈值 0.08 →  6 簇（但开始把不相关的东西合进来）

    取 **0.16**：这是"还能看出结构"和"不再乱合"之间的位置。
    这个数字要在答辩时如实说明——它是扫出来的，不是理论最优，
    换数据集必须重扫。

    ## 结果不可全信，所以算纯度并如实报告

    0.16 下的簇人工核了一遍，约四成是对的（漏水→漏水、垃圾→垃圾、
    私搭→私搭），其余是被"楼道""单元""老人""物业说""越来越"这类
    高频词串起来的假簇。原因很实在：50–100 字的短文本，去掉虚词和
    地点词之后剩下的实词太少，2-gram 重叠经常为 0。

    所以 `purity` 这个数字一定要下发到页面并显示出来。
    **它不是装饰：它是"这份聚类结果该信几分"的唯一依据。**
    纯度低的时候，页面必须让使用者看到"以词表结论为准"。

    ## 输出的簇必须人工命名

    簇里给出 `top_terms`（簇内平均 TF-IDF 最高的词）和 `topics`
    （命中了词表里的哪些已知主题）。有 `topics` 的簇能直接命名；
    只有 `top_terms` 没有 `topics` 的，是**新主题候选**——
    这类才是聚类的真正价值：找到词表覆盖不到的诉求类型。
    """
    tiles = ResidentTicket.query.order_by(ResidentTicket.created_at).all()
    total = len(tiles)
    if total < MIN_N_FOR_CLUSTER:
        return {
            "available": False,
            "n": total,
            "need": MIN_N_FOR_CLUSTER,
            "reason": (f"当前 {total} 条诉求，低于聚类门槛 {MIN_N_FOR_CLUSTER} 条。"
                       "样本不足时聚类会退化成「每条一个簇」或「全在一个簇」，"
                       "两种结果都无法支撑结论，故不输出。"),
            "fallback": "已改用主题词表归并（见 hotspots）+ 高频字串（见 ngrams）。",
        }

    try:
        import numpy as np
    except Exception:           # 线上环境可能没装 numpy（不在 requirements 里），
        # 聚类是"锦上添花"的分析，hotspots 词表归并才是主线。
        # 缺 numpy 就诚实降级，不能像 openpyxl 那样让接口直接 500。
        return {"available": False, "n": total, "need": MIN_N_FOR_CLUSTER,
                "reason": "运行环境缺少 numpy，无法做 TF-IDF 聚类。",
                "fallback": "见 hotspots()（主题词表归并）+ ngrams（高频字串）。"}

    texts = [_text(t) for t in tiles]
    mat, vocab = _vectorize(texts, n=n, min_docs=min_docs, max_df=max_df)
    if mat is None:
        return {"available": False, "n": total, "need": MIN_N_FOR_CLUSTER,
                "reason": "没有出现频次达标的字串，无法构造特征向量。",
                "fallback": "见 hotspots()。"}

    sim = mat @ mat.T                 # 已 L2 归一化，点积即余弦
    groups = _hac(sim, threshold)

    # 相似度分布一起下发。这个数字比任何"聚类质量良好"的措辞都可信：
    # 中位数 0 就是在说"一半以上的诉求两两之间没有任何共同字串"。
    iu = np.triu_indices(len(texts), 1)
    vals = sim[iu]
    sim_stats = {
        "median": round(float(np.median(vals)), 4) if len(vals) else 0.0,
        "p95": round(float(np.percentile(vals, 95)), 4) if len(vals) else 0.0,
        "max": round(float(vals.max()), 4) if len(vals) else 0.0,
        "mean": round(float(vals.mean()), 4) if len(vals) else 0.0,
        "pairs": int(len(vals)),
    }
    purity = _purity(groups, texts)

    out = []
    for g in groups:
        # 簇内关键词：把成员的 TF-IDF 向量加起来取 top，
        # 用"平均"而不是"和"会让成员多的簇词变散，所以用 sum 再除以成员数
        # 只在比较时无意义——排序不受正数缩放影响，直接用 sum。
        center = mat[g].sum(axis=0)
        idxs = np.argsort(center)[::-1]
        terms = [vocab[i] for i in idxs if center[i] > 0][:8]
        member_topics = Counter()
        for i in g:
            for tp in _hit_topics(texts[i]):
                member_topics[tp] += 1
        # 簇的代表主题：超过一半成员都命中的才算，否则宁可说"新主题候选"。
        # 一个 8 条成员的簇里有 2 条提到"漏水"，不能说这个簇就是漏水问题。
        rep = [t for t, c in member_topics.items() if c * 2 > len(g)]
        out.append({
            "size": len(g),
            "share": round(len(g) / total, 4),
            "topics": rep,
            "top_terms": terms,
            "label": "、".join(rep) if rep else "新主题候选（需人工命名）",
            "is_new": not rep,
            "members": [{
                "id": tiles[i].id, "code": tiles[i].code,
                "text": (tiles[i].raw_text or "")[:70],
                "status": tiles[i].status or "",
                "at": tiles[i].created_at.strftime("%Y-%m-%d") if tiles[i].created_at else "",
            } for i in g],
        })

    return {
        "available": True,
        "n": total,
        "need": MIN_N_FOR_CLUSTER,
        "clusters": len(out),
        "multi_clusters": sum(1 for c in out if c["size"] >= 2),
        "threshold": threshold,
        "features": len(vocab),
        "purity": purity,
        "sim_stats": sim_stats,
        # 纯度分档：页面据此决定显示什么颜色的提示条。
        # 门槛不是国标，是本次实测定的——0.7 以上才敢说"结构清晰"。
        # 实测值 30%，落在"低"，页面会明确提示"以词表结论为准"。
        #
        # 这个数字会随词表改动而变：去掉「老人」这类身份词之前是 39%，
        # 改完是 30%。原因是纯度按"簇内全体成员的主题交集非空"算，
        # 主题集合一变，能判为纯的簇就变。所以文档里写纯度**必须同时写
        # 当时的词表版本与样本量**，单独一个百分数没有可比性。
        "quality": ("高" if purity >= 0.7 else "中" if purity >= 0.5 else "低"),
        "method": "TF-IDF(2-gram) + 余弦相似度 + 平均链接层次聚类",
        "items": out,
        # 诚实标注：这只是"文本相似"，不是"语义相似"。
        # "楼道堆物"和"楼道灯坏"字面很近但主题完全不同——这正是
        # 为什么上面还要叠一层词表判断，而不是只吃聚类结果。
        "limits": ("基于字面 2-gram 相似度，不含语义理解："
                   "「楼道堆物」与「楼道灯坏」字面相近但主题不同，"
                   "「消防通道停车」与「下水道反水」都含「物业说」也会被拉近。"
                   f"本次实测纯度 {purity:.0%}，簇的定性一律以词表命中为准，"
                   "聚类只用于发现「词表没覆盖到的新主题候选」。"),
    }


def summary():
    """热点页顶部卡片。"""
    hs = hotspots()
    cl = cluster()
    return {
        "tickets": hs["total"],
        "topics": len(hs["topics"]),
        "unmatched": hs["unmatched"],
        "top_topic": hs["topics"][0]["topic"] if hs["topics"] else "",
        "top_open": hs["topics"][0]["open"] if hs["topics"] else 0,
        "cluster_available": cl["available"],
        "cluster_purity": cl.get("purity"),
        "cluster_quality": cl.get("quality"),
        "cluster_need": cl["need"],
    }
