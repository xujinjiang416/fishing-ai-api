"""wechat.py — 微信小程序能力（登录 / 支付）

未配置 WECHAT_APPID 时走模拟实现，方便本地联调：
- 登录：code 直接映射成稳定的假 openid（同一 code 永远同一用户）
- 支付：直接返回「已支付」，不做真实下单

配置了 appid/secret 后，把 _real_* 分支的实现补上即可（已留 TODO）。
"""

import hashlib

from ..core.config import settings


def code2session(code: str) -> dict:
    """用 wx.login 的 code 换 openid。返回 { openid, session_key }。"""
    if not settings.wechat_appid:
        fake = hashlib.md5(f"mock-openid:{code}".encode("utf-8")).hexdigest()[:24]
        return {"openid": f"mock_{fake}", "session_key": "mock_session_key"}

    # TODO: 真实实现：GET https://api.weixin.qq.com/sns/jscode2session
    #   params: appid / secret / js_code=code / grant_type=authorization_code
    #   校验 errcode，失败抛 HTTPException(400, errmsg)
    raise NotImplementedError("请补全微信 code2session 调用")


def create_payment(order_no: str, amount: float, openid: str | None = None) -> dict:
    """统一下单。返回小程序端 wx.requestPayment 需要的参数。"""
    if not settings.wechat_appid:
        return {"orderNo": order_no, "amount": amount, "mock": True}

    # TODO: 真实实现：调用微信支付 v3 统一下单，返回
    #   { timeStamp, nonceStr, package, signType, paySign }
    raise NotImplementedError("请补全微信支付统一下单")
