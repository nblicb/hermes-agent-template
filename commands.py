"""
Slash command handlers — intercepted in monkey-patch before Hermes agent.
Zero token consumption. Direct DB operations.

Commands:
  /help, /start    — show help text
  /watch           — watchlist management (add/list/remove/clear)
  /alert           — price alert management (add/list/remove)
  /usage           — show daily quota
  /pro             — Pro subscription info
  /subscribe_*     — enable push notifications
  /unsubscribe_*   — disable push notifications
  /notify          — notification settings panel
"""

import os
import re
import datetime
import logging

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _TZ = None

logger = logging.getLogger("commands")


def _now() -> datetime.datetime:
    """Naive UTC now. DB columns are tz-naive UTC (matches V2 semantics)."""
    return datetime.datetime.utcnow()


def _fmt_date(dt: datetime.datetime) -> str:
    """Format a naive-UTC datetime as Asia/Shanghai date (YYYY-MM-DD)."""
    if dt is None:
        return "—"
    try:
        if _TZ is not None:
            aware = dt.replace(tzinfo=datetime.timezone.utc).astimezone(_TZ)
            return aware.strftime("%Y-%m-%d")
    except Exception:
        pass
    return (dt + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")

# ── DB helper ──────────────────────────────────────────────────
_db_conn = None


def _get_db():
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.cursor().execute("SELECT 1")
            return _db_conn
        except Exception:
            _db_conn = None
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    try:
        import psycopg2
        _db_conn = psycopg2.connect(db_url)
        _db_conn.autocommit = True
        return _db_conn
    except Exception as e:
        logger.debug("DB connection failed: %s", e)
        return None


def _is_chinese(text: str, user_id: str = None) -> bool:
    """Detect zh. Reads telegram_notify_settings.lang first, falls back to char scan."""
    if user_id:
        conn = _get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT lang FROM telegram_notify_settings WHERE tg_user_id = %s",
                    (int(user_id),)
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] == "zh"
            except Exception:
                pass
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def _remember_lang(user_id: str, text: str) -> None:
    """Persist lang='zh' once we see a Chinese message. Never overwrites back to 'en'."""
    if not any('\u4e00' <= c <= '\u9fff' for c in text):
        return
    conn = _get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telegram_notify_settings (tg_user_id, lang) VALUES (%s, 'zh') "
            "ON CONFLICT (tg_user_id) DO UPDATE SET lang = 'zh'",
            (int(user_id),)
        )
    except Exception:
        pass


# ── Tier / Quota ───────────────────────────────────────────────
ADMIN_IDS = {"353559286"}
LIMITS_WATCH = {"free": 10, "pro": 30}
LIMITS_ALERT = {"free": 5, "pro": 20}
DAILY_QUOTA = {"free": 20, "pro": 50}


def _get_tier(user_id: str) -> str:
    if user_id in ADMIN_IDS:
        return "admin"
    conn = _get_db()
    if not conn:
        return "free"
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tier, is_active, expires_at FROM tg_subscriptions WHERE tg_user_id = %s ORDER BY created_at DESC LIMIT 1",
            (int(user_id),)
        )
        row = cur.fetchone()
        if row and row[1] and (row[2] is None or row[2] > _now()):
            return row[0] or "free"
    except Exception:
        pass
    return "free"


# ── /help /start ───────────────────────────────────────────────
def handle_help(user_id: str, msg: str) -> str:
    zh = _is_chinese(msg, user_id)
    if zh:
        return (
            "📋 *InvestLog AI 命令*\n\n"
            "💬 直接输入问题即可分析股票\n\n"
            "📌 *自选股*\n"
            "/watch add AAPL — 添加\n"
            "/watch list — 查看\n"
            "/watch remove AAPL — 移除\n\n"
            "🔔 *价格监控*\n"
            "/alert AAPL > 200 — 涨破提醒\n"
            "/alert AAPL < 150 — 跌破提醒\n"
            "/alert list — 查看监控\n\n"
            "📊 /usage — 查看今日用量\n"
            "⭐ /pro — 升级 Pro\n"
            "🔔 /notify — 推送设置"
        )
    return (
        "📋 *InvestLog AI Commands*\n\n"
        "💬 Just type your question to analyze stocks\n\n"
        "📌 *Watchlist*\n"
        "/watch add AAPL — Add\n"
        "/watch list — View\n"
        "/watch remove AAPL — Remove\n\n"
        "🔔 *Price Alerts*\n"
        "/alert AAPL > 200 — Alert above\n"
        "/alert AAPL < 150 — Alert below\n"
        "/alert list — View alerts\n\n"
        "📊 /usage — Daily usage\n"
        "⭐ /pro — Upgrade to Pro\n"
        "🔔 /notify — Push settings"
    )


# ── /watch ─────────────────────────────────────────────────────
def handle_watch(user_id: str, msg: str) -> str:
    zh = _is_chinese(msg, user_id)
    parts = msg.strip().split()
    # /watch or /watch with no args
    if len(parts) <= 1:
        if zh:
            return "📋 *自选股管理*\n\n/watch add AAPL — 添加\n/watch remove AAPL — 移除\n/watch list — 查看\n/watch clear — 清空"
        return "📋 *Watchlist*\n\n/watch add AAPL — Add\n/watch remove AAPL — Remove\n/watch list — View\n/watch clear — Clear"

    sub = parts[1].lower()
    conn = _get_db()
    if not conn:
        return "⚠️ Database unavailable" if not zh else "⚠️ 数据库暂不可用"

    cur = conn.cursor()
    uid = int(user_id)
    tier = _get_tier(user_id)
    limit = 999 if tier == "admin" else LIMITS_WATCH.get(tier, 10)

    if sub == "add" and len(parts) >= 3:
        symbol = parts[2].upper()
        cur.execute("SELECT COUNT(*) FROM tg_watchlist WHERE tg_user_id = %s", (uid,))
        count = cur.fetchone()[0]
        if tier != "admin" and count >= limit:
            return f"⚠️ {'自选股已满' if zh else 'Watchlist full'} ({count}/{limit})"
        cur.execute("SELECT id FROM tg_watchlist WHERE tg_user_id = %s AND symbol = %s", (uid, symbol))
        if cur.fetchone():
            return f"⚠️ {symbol} {'已在自选股中' if zh else 'already in watchlist'}"
        cur.execute("INSERT INTO tg_watchlist (tg_user_id, symbol, added_at) VALUES (%s, %s, NOW())", (uid, symbol))
        return f"✅ {'已添加' if zh else 'Added'} {symbol}"

    elif sub == "remove" and len(parts) >= 3:
        symbol = parts[2].upper()
        cur.execute("DELETE FROM tg_watchlist WHERE tg_user_id = %s AND symbol = %s", (uid, symbol))
        if cur.rowcount:
            return f"✅ {'已移除' if zh else 'Removed'} {symbol}"
        return f"⚠️ {symbol} {'不在自选股中' if zh else 'not in watchlist'}"

    elif sub == "list":
        cur.execute(
            """
            SELECT w.symbol,
                   q.price,
                   COALESCE(q.change_percent,
                       CASE WHEN q.previous_close > 0
                            THEN ((q.price - q.previous_close) / q.previous_close * 100)
                            ELSE 0 END,
                       0)::float AS chg
            FROM tg_watchlist w
            LEFT JOIN LATERAL (
                SELECT price, change_percent, previous_close
                FROM quote_snapshot
                WHERE symbol = w.symbol AND volume IS NOT NULL
                ORDER BY snap_date DESC LIMIT 1
            ) q ON true
            WHERE w.tg_user_id = %s
            ORDER BY w.added_at
            """,
            (uid,)
        )
        rows = cur.fetchall()
        if not rows:
            return f"📋 {'自选股为空' if zh else 'Watchlist empty'}\n\n/watch add AAPL"
        count = len(rows)
        lines = []
        for symbol, price, chg in rows:
            if price is None:
                lines.append(f"⚪ {symbol}  —")
                continue
            emoji = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
            lines.append(f"{emoji} {symbol}  ${float(price):.2f}  {chg:+.2f}%")
        header = f"📋 {'自选股' if zh else 'Watchlist'} ({count}/{limit})"
        return f"{header}\n\n" + "\n".join(lines)

    elif sub == "clear":
        cur.execute("DELETE FROM tg_watchlist WHERE tg_user_id = %s", (uid,))
        return f"✅ {'已清空自选股' if zh else 'Watchlist cleared'}"

    else:
        if zh:
            return "📋 *自选股管理*\n\n/watch add AAPL — 添加\n/watch remove AAPL — 移除\n/watch list — 查看\n/watch clear — 清空"
        return "📋 *Watchlist*\n\n/watch add AAPL — Add\n/watch remove AAPL — Remove\n/watch list — View\n/watch clear — Clear"


# ── /alert ─────────────────────────────────────────────────────
_RE_PRICE = re.compile(r"^([A-Za-z0-9.\-^]+)\s*([><])\s*(\d+(?:\.\d+)?)$")
_RE_PCT = re.compile(r"^([A-Za-z0-9.\-^]+)\s*([+-])(\d+(?:\.\d+)?)%$")


def handle_alert(user_id: str, msg: str) -> str:
    zh = _is_chinese(msg, user_id)
    raw = msg.strip()
    after = raw.split(None, 1)[1] if " " in raw else ""
    after = after.strip()

    if not after:
        if zh:
            return "🔔 *价格监控*\n\n/alert AAPL > 200 — 涨破提醒\n/alert AAPL < 150 — 跌破提醒\n/alert AAPL +5% — 涨幅提醒\n/alert list — 查看\n/alert remove 3 — 删除"
        return "🔔 *Price Alerts*\n\n/alert AAPL > 200 — Above\n/alert AAPL < 150 — Below\n/alert AAPL +5% — Daily change\n/alert list — View\n/alert remove 3 — Delete"

    conn = _get_db()
    if not conn:
        return "⚠️ Database unavailable" if not zh else "⚠️ 数据库暂不可用"

    cur = conn.cursor()
    uid = int(user_id)
    tier = _get_tier(user_id)
    limit = 999 if tier == "admin" else LIMITS_ALERT.get(tier, 5)

    lower = after.lower()
    if lower == "list":
        cur.execute(
            """
            SELECT a.id, a.symbol, a.condition_type, a.target_value,
                   q.price,
                   COALESCE(q.change_percent,
                       CASE WHEN q.previous_close > 0
                            THEN ((q.price - q.previous_close) / q.previous_close * 100)
                            ELSE 0 END,
                       0)::float AS chg
            FROM tg_price_alerts a
            LEFT JOIN LATERAL (
                SELECT price, change_percent, previous_close
                FROM quote_snapshot
                WHERE symbol = a.symbol AND volume IS NOT NULL
                ORDER BY snap_date DESC LIMIT 1
            ) q ON true
            WHERE a.tg_user_id = %s AND a.is_active = true
            ORDER BY a.created_at
            """,
            (uid,)
        )
        rows = cur.fetchall()
        if not rows:
            return f"📋 {'暂无监控' if zh else 'No active alerts'}\n\n/alert AAPL > 200"
        lines = []
        for i, (_id, symbol, ctype, target, price, chg) in enumerate(rows, 1):
            target = float(target)
            p = float(price) if price is not None else None
            if ctype == "price_above":
                cond_str = f"> ${target:g}"
                dist = f"  ({((target-p)/p*100):+.1f}%)" if p else ""
                line = f"#{i}  {symbol}  {cond_str}{dist}"
            elif ctype == "price_below":
                cond_str = f"< ${target:g}"
                dist = f"  ({((target-p)/p*100):+.1f}%)" if p else ""
                line = f"#{i}  {symbol}  {cond_str}{dist}"
            elif ctype == "change_pct_up":
                now = f"  ({'当前' if zh else 'now'} {chg:+.2f}%)" if p else ""
                line = f"#{i}  {symbol}  +{target:g}%{now}"
            elif ctype == "change_pct_down":
                now = f"  ({'当前' if zh else 'now'} {chg:+.2f}%)" if p else ""
                line = f"#{i}  {symbol}  -{target:g}%{now}"
            else:
                line = f"#{i}  {symbol}  ?"
            lines.append(line)
        count = len(rows)
        header = f"🔔 {'监控列表' if zh else 'Alerts'} ({count}/{limit})"
        footer = f"\n\n/alert remove N — {'删除' if zh else 'remove'}"
        return f"{header}\n\n" + "\n".join(lines) + footer

    elif lower.startswith("remove"):
        parts = after.split()
        if len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            if n < 1:
                return "⚠️ /alert remove N（N >= 1）" if zh else "⚠️ /alert remove N (N >= 1)"
            cur.execute(
                "SELECT id, symbol FROM tg_price_alerts WHERE tg_user_id = %s AND is_active = true ORDER BY created_at OFFSET %s LIMIT 1",
                (uid, n - 1)
            )
            row = cur.fetchone()
            if not row:
                return f"⚠️ {'未找到监控' if zh else 'Alert not found'} #{n}"
            alert_id, symbol = row
            cur.execute("DELETE FROM tg_price_alerts WHERE id = %s AND tg_user_id = %s", (alert_id, uid))
            if cur.rowcount:
                return f"✅ {'已删除监控' if zh else 'Alert removed'} #{n} {symbol}"
            return f"⚠️ {'未找到监控' if zh else 'Alert not found'} #{n}"
        return "⚠️ /alert remove N" if not zh else "⚠️ 用法：/alert remove N"

    else:
        # Parse condition
        m = _RE_PRICE.match(after)
        if m:
            symbol, op, value = m.group(1).upper(), m.group(2), float(m.group(3))
            ctype = "price_above" if op == ">" else "price_below"
        else:
            m = _RE_PCT.match(after)
            if m:
                symbol, sign, value = m.group(1).upper(), m.group(2), float(m.group(3))
                ctype = "change_pct_up" if sign == "+" else "change_pct_down"
            else:
                return f"⚠️ {'格式错误' if zh else 'Invalid format'}\n\n/alert AAPL > 200"

        cur.execute("SELECT COUNT(*) FROM tg_price_alerts WHERE tg_user_id = %s AND is_active = true", (uid,))
        count = cur.fetchone()[0]
        if tier != "admin" and count >= limit:
            return f"⚠️ {'监控已满' if zh else 'Alert limit reached'} ({count}/{limit})"

        cur.execute(
            "INSERT INTO tg_price_alerts (tg_user_id, symbol, condition_type, target_value, is_active, created_at) VALUES (%s, %s, %s, %s, true, NOW())",
            (uid, symbol, ctype, value)
        )
        desc = f"> ${value:g}" if ctype == "price_above" else f"< ${value:g}" if ctype == "price_below" else f"+{value:g}%" if ctype == "change_pct_up" else f"-{value:g}%"
        return f"✅ {'已添加监控' if zh else 'Alert set'}: {symbol} {desc}"


# ── /usage ─────────────────────────────────────────────────────
def handle_usage(user_id: str, msg: str) -> str:
    zh = _is_chinese(msg, user_id)
    tier = _get_tier(user_id)
    limit = 999 if tier == "admin" else DAILY_QUOTA.get(tier, 20)
    tier_label = {"admin": "Admin", "pro": "Pro", "free": "Free"}.get(tier, tier)

    # Count today's usage from il_chat_usage
    used = 0
    conn = _get_db()
    if conn:
        try:
            cur = conn.cursor()
            today = datetime.date.today().isoformat()
            cur.execute(
                "SELECT COUNT(*) FROM il_chat_usage WHERE user_id = %s AND used_date = %s AND error IS NULL",
                (f"tg:{user_id}", today)
            )
            used = cur.fetchone()[0]
        except Exception:
            pass

    remaining = max(0, limit - used)
    if zh:
        text = f"📊 *今日用量*\n\n等级：{tier_label}\n已用：{used}/{limit}\n剩余：{remaining}"
        if tier == "free":
            text += "\n\n发送 /pro 升级到每日 50 次"
    else:
        text = f"📊 *Daily Usage*\n\nTier: {tier_label}\nUsed: {used}/{limit}\nRemaining: {remaining}"
        if tier == "free":
            text += "\n\nSend /pro to upgrade to 50/day"
    return text


# ── /pro ───────────────────────────────────────────────────────
PRO_STARS_PRICE = 500
PRO_PAYLOAD = "sub_pro"


def handle_pro(user_id: str, msg: str):
    """Return status text if already Pro, else invoice dict for Stars checkout."""
    zh = _is_chinese(msg, user_id)

    # Check if already Pro
    conn = _get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT expires_at FROM tg_subscriptions WHERE tg_user_id = %s AND is_active = true AND tier = 'pro' ORDER BY expires_at DESC LIMIT 1",
                (int(user_id),)
            )
            row = cur.fetchone()
            if row and row[0] and row[0] > _now():
                exp = _fmt_date(row[0])
                if zh:
                    return f"✅ 你已是 Pro 用户\n有效期至 {exp}"
                return f"✅ You are already Pro\nValid until {exp}"
        except Exception:
            pass

    if zh:
        promo = (
            "⭐ *InvestLog Pro*\n\n"
            "升级 Pro 解锁：\n"
            "• 每日 50 次 AI 分析（Free 20 次）\n"
            "• 自选股上限 30 只（Free 10 只）\n"
            "• 价格监控 20 个（Free 5 个）\n\n"
            f"价格：{PRO_STARS_PRICE} Stars/月"
        )
        title = f"InvestLog Pro — {PRO_STARS_PRICE} Stars/月"
        desc = "AI 投资助手 Pro 会员，有效期 30 天"
    else:
        promo = (
            "⭐ *InvestLog Pro*\n\n"
            "Upgrade to Pro:\n"
            "• 50 AI analyses/day (Free: 20)\n"
            "• 30 watchlist slots (Free: 10)\n"
            "• 20 price alerts (Free: 5)\n\n"
            f"Price: {PRO_STARS_PRICE} Stars/month"
        )
        title = f"InvestLog Pro — {PRO_STARS_PRICE} Stars/mo"
        desc = "AI Investment Assistant Pro, 30-day access"

    return {
        "type": "invoice",
        "promo_text": promo,
        "title": title,
        "description": desc,
        "payload": PRO_PAYLOAD,
        "currency": "XTR",
        "prices": [("InvestLog Pro", PRO_STARS_PRICE)],
    }


# ── Payment: pre_checkout + successful_payment ─────────────────
def pre_checkout_ok(payload: str) -> bool:
    """Validate invoice payload before charging user."""
    return payload == PRO_PAYLOAD


def activate_pro_subscription(
    user_id: str,
    payment_charge_id: str,
    provider_charge_id: str,
    amount: int,
    payload: str,
) -> str:
    """Persist payment + renew Pro subscription. Returns confirmation text."""
    zh_flag = False
    conn = _get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT lang FROM telegram_notify_settings WHERE tg_user_id = %s",
                (int(user_id),)
            )
            row = cur.fetchone()
            zh_flag = bool(row and row[0] == "zh")
        except Exception:
            pass

    if payload != PRO_PAYLOAD:
        logger.warning("unknown payment payload: %s (user=%s)", payload, user_id)
        return (
            "⚠️ 未知的支付类型，请联系管理员。" if zh_flag
            else "⚠️ Unknown payment. Contact admin."
        )

    if not conn:
        return (
            "✅ 支付已收到，但数据库暂不可用，请联系管理员。"
            if zh_flag else
            "✅ Payment received but DB unavailable. Contact admin."
        )

    now = _now()
    expires = now + datetime.timedelta(days=30)

    try:
        cur = conn.cursor()
        uid = int(user_id)
        # Record payment
        cur.execute(
            "INSERT INTO tg_payments (tg_user_id, payment_charge_id, provider_charge_id, amount, payload, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'completed', NOW())",
            (uid, payment_charge_id or "", provider_charge_id or "", amount, payload)
        )

        # Stack renewal: if active sub exists, extend from current expiry
        cur.execute(
            "SELECT id, expires_at FROM tg_subscriptions WHERE tg_user_id = %s AND is_active = true ORDER BY expires_at DESC LIMIT 1",
            (uid,)
        )
        row = cur.fetchone()
        if row:
            sub_id, cur_exp = row
            if cur_exp and cur_exp > now:
                expires = cur_exp + datetime.timedelta(days=30)
            cur.execute(
                "UPDATE tg_subscriptions SET tier='pro', stars_payment_id=%s, expires_at=%s, updated_at=NOW() WHERE id=%s",
                (payment_charge_id or "", expires, sub_id)
            )
        else:
            cur.execute(
                "INSERT INTO tg_subscriptions (tg_user_id, tier, stars_payment_id, expires_at, is_active, started_at, created_at) "
                "VALUES (%s, 'pro', %s, %s, true, NOW(), NOW())",
                (uid, payment_charge_id or "", expires)
            )

        # Ensure notify_settings row exists (harmless if already)
        cur.execute(
            "INSERT INTO telegram_notify_settings (tg_user_id) VALUES (%s) ON CONFLICT (tg_user_id) DO NOTHING",
            (uid,)
        )

        exp_str = _fmt_date(expires)
        logger.info("Pro activated user=%s expires=%s charge=%s", user_id, exp_str, payment_charge_id)
        if zh_flag:
            return f"✅ Pro 已激活！感谢支持 🎉\n有效期至 {exp_str}"
        return f"✅ Pro activated! Thank you 🎉\nValid until {exp_str}"
    except Exception as e:
        logger.error("activate_pro_subscription failed user=%s: %s", user_id, e)
        if zh_flag:
            return "⚠️ 支付已收到，但激活失败，请联系管理员。"
        return "⚠️ Payment received but activation failed. Contact admin."


# ── /notify /subscribe /unsubscribe ────────────────────────────
def handle_notify(user_id: str, msg: str) -> str:
    zh = _is_chinese(msg, user_id)
    conn = _get_db()
    if not conn:
        return "⚠️ Database unavailable"

    cur = conn.cursor()
    uid = int(user_id)
    cur.execute("SELECT earnings_alert, market_digest FROM telegram_notify_settings WHERE tg_user_id = %s", (uid,))
    row = cur.fetchone()
    earnings = row[0] if row else True
    digest = row[1] if row else True

    e_icon = "✅" if earnings else "❌"
    d_icon = "✅" if digest else "❌"

    if zh:
        return (
            f"🔔 *推送通知设置*\n\n"
            f"📅 财报推送：{e_icon}\n"
            f"📊 每日简报：{d_icon}\n\n"
            f"/subscribe\\_earnings — 开启财报推送\n"
            f"/unsubscribe\\_earnings — 关闭财报推送\n"
            f"/subscribe\\_digest — 开启每日简报\n"
            f"/unsubscribe\\_digest — 关闭每日简报"
        )
    return (
        f"🔔 *Push Notification Settings*\n\n"
        f"📅 Earnings Alerts: {e_icon}\n"
        f"📊 Daily Digest: {d_icon}\n\n"
        f"/subscribe\\_earnings — Enable earnings alerts\n"
        f"/unsubscribe\\_earnings — Disable earnings alerts\n"
        f"/subscribe\\_digest — Enable daily digest\n"
        f"/unsubscribe\\_digest — Disable daily digest"
    )


def handle_subscribe(user_id: str, msg: str, field: str, enable: bool) -> str:
    zh = _is_chinese(msg, user_id)
    conn = _get_db()
    if not conn:
        return "⚠️ Database unavailable"

    cur = conn.cursor()
    uid = int(user_id)

    # Upsert
    cur.execute("SELECT tg_user_id FROM telegram_notify_settings WHERE tg_user_id = %s", (uid,))
    if cur.fetchone():
        cur.execute(f"UPDATE telegram_notify_settings SET {field} = %s WHERE tg_user_id = %s", (enable, uid))
    else:
        cur.execute(f"INSERT INTO telegram_notify_settings (tg_user_id, {field}) VALUES (%s, %s)", (uid, enable))

    labels = {
        "earnings_alert": ("财报推送", "earnings alerts"),
        "market_digest": ("每日简报", "daily digest"),
    }
    label_zh, label_en = labels.get(field, (field, field))
    action_zh = "已开启" if enable else "已关闭"
    action_en = "Enabled" if enable else "Disabled"

    if zh:
        return f"✅ {action_zh}{label_zh}"
    return f"✅ {action_en} {label_en}"


# ── Command dispatcher ─────────────────────────────────────────
COMMAND_MAP = {
    "/help": handle_help,
    "/start": handle_help,
    "/watch": handle_watch,
    "/alert": handle_alert,
    "/usage": handle_usage,
    "/pro": handle_pro,
    "/subscribe": handle_pro,  # alias
    "/notify": handle_notify,
}


def dispatch_command(user_id: str, msg: str) -> str | None:
    """Try to handle a slash command. Returns response text, or None if not a command."""
    text = (msg or "").strip()
    if user_id and text:
        _remember_lang(user_id, text)
    if not text.startswith("/"):
        return None

    # Extract command (first word, lowercase)
    cmd = text.split()[0].lower().split("@")[0]  # strip @botname

    # Direct map
    handler = COMMAND_MAP.get(cmd)
    if handler:
        try:
            return handler(user_id, text)
        except Exception as e:
            logger.error("Command %s failed: %s", cmd, e)
            zh = _is_chinese(text, user_id)
            return "⚠️ 命令执行出错，请重试。" if zh else "⚠️ Command error, please try again."

    # Subscribe/unsubscribe variants
    if cmd == "/subscribe_earnings":
        return handle_subscribe(user_id, text, "earnings_alert", True)
    elif cmd == "/unsubscribe_earnings":
        return handle_subscribe(user_id, text, "earnings_alert", False)
    elif cmd == "/subscribe_digest":
        return handle_subscribe(user_id, text, "market_digest", True)
    elif cmd == "/unsubscribe_digest":
        return handle_subscribe(user_id, text, "market_digest", False)

    # Not our command — let Hermes handle it
    return None
