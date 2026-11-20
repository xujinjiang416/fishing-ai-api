"""wechat.py — 微信小程序能力（登录 / 支付）

未配置 WECHAT_APPID 时走模拟实现，方便本地联调：
- 登录：code 直接映射成稳定的假 openid（同一 code 永远同一用户）
- 支付：直接返回「已支付」，不做真实下单

配置了 appid/secret 后，把 _real_* 分支的实现补上即可（已留 TODO）。

职责：
- 对外暴露登录换取 openid、支付下单两个入口，屏蔽「真实 / 模拟」差异。

关键数据流：
  wx.login code → code2session → openid/session_key → 业务侧建用户
  订单号 + 金额 → create_payment → 小程序 wx.requestPayment 参数

依赖：
- 微信 code2session（api.weixin.qq.com/sns/jscode2session，待接入）；
- 微信支付 v3 统一下单（待接入）；
- app.core.config.settings：wechat_appid 等。

失败降级策略：
- 未配置 appid 时走模拟实现，保证本地联调可用；
- 配置后真实分支尚未实现，会抛 NotImplementedError，避免静默返回假数据。

注意事项：
- 模拟 openid 由 code 哈希而来，同一 code 永远映射同一用户；
- 切勿在日志中输出 session_key 等敏感凭证。
"""

import hashlib

from ..core.config import settings


def code2session(code: str) -> dict:
    """用 wx.login 的 code 换 openid。返回 { openid, session_key }。

    Args:
        code: 小程序 wx.login 返回的临时登录凭证。

    Returns:
        dict: ``{"openid": str, "session_key": str}``；未配置 appid 时为模拟值。

    Raises:
        NotImplementedError: 已配置 appid 但真实 code2session 尚未接入。
    """
    if not settings.wechat_appid:
        fake = hashlib.md5(f"mock-openid:{code}".encode("utf-8")).hexdigest()[:24]
        return {"openid": f"mock_{fake}", "session_key": "mock_session_key"}

    # TODO: 真实实现：GET https://api.weixin.qq.com/sns/jscode2session
    #   params: appid / secret / js_code=code / grant_type=authorization_code
    #   校验 errcode，失败抛 HTTPException(400, errmsg)
    raise NotImplementedError("请补全微信 code2session 调用")


def create_payment(order_no: str, amount: float, openid: str | None = None) -> dict:
    """统一下单。返回小程序端 wx.requestPayment 需要的参数。

    Args:
        order_no: 业务订单号。
        amount: 支付金额（元）。
        openid: 支付用户 openid，可为空。

    Returns:
        dict: 未配置 appid 时返回 ``{orderNo, amount, mock: True}`` 模拟结果。

    Raises:
        NotImplementedError: 已配置 appid 但真实统一下单尚未接入。
    """
    if not settings.wechat_appid:
        return {"orderNo": order_no, "amount": amount, "mock": True}

    # TODO: 真实实现：调用微信支付 v3 统一下单，返回
    #   { timeStamp, nonceStr, package, signType, paySign }
    raise NotImplementedError("请补全微信支付统一下单")
