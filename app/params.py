"""URL 参数取值与校验

页面之间传递"我从哪一屏来的"需要一个回跳参数。这类参数直接拼进 href，
一旦不校验就是开放重定向——攻击者可以把"返回列表"做成跳到钓鱼站的按钮。
所以凡是从 query string 里取出来要拼进链接的值，都必须过这里。
"""
from flask import request


def safe_back(default: str) -> str:
    r"""解析 ?back=，只放行站内相对路径

    为什么需要它：列表页点进详情后，"返回"应该回到**当时那一屏**——
    筛选条件、滚动位置都在那个 URL 里。单纯用 history.back() 有两个问题：
    从书签、通知或搜索点进来时根本没有上一页，history.back() 会把人送出站外；
    而且列表是异步渲染的，浏览器后退常常恢复成一个空白列表。
    所以把来源 URL 当参数传下去，返回时原样回去。

    只接受以单个 "/" 开头的路径，拒绝：
      "//evil.com"      协议相对 URL，浏览器会当成外站
      "https://..."     绝对地址（不以 / 开头，第一道就拦了）
      "javascript:..."  伪协议（同上）
      "/\\evil.com"     反斜杠路径混淆（老 IE 会把 \ 当 /）
    """
    b = (request.args.get("back") or "").strip()
    if not b.startswith("/") or b.startswith("//"):
        return default
    # 反斜杠要单独拦：浏览器会把 "/\evil.com" 当成 "//evil.com" 处理
    if "\\" in b:
        return default
    return b
