r"""关键词搜索：把用户输入安全地变成 LIKE 条件。

## 为什么这里就是 LIKE，而不是 FTS5

docs/17 把"台账搜索用 LIKE、中文不分词"写成一条**取舍**，不是欠账。理由不是
"懒得做"，而是实测出来的三条（证据：`scripts/bench_ledger_search.py`）：

1. **FTS5 默认分词器对中文是坏的**。unicode61 按空白和标点切分，一整句中文
   （"3号楼2单元电梯运行时有异响"）会变成**一个 token**，搜"电梯"命中 0 条。
   这不是慢，是漏——而且漏得毫无提示。1 万条实测：LIKE 命中 824 条，
   unicode61 命中 0 条。
2. **换成 trigram 能匹配中文子串，但 2 字词静默失效**。trigram 要求 ≥3 字符，
   搜"电梯""渗水"返回 0 条，同样不报错。而 2 字词恰恰是中文里最常用的检索词
   （电梯 / 渗水 / 噪音 / 独居）。同时索引体积是正文的 5.7 倍（10.6MB vs 1.9MB）。
3. **真要分词就得上第三方分词器**（jieba 之类），那才是真正破坏本项目
   "零依赖、可整包搬移"这条硬约定。

而 LIKE 的成本在本项目量级完全够用：1 万条 count 3.3ms、5 万条 17.8ms，
取页 0.1ms 量级。按线性外推，撞到"人眼可感知"的 100ms 大约要 28 万条台账——
一个街道社区的台账永远到不了这个量级。

## 既然用 LIKE，就必须把 LIKE 的两件事做对

**一是通配符**。`%` 和 `_` 在 LIKE 里是元字符。用户搜 "%" 时如果直接拼
`LIKE '%%%'`，会命中全表——他要的是"含 % 的记录"，得到的是"所有记录"，
而且界面上看不出异常（有结果、有分页），只会以为"搜 % 就是全部"。这里统一
转义 `\ % _` 三者，并用 `escape='\'` 告诉 SQLite。

**二是多词**。中文用户习惯"电梯 3号楼"这样叠词收窄，而 `LIKE '%电梯 3号楼%'`
把空格也当成匹配内容，必然 0 条，体验上像"搜索坏了"。这里按空白切词、
**逐词 AND**（每个词都必须在某一列里出现），单次最多 4 个词——再多就没有
收窄价值，只是白扫几遍表。
"""
import json
import re

from sqlalchemy import and_, or_

from app.agents.base import mask_sensitive

LIKE_ESCAPE = "\\"
MAX_TERMS = 4        # 叠词超过 4 个基本是"我把整句话粘进来了"，收窄意义为零
MAX_LEN = 40         # 单个词上限，防止超长串把 SQL 参数和日志撑爆

_SPLIT = re.compile(r"[\s\u3000]+")   # 半角空白 + 全角空格（中文输入法常见）


def terms(raw):
    """把用户输入切成检索词：去空白、去重保序、限个数与长度。"""
    if not raw:
        return []
    out = []
    for w in _SPLIT.split(str(raw).strip()):
        w = w.strip()[:MAX_LEN]
        if w and w not in out:
            out.append(w)
        if len(out) >= MAX_TERMS:
            break
    return out


def pattern(word):
    r"""单个词 → LIKE 模式串，转义 `\ % _`。

    顺序要紧：先把反斜杠自身翻倍，再转义 % 和 _。反过来做的话，
    用户输入的 `\%` 会先变成 `\\%`，反斜杠被重复转义，模式串就错了。
    """
    s = str(word).replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    s = s.replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return "%" + s + "%"


def like_any(columns, word):
    """一个词命中任意一列即可。"""
    p = pattern(word)
    return or_(*[c.like(p, escape=LIKE_ESCAPE) for c in columns])


def like_all(columns, raw):
    """多词 AND；没有有效词时返回 None（调用方据此跳过过滤）。"""
    ws = terms(raw)
    if not ws:
        return None
    conds = [like_any(columns, w) for w in ws]
    return conds[0] if len(conds) == 1 else and_(*conds)


def _values(obj, out):
    """递归收集 JSON 里的叶子值，丢掉键名与布尔标记。"""
    if isinstance(obj, dict):
        for v in obj.values():
            _values(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _values(v, out)
    elif isinstance(obj, bool) or obj is None:
        return out                       # "reused_archive: true" 是标记，不是内容
    else:
        # 检索文本是 content 的第二份拷贝，因此必须和审计日志同等脱敏：
        # 原件里有手机号/身份证明文，若原样落进 search_text，
        # 就等于绕开脱敏在库里多留了一份明文（等保检查直接扣分）。
        # 代价是"按手机号搜台账"搜不到——台账本来也不该按号码检索。
        out.append(mask_sensitive(str(obj)))
    return out


def content_text(content):
    """把台账 content 转成可检索的纯文本（不含键名）。

    只拼值不拼键，是为了让"搜 type / 搜下划线命中全部"这类假命中消失；
    嵌在 `fields` 里的值也会被展平进来——居民填的字段恰恰是最常被搜的那些。

    入参既接受 dict 也接受已经序列化好的 JSON 字符串：后者是调用方最顺手的
    形态（写库的就是 `content_json`），在这里统一反序列化，省得每个调用点
    各写一遍 json.loads + try。
    """
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            # 不是 JSON 就当成一段普通文本（早期手工写的台账就是这样）
            return mask_sensitive(content)
    return " ".join(_values(content, []))
