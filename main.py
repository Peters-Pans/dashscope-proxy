"""
阿里云 DashScope 代理
固定周期窗口：5H(整点对齐) / 自然周(周一重置) / 自然月(1号重置)
支持 OpenAI 协议（兼容 OpenAI 工具）和 Anthropic 协议（Claude Code）
"""

import os, uuid, time, json, secrets, string, calendar
import datetime

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from redis.asyncio import Redis
from pydantic import BaseModel, Field, ConfigDict

# ─────────────────────────────────────────
#  默认配额（每个子Key）
# ─────────────────────────────────────────
DEFAULT_LIMITS = {"5h": 1_500, "week": 11_250, "month": 22_500}

# ─────────────────────────────────────────
#  Pydantic 校验模型
# ─────────────────────────────────────────
class LimitsUpdate(BaseModel):
    """配额限制更新"""
    model_config = ConfigDict(populate_by_name=True)

    month: int | None = Field(None, ge=0, le=1_000_000_000, description="月限额")
    week: int | None = Field(None, ge=0, le=500_000_000, description="周限额")
    five_hour: int | None = Field(None, alias="5h", ge=0, le=100_000_000, description="5小时限额")


class LabelUpdate(BaseModel):
    """用户标签更新"""
    label: str = Field(..., max_length=100, description="用户名称")
    note: str = Field("", max_length=1000, description="备注")


class UsageUpdate(BaseModel):
    """用量更新"""
    model_config = ConfigDict(populate_by_name=True)

    month: int | None = Field(None, ge=0, le=1_000_000_000, description="月用量")
    week: int | None = Field(None, ge=0, le=500_000_000, description="周用量")
    five_hour: int | None = Field(None, alias="5h", ge=0, le=100_000_000, description="5小时用量")

# ─────────────────────────────────────────
#  Coding Plan 模型白名单
# ─────────────────────────────────────────

# OpenAI 协议白名单：调用这些模型才计入配额，其他模型直接透传不扣额
PLAN_MODELS: set[str] = {
    "qwen3.5-plus",
    "qwen3-max-2026-01-23",
    "qwen3-coder-next",
    "qwen3-coder-plus",
    "MiniMax-M2.5",
    "glm-5",
    "glm-4.7",
    "kimi-k2.5",
}

# Anthropic 协议白名单：Claude Code 使用，计入同一套配额
# 根据百炼 Coding Plan 实际支持的模型填写，其他模型直接透传不扣额
ANTHROPIC_PLAN_MODELS: set[str] = {
    "claude-sonnet-4-5",
    "claude-opus-4-5",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
}

# ─────────────────────────────────────────
#  上游地址 & 认证
# ─────────────────────────────────────────
ALIYUN_BASE      = os.getenv("ALIYUN_BASE_URL",      "https://coding.dashscope.aliyuncs.com/v1")
ANTHROPIC_BASE   = os.getenv("ANTHROPIC_BASE",        "https://coding.dashscope.aliyuncs.com/apps/anthropic")
ALIYUN_KEY       = os.getenv("ALIYUN_API_KEY",        "")

_admin_token_raw = os.getenv("ADMIN_TOKEN", "change-me")
if _admin_token_raw == "change-me":
    raise RuntimeError("启动失败：请在环境变量中设置 ADMIN_TOKEN，不能使用默认值 'change-me'")
ADMIN_TOKEN = _admin_token_raw

# ─────────────────────────────────────────
#  固定周期 Key 计算
# ─────────────────────────────────────────

def period_info(kid: str) -> dict:
    """返回当前周期的 Redis key、TTL（EXPIREAT时间戳）、重置时刻"""
    now = datetime.datetime.now()

    # ── 5小时块：00-04, 05-09, 10-14, 15-19, 20-23 ──
    slot = now.hour // 5
    date_s = now.strftime("%Y%m%d")
    next_slot_hour = (slot + 1) * 5
    if next_slot_hour >= 24:
        next_reset_5h = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    else:
        next_reset_5h = now.replace(
            hour=next_slot_hour, minute=0, second=0, microsecond=0)

    # ── 自然周：下周一 00:00 ──
    days_to_monday = 7 - now.weekday()  # weekday(): Mon=0 … Sun=6
    next_monday = (now + datetime.timedelta(days=days_to_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    iso = now.isocalendar()

    # ── 自然月：下月1号 00:00 ──
    if now.month == 12:
        next_month_dt = datetime.datetime(now.year + 1, 1, 1)
    else:
        next_month_dt = datetime.datetime(now.year, now.month + 1, 1)

    return {
        "5h": {
            "key":       f"quota:5h:{kid}:{date_s}:{slot}",
            "expire_at": int(next_reset_5h.timestamp()),
            "reset_at":  next_reset_5h.strftime("%Y-%m-%d %H:%M"),
            "label":     f"{slot*5:02d}:00–{min((slot+1)*5,24):02d}:00",
        },
        "week": {
            "key":       f"quota:week:{kid}:{iso[0]}W{iso[1]:02d}",
            "expire_at": int(next_monday.timestamp()),
            "reset_at":  next_monday.strftime("%Y-%m-%d %H:%M"),
            "label":     f"第{iso[1]}周",
        },
        "month": {
            "key":       f"quota:month:{kid}:{now.strftime('%Y%m')}",
            "expire_at": int(next_month_dt.timestamp()),
            "reset_at":  next_month_dt.strftime("%Y-%m-%d %H:%M"),
            "label":     now.strftime("%Y年%m月"),
        },
    }

# ─────────────────────────────────────────
#  Lua
# ─────────────────────────────────────────

LUA_CHECK = """
local k5h  = KEYS[1]; local kw  = KEYS[2]; local km  = KEYS[3]
local l5h  = tonumber(ARGV[1]); local lw = tonumber(ARGV[2]); local lm = tonumber(ARGV[3])
local e5h  = tonumber(ARGV[4]); local ew = tonumber(ARGV[5]); local em = tonumber(ARGV[6])

local cm = tonumber(redis.call('GET', km)) or 0
if cm >= lm then return 'MONTH_LIMIT' end
local cw = tonumber(redis.call('GET', kw)) or 0
if cw >= lw then return 'WEEK_LIMIT' end
local c5h = tonumber(redis.call('GET', k5h)) or 0
if c5h >= l5h then return '5H_LIMIT' end

local nm = redis.call('INCR', km)
if nm == 1 then redis.call('EXPIREAT', km, em) end
local nw = redis.call('INCR', kw)
if nw == 1 then redis.call('EXPIREAT', kw, ew) end
local n5h = redis.call('INCR', k5h)
if n5h == 1 then redis.call('EXPIREAT', k5h, e5h) end
return 'OK'
"""

LUA_ROLLBACK = """
local function decr_safe(k)
    local v = tonumber(redis.call('GET', k)) or 0
    if v > 0 then redis.call('DECR', k) end
end
decr_safe(KEYS[1]); decr_safe(KEYS[2]); decr_safe(KEYS[3])
return 'OK'
"""

# ─────────────────────────────────────────
#  App
# ─────────────────────────────────────────
app = FastAPI(title="DashScope Proxy", docs_url=None, redoc_url=None)
rdb: Redis = None


@app.on_event("startup")
async def startup():
    global rdb
    rdb = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    for i in range(1, 5):
        kid = f"k{i}"
        if not await rdb.exists(f"key:meta:{kid}"):
            secret = os.getenv(f"KEY_{i}", "sk-sub-" + "".join(
                secrets.choice(string.ascii_lowercase + string.digits) for _ in range(16)))
            meta = {
                "kid": kid, "label": f"用户{i}", "secret": secret,
                "enabled": True, "limits": {"5h": None, "week": None, "month": None},
                "note": "", "created_at": int(time.time()),
            }
            await rdb.set(f"key:meta:{kid}", json.dumps(meta, ensure_ascii=False))
    await _rebuild_secret_map()


async def _rebuild_secret_map():
    mapping = {}
    for i in range(1, 5):
        raw = await rdb.get(f"key:meta:k{i}")
        if raw:
            m = json.loads(raw)
            mapping[m["secret"]] = m["kid"]
    if mapping:
        await rdb.delete("map:secret")
        await rdb.hset("map:secret", mapping=mapping)


async def _get_meta(kid: str) -> dict | None:
    raw = await rdb.get(f"key:meta:{kid}")
    return json.loads(raw) if raw else None


async def _save_meta(meta: dict):
    await rdb.set(f"key:meta:{meta['kid']}", json.dumps(meta, ensure_ascii=False))
    await _rebuild_secret_map()


def _limits(meta: dict) -> dict:
    return {k: (meta["limits"].get(k) or DEFAULT_LIMITS[k]) for k in ("5h", "week", "month")}


async def _usage(kid: str) -> dict:
    pi = period_info(kid)
    result = {}
    for dim in ("5h", "week", "month"):
        val = await rdb.get(pi[dim]["key"])
        result[dim] = int(val) if val else 0
    return result


# ─────────────────────────────────────────
#  静态页面
# ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/_panel/admin")
async def admin_page(): return FileResponse("static/admin.html")

@app.get("/_panel/usage")
async def user_page(): return FileResponse("static/user.html")


# ─────────────────────────────────────────
#  管理 API
# ─────────────────────────────────────────
def _check_admin(request: Request):
    if request.headers.get("X-Admin-Token", "") != ADMIN_TOKEN:
        raise HTTPException(status_code=403)


def _mask_secret(secret: str) -> str:
    """脱敏显示 secret：sk-sub-****xxxx（保留前缀和后4位）"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:7]}****{secret[-4:]}"


@app.get("/_admin/keys")
async def admin_list_keys(request: Request):
    _check_admin(request)
    result = []
    for i in range(1, 5):
        meta = await _get_meta(f"k{i}")
        if not meta: continue
        lims = _limits(meta)
        used = await _usage(meta["kid"])
        pi   = period_info(meta["kid"])
        result.append({
            **{k: v for k, v in meta.items() if k != "secret"},  # 排除完整 secret
            "secret_preview": _mask_secret(meta["secret"]),      # 返回脱敏版本
            "limits_effective": lims,
            "usage": used,
            "pct":   {k: round(used[k] / lims[k] * 100, 1) for k in lims},
            "period_info": {k: pi[k]["reset_at"] for k in pi},
        })
    return result


@app.post("/_admin/keys/{kid}/toggle")
async def admin_toggle(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["enabled"] = not meta["enabled"]
    await _save_meta(meta)
    return {"kid": kid, "enabled": meta["enabled"]}


@app.post("/_admin/keys/{kid}/regenerate")
async def admin_regenerate(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["secret"] = "sk-sub-" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
    await _save_meta(meta)
    return {"kid": kid, "secret": meta["secret"]}


@app.get("/_admin/keys/{kid}/secret")
async def admin_reveal_secret(kid: str, request: Request):
    """获取完整 secret（敏感操作，需要 admin token）"""
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    return {"kid": kid, "secret": meta["secret"]}


@app.put("/_admin/keys/{kid}/limits")
async def admin_set_limits(kid: str, body: LimitsUpdate, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    # 使用 Pydantic 模型的字段名映射
    if body.month is not None:
        meta["limits"]["month"] = body.month
    if body.week is not None:
        meta["limits"]["week"] = body.week
    if body.five_hour is not None:
        meta["limits"]["5h"] = body.five_hour
    await _save_meta(meta)
    return {"kid": kid, "limits": meta["limits"], "effective": _limits(meta)}


@app.put("/_admin/keys/{kid}/label")
async def admin_set_label(kid: str, body: LabelUpdate, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    meta["label"] = body.label
    meta["note"] = body.note
    await _save_meta(meta)
    return {"kid": kid, "label": meta["label"], "note": meta["note"]}


@app.put("/_admin/keys/{kid}/usage")
async def admin_set_usage(kid: str, body: UsageUpdate, request: Request):
    """手动修改当前周期的已用量"""
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    pi = period_info(kid)
    if body.month is not None:
        await rdb.set(pi["month"]["key"], body.month)
        await rdb.expireat(pi["month"]["key"], pi["month"]["expire_at"])
    if body.week is not None:
        await rdb.set(pi["week"]["key"], body.week)
        await rdb.expireat(pi["week"]["key"], pi["week"]["expire_at"])
    if body.five_hour is not None:
        await rdb.set(pi["5h"]["key"], body.five_hour)
        await rdb.expireat(pi["5h"]["key"], pi["5h"]["expire_at"])
    return {"kid": kid, "usage": await _usage(kid)}


@app.delete("/_admin/keys/{kid}/reset-usage")
async def admin_reset_usage(kid: str, request: Request):
    _check_admin(request)
    meta = await _get_meta(kid)
    if not meta: raise HTTPException(404)
    pi = period_info(kid)
    for dim in ("5h", "week", "month"):
        await rdb.delete(pi[dim]["key"])
    return {"kid": kid, "reset": True}


# ─────────────────────────────────────────
#  用户用量 API
# ─────────────────────────────────────────
@app.get("/_usage")
async def user_usage(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401)
    secret = auth.removeprefix("Bearer ").strip()
    kid = await rdb.hget("map:secret", secret)
    if not kid: raise HTTPException(401, "Invalid key")
    meta = await _get_meta(kid)
    lims = _limits(meta)
    used = await _usage(kid)
    pi   = period_info(kid)
    return {
        "label": meta["label"], "kid": kid, "secret": secret,
        "enabled": meta["enabled"],
        "limits": lims, "usage": used,
        "pct":    {k: round(used[k] / lims[k] * 100, 1) for k in lims},
        "reset_at": {k: pi[k]["reset_at"] for k in pi},
        "period_label": {k: pi[k]["label"] for k in pi},
        "updated_at": int(time.time()),
    }


# ─────────────────────────────────────────
#  配额检查公共逻辑
# ─────────────────────────────────────────
async def _check_and_deduct_quota(kid: str, meta: dict) -> tuple:
    """检查并扣除配额，返回 (k5h, kw, km) 用于回滚。配额不足时抛出 HTTPException。"""
    lims = _limits(meta)
    pi   = period_info(kid)
    k5h, kw, km = pi["5h"]["key"], pi["week"]["key"], pi["month"]["key"]
    e5h, ew, em = pi["5h"]["expire_at"], pi["week"]["expire_at"], pi["month"]["expire_at"]

    res = await rdb.eval(
        LUA_CHECK, 3, k5h, kw, km,
        lims["5h"], lims["week"], lims["month"],
        e5h, ew, em,
    )
    if res != "OK":
        msgs = {
            "5H_LIMIT":    f"5小时配额已用尽，重置于 {pi['5h']['reset_at']}",
            "WEEK_LIMIT":  f"本周配额已用尽，重置于 {pi['week']['reset_at']}",
            "MONTH_LIMIT": f"本月配额已用尽，重置于 {pi['month']['reset_at']}",
        }
        raise HTTPException(429, msgs.get(res, res))
    return (k5h, kw, km)


# ─────────────────────────────────────────
#  主代理
# ─────────────────────────────────────────
from starlette.responses import StreamingResponse

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    # ── 协议检测：优先 x-api-key（Anthropic），其次 Authorization Bearer（OpenAI）──
    x_api_key  = request.headers.get("x-api-key", "").strip()
    auth_header = request.headers.get("Authorization", "")

    if x_api_key:
        return await _handle_anthropic(request, path, x_api_key)
    elif auth_header.startswith("Bearer "):
        secret = auth_header.removeprefix("Bearer ").strip()
        return await _handle_openai(request, path, secret)
    else:
        raise HTTPException(401, "Missing Authorization header")


async def _handle_openai(request: Request, path: str, secret: str):
    """处理 OpenAI 协议请求，转发到 DashScope OpenAI 兼容端点。"""
    kid = await rdb.hget("map:secret", secret)
    if not kid: raise HTTPException(401, "Invalid API key")
    meta = await _get_meta(kid)
    if not meta or not meta["enabled"]: raise HTTPException(403, "Key is disabled")

    body = await request.body()

    # ── 检查模型是否在 OpenAI Plan 白名单内 ──
    in_plan = True
    if body:
        try:
            model = json.loads(body).get("model", "")
            if model and model not in PLAN_MODELS:
                in_plan = False
        except Exception:
            pass  # 非 JSON 请求直接放行

    # 去掉 path 开头的 v1/（ALIYUN_BASE 已包含 /v1）
    upstream_path = path[3:] if path.startswith("v1/") else path
    upstream_url  = f"{ALIYUN_BASE}/{upstream_path}"

    upstream_headers = {k: v for k, v in request.headers.items()
                        if k.lower() not in ("host", "authorization", "content-length",
                                             "transfer-encoding", "connection", "keep-alive")}
    upstream_headers["Authorization"] = f"Bearer {ALIYUN_KEY}"

    if not in_plan:
        return await _forward(request, upstream_url, upstream_headers, body, quota_keys=None)

    quota_keys = await _check_and_deduct_quota(kid, meta)
    return await _forward(request, upstream_url, upstream_headers, body, quota_keys=quota_keys)


async def _handle_anthropic(request: Request, path: str, secret: str):
    """处理 Anthropic 协议请求，转发到 DashScope Anthropic 兼容端点。"""
    kid = await rdb.hget("map:secret", secret)
    if not kid: raise HTTPException(401, "Invalid API key")
    meta = await _get_meta(kid)
    if not meta or not meta["enabled"]: raise HTTPException(403, "Key is disabled")

    body = await request.body()

    # ── 检查模型是否在 Anthropic Plan 白名单内 ──
    in_plan = True
    if body:
        try:
            model = json.loads(body).get("model", "")
            if model and model not in ANTHROPIC_PLAN_MODELS:
                in_plan = False
        except Exception:
            pass

    # Anthropic 协议：path 保持原样（ANTHROPIC_BASE 不含 /v1）
    upstream_url = f"{ANTHROPIC_BASE}/{path}"

    # 构造上游请求头：移除客户端 x-api-key，注入真实阿里云 Key
    upstream_headers = {k: v for k, v in request.headers.items()
                        if k.lower() not in ("host", "x-api-key", "content-length",
                                             "transfer-encoding", "connection", "keep-alive")}
    upstream_headers["x-api-key"] = ALIYUN_KEY

    if not in_plan:
        return await _forward(request, upstream_url, upstream_headers, body, quota_keys=None)

    quota_keys = await _check_and_deduct_quota(kid, meta)
    return await _forward(request, upstream_url, upstream_headers, body, quota_keys=quota_keys)


async def _forward(request: Request, upstream_url: str,
                   headers: dict, body: bytes,
                   quota_keys: tuple | None):
    """
    透传请求到上游，自动处理流式/非流式。
    quota_keys: 若上游失败需要回滚的 Redis key 三元组，None 表示不回滚。
    """
    is_stream = False
    if body:
        try:
            is_stream = json.loads(body).get("stream", False)
        except Exception:
            pass

    skip = {"transfer-encoding", "connection", "keep-alive", "content-length"}

    # ── 流式透传 ─────────────────────────────────
    if is_stream:
        async def event_stream():
            rollback_done = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        method=request.method,
                        url=upstream_url,
                        headers=headers, content=body,
                        params=dict(request.query_params),
                    ) as upstream:
                        if upstream.status_code >= 500 and quota_keys:
                            await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
                            rollback_done = True
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
            except Exception:
                if quota_keys and not rollback_done:
                    await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── 非流式：一次性返回 ────────────────────────
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            upstream = await client.request(
                method=request.method,
                url=upstream_url,
                headers=headers, content=body,
                params=dict(request.query_params),
            )
    except Exception as e:
        if quota_keys:
            await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)
        raise HTTPException(502, f"上游连接失败: {e}")

    if upstream.status_code >= 500 and quota_keys:
        await rdb.eval(LUA_ROLLBACK, 3, *quota_keys)

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in skip}
    return Response(content=upstream.content,
                    status_code=upstream.status_code,
                    headers=resp_headers)
