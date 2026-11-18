import json
import time
import asyncio
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, Request, Form, Response, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import requests
import sync_engine as se
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI(title="Trakt Multi Sync", version="3.0.1")
app.mount("/static", StaticFiles(directory="static"), name="static")

DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)
ACCOUNT_PATH = DATA_DIR / "accounts.json"

# Device Code 临时存储池：device_code -> session info
device_sessions = {}


def load_accounts() -> dict:
    if not ACCOUNT_PATH.exists():
        return {}
    try:
        content = ACCOUNT_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        raw = json.loads(content)
        if isinstance(raw, list):
            converted = {}
            for acc in raw:
                if isinstance(acc, dict):
                    name = acc.get("remark") or acc.get("name") or "default"
                    converted[name] = {
                        "client_id": acc.get("client_id", ""),
                        "client_secret": acc.get("client_secret", ""),
                        "access_token": acc.get("access_token", ""),
                        "refresh_token": acc.get("refresh_token", ""),
                        "expires_at": acc.get("expires_at", 0)
                    }
            return converted
        elif isinstance(raw, dict):
            if "client_id" in raw or "access_token" in raw:
                return {"main": raw}
            return raw
        return {}
    except Exception as e:
        se.log(f"读取账号文件失败: {e}")
        return {}


async def sync_task():
    se.log("===== 开始执行星型双向同步 =====")
    try:
        await asyncio.to_thread(se.run_sync)
        se.log("===== 本轮星型双向同步成功结束 =====")
    except Exception as e:
        se.log(f"❌ 同步过程发生异常: {e}")


def run_background_sync():
    """后台定时触发任务的包装函数"""
    if se.LAST_SYNC_STATUS["running"]:
        se.log("上一轮同步仍在进行中，跳过本次定时触发。")
        return
    asyncio.run(sync_task())


# ==================== 定时任务调度器初始化与管理 ====================
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

def setup_scheduler():
    """根据当前配置的 sync_interval 初始化或刷新后台定时任务"""
    try:
        cfg = se.load_config()
        interval = int(cfg.get("sync_interval", 1800))
    except Exception:
        interval = 1800

    job_id = "auto_sync_job"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if interval > 0:
        scheduler.add_job(
            run_background_sync,
            'interval',
            seconds=interval,
            id=job_id,
            replace_existing=True
        )
        se.log(f"后台定时同步任务已加载/更新，触发间隔: {interval} 秒")

# 启动调度器
setup_scheduler()
scheduler.start()
# =========================================================================


# ==================== 登录鉴权相关接口 ====================
@app.get("/api/me")
async def api_me(admin_token: str = Cookie(default=None)):
    cfg = se.load_config()
    expected = cfg.get("admin_token", "admin")
    if admin_token and admin_token == expected:
        return JSONResponse({"logged_in": True})
    return JSONResponse({"logged_in": False}, status_code=401)

@app.post("/api/login")
async def api_login(request: Request, response: Response):
    try:
        body = await request.json()
        pwd = body.get("password", "")
        cfg = se.load_config()
        expected = cfg.get("admin_token", "admin")
        if pwd == expected:
            response.set_cookie(key="admin_token", value=expected, httponly=True)
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "msg": "密码错误"}, status_code=401)
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)

@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(key="admin_token")
    return JSONResponse({"ok": True})
# =========================================================================

@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("static/index.html").read_text(encoding="utf-8")
    return html


@app.get("/api/status")
async def api_status():
    return JSONResponse({"running": se.LAST_SYNC_STATUS["running"]})


@app.get("/api/logs")
async def api_logs(since: int = 0):
    lines = se.get_logs(since)
    return JSONResponse({"logs": lines, "total": len(se.LOG_BUFFER)})


@app.post("/api/start_sync")
async def api_start_sync():
    if se.LAST_SYNC_STATUS["running"]:
        return JSONResponse({"ok": False, "msg": "同步正在运行中，请勿重复触发"})
    asyncio.create_task(sync_task())
    return JSONResponse({"ok": True, "msg": "已启动同步任务"})


# ---------- 统一走 PIN（Device Code）授权接口 ----------
@app.post("/api/oauth/device/start")
async def api_oauth_device_start(remark: str = Form(...), client_id: str = Form(...), client_secret: str = Form("")):
    remark = remark.strip()
    client_id = client_id.strip()
    client_secret = client_secret.strip()

    if not remark or not client_id:
        return JSONResponse({"ok": False, "msg": "备注名和 Client ID 不能为空"}, status_code=400)

    headers_base = {
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "User-Agent": "Trakt Multi Sync/3.0.1"
    }
    dev_payload = {"client_id": client_id}
    if client_secret:
        dev_payload["client_secret"] = client_secret

    try:
        r_dev = requests.post(
            "https://trakt.tv/oauth/device/code",
            json=dev_payload,
            headers=headers_base,
            timeout=15
        )
        r_dev.raise_for_status()
        dev_info = r_dev.json()
    except Exception as e:
        se.log(f"获取 device_code 失败: {e}")
        return JSONResponse({"ok": False, "msg": f"获取授权码失败: {e}"}, status_code=400)

    device_code = dev_info["device_code"]
    device_sessions[device_code] = {
        "remark": remark,
        "client_id": client_id,
        "client_secret": client_secret,
        "exp": time.time() + int(dev_info.get("expires_in", 600)),
        "interval": max(3, int(dev_info.get("interval", 5)))
    }

    se.log(f"账号 [{remark}] 申请到 PIN={dev_info['user_code']}，请在浏览器打开 https://trakt.tv/activate 输入验证码")
    return JSONResponse({
        "ok": True,
        "user_code": dev_info["user_code"],
        "device_code": device_code,
        "verification_url": dev_info.get("verification_url", "https://trakt.tv/activate"),
        "interval": dev_info.get("interval", 5)
    })


@app.get("/api/oauth/device/poll")
async def api_oauth_device_poll(device_code: str):
    sess = device_sessions.get(device_code)
    if not sess:
        return JSONResponse({"status": "cancelled"})
    if time.time() >= sess["exp"]:
        device_sessions.pop(device_code, None)
        return JSONResponse({"status": "expired"})

    client_id = sess["client_id"]
    client_secret = sess["client_secret"]
    remark = sess["remark"]

    # Trakt 官方 Device Token 兼容载荷（同时传入 device_code 与 code 确保万无一失）
    poll_payload = {
        "client_id": client_id,
        "device_code": device_code,
        "code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
    }
    if client_secret:
        poll_payload["client_secret"] = client_secret

    headers_poll = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "User-Agent": "Trakt Multi Sync/3.0.1"
    }

    try:
        pr = requests.post(
            "https://trakt.tv/oauth/device/token",
            json=poll_payload,
            headers=headers_poll,
            timeout=15
        )
    except Exception as e:
        se.log(f"轮询网络请求异常: {e}")
        return JSONResponse({"status": "pending"})

    code = pr.status_code
    text = pr.text.strip()
    
    if code == 200:
        token_data = pr.json()
        accounts = load_accounts()
        accounts[remark] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": time.time() + token_data.get("expires_in", 7776000)
        }
        se.save_accounts(accounts)
        device_sessions.pop(device_code, None)
        se.log(f"✅ 账号 [{remark}] PIN 授权成功并已成功持久化保存 token！")
        return JSONResponse({"status": "authorized", "remark": remark})

    if code == 400 and not text:
        return JSONResponse({"status": "pending"})

    err_json = {}
    try:
        err_json = pr.json()
    except Exception:
        pass
    
    err = err_json.get("error")
    
    if err and err != "authorization_pending":
        se.log(f"⚠️ 轮询状态响应 [{code}] error={err}, full_resp={text}")

    if err == "authorization_pending":
        return JSONResponse({"status": "pending"})
    elif err == "slow_down":
        return JSONResponse({"status": "slow_down"})
    elif err in ("access_denied", "expired_token"):
        device_sessions.pop(device_code, None)
        se.log(f"❌ 授权终止: {err}")
        return JSONResponse({"status": "expired"})

    return JSONResponse({"status": "pending"})


@app.get("/api/list_accounts")
async def api_list_accounts():
    accs = load_accounts()
    out = []
    for name, a in accs.items():
        if not isinstance(a, dict):
            continue
        expires_at = a.get("expires_at", time.time() + 999999)
        out.append({
            "remark": name,
            "client_id": (a.get("client_id") or "")[:10] + "...",
            "expires_at": expires_at,
            "expired": time.time() > expires_at
        })
    return JSONResponse({"accounts": out})


@app.post("/api/del_account")
async def api_del_account(remark: str = Form(...)):
    accs = load_accounts()
    if remark in accs:
        del accs[remark]
        se.save_accounts(accs)
        se.log(f"已删除账号 [{remark}]")
    return JSONResponse({"ok": True})


@app.get("/api/config")
async def api_get_config():
    return JSONResponse(se.load_config())


@app.post("/api/config")
async def api_set_config(request: Request):
    try:
        body = await request.json()
        cfg = se.load_config()
        cfg.update(body)
        se.save_config(cfg)
        
        # 【核心新增】当用户在后台修改并保存配置时，立刻动态重置 APScheduler 定时任务间隔
        setup_scheduler()
        
        se.log("同步配置已更新并成功刷新定时任务")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080)