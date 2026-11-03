"""统一 API 返回格式"""


def ok(data=None, msg="ok"):
    return {"code": 0, "msg": msg, "data": data}


def fail(msg="error", code=1):
    return {"code": code, "msg": msg, "data": None}
