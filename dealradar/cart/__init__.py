"""购物车提醒（S13 / S14，ADR-008）：型号确认 → 商品页 → 读价格 → 建议 → 提醒 → 汇总邮件。只在 Mac 本地跑。

接口（run.py / mac_run 按这个调用）：
    from dealradar.cart import run
    run(final_events, now, settings) -> {"reminders": [...], "digest": {"subject", "body"} | None}
    from dealradar.cart.mail import send_digest
    send_digest(subject, body, preview: bool) -> dict
"""
from .runner import run

__all__ = ["run"]
