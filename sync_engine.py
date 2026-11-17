import os
import json
import time
import threading
import requests
from pathlib import Path
from datetime import datetime
from collections import deque

BASE = "https://api.trakt.tv"
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
CONFIG_FILE = DATA_DIR / "config.json"

LOG_BUFFER = deque(maxlen=500)
LOG_LOCK = threading.Lock()

SYNC_LOCK = threading.Lock()
LAST_SYNC_STATUS = {"running": False, "last_run": None, "last_error": None}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with LOG_LOCK:
        LOG_BUFFER.append(line)
    print(line, flush=True)


def get_logs(since=0):
    with LOG_LOCK:
        items = list(LOG_BUFFER)
    return items[since:]


DEFAULT_CONFIG = {
    "master_account": "",
    "slave_accounts": [],
    "sync_watched": True,
    "sync_ratings": True,
    "sync_watchlist": True,
    "sync_collection": True,
    "sync_lists": True,
    "sync_playback": True,     # 继续观看/播放进度同步
    "sync_hidden": True,       # 隐藏项目同步
    "sync_likes": True,        # 点赞片单同步
    "sync_interval": 1800,
    "request_sleep": 0.5,
    "public_url": "http://localhost:8080",
    "admin_token": "admin",
    "password_changed": False,
}


def load_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        cfg = dict(DEFAULT_CONFIG)
    else:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        cfg = merged
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_accounts():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        content = ACCOUNTS_FILE.read_text(encoding="utf-8").strip()
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
        log(f"❌ 读取账号文件失败: {e}")
        return {}


def save_accounts(accounts):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


def get_account_credential(account_name: str):
    accounts = load_accounts()
    cfg = load_config()
    master_name = cfg.get("master_account", "main")
    target = accounts.get(account_name, {})
    master = accounts.get(master_name, {})

    cid = (target.get("client_id") or "").strip()
    csec = (target.get("client_secret") or "").strip()
    if not cid:
        cid = (master.get("client_id") or "").strip()
        csec = (master.get("client_secret") or "").strip()
    if not cid:
        raise ValueError("缺少 Trakt Client ID，请在主账号里填写")
    return cid, csec


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


class Trakt:
    def __init__(self, name, cfg):
        self.name = name
        self.client_id = cfg["client_id"]
        self.client_secret = cfg.get("client_secret", "")
        self.access_token = cfg["access_token"]
        self.refresh_token = cfg["refresh_token"]
        self.cfg = cfg

    def headers(self):
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "Trakt-Multi-Sync/v2.0.3"
        }

    def refresh(self):
        log(f"[{self.name}] 正在刷新 access_token ...")
        body = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "grant_type": "refresh_token",
        }
        if self.client_secret:
            body["client_secret"] = self.client_secret
        try:
            r = requests.post(f"{BASE}/oauth/token", data=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            self.access_token = data["access_token"]
            self.refresh_token = data["refresh_token"]
            self.cfg["access_token"] = self.access_token
            self.cfg["refresh_token"] = self.refresh_token

            accounts = load_accounts()
            if self.name in accounts:
                accounts[self.name]["access_token"] = self.access_token
                accounts[self.name]["refresh_token"] = self.refresh_token
                save_accounts(accounts)
                log(f"[{self.name}] 刷新后的 token 已成功持久化落盘")
        except requests.exceptions.HTTPError as he:
            err_text = he.response.text if he.response is not None else str(he)
            log(f"[{self.name}] ❌ 刷新 token 失败 [HTTP {he.response.status_code if he.response else 'N/A'}]: {err_text}")
            raise
        except Exception as e:
            log(f"[{self.name}] ❌ 刷新 token 异常: {e}")
            raise

    def _req(self, method, path, **kwargs):
        for attempt in range(3):
            try:
                r = requests.request(method, f"{BASE}{path}", headers=self.headers(), timeout=60, **kwargs)
                if r.status_code == 401 and attempt == 0:
                    log(f"[{self.name}] 收到 401 未授权响应，尝试刷新 Token 后重试...")
                    self.refresh()
                    continue
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 10))
                    log(f"[{self.name}] ⚠️ 触发 Trakt API 频控限制 (429)，等待 {retry_after} 秒后重试...")
                    time.sleep(retry_after)
                    continue
                
                # 若遇到其他 4xx / 5xx 错误，记录详细的响应内容
                if r.status_code >= 400:
                    log(f"[{self.name}] ⚠️ 请求 {path} 返回错误状态码 [{r.status_code}]: {r.text}")

                r.raise_for_status()
                if not r.text:
                    return {}
                return r.json()
            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 429 and attempt < 2:
                    retry_after = int(he.response.headers.get("Retry-After", 10))
                    log(f"[{self.name}] ⚠️ 触发 429 限速，等待 {retry_after} 秒...")
                    time.sleep(retry_after)
                    continue
                err_body = he.response.text if he.response is not None else "无响应体"
                status_code = he.response.status_code if he.response is not None else "未知"
                log(f"[{self.name}] ❌ HTTPError 请求 [{path}] 失败 (状态码: {status_code}): {err_body}")
                raise
            except Exception as e:
                log(f"[{self.name}] ❌ 请求 [{path}] 发生网络或未知异常 (第 {attempt+1} 次尝试): {e}")
                if attempt == 2:
                    raise
                time.sleep(2)
        raise RuntimeError(f"[{self.name}] 请求 {path} 达到最大重试次数仍失败")

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body):
        return self._req("POST", path, json=body)


def fetch_all(t: Trakt, cfg):
    data = {}
    sleep = cfg["request_sleep"]
    log(f"[{t.name}] 开始拉取各项云端数据...")

    # 1. 观看历史
    if cfg.get("sync_watched"):
        try:
            data["watched_shows"] = t.get("/sync/watched/shows")
            data["watched_movies"] = t.get("/sync/watched/movies")
            log(f"[{t.name}] ✓ 观看历史拉取成功 (剧集: {len(data['watched_shows'])}部, 电影: {len(data['watched_movies'])}部)")
        except Exception as e:
            log(f"[{t.name}] ❌ 拉取观看历史异常: {e}")
            data["watched_shows"], data["watched_movies"] = [], []
        time.sleep(sleep)

    # 2. 评分
    if cfg.get("sync_ratings"):
        try:
            data["ratings_shows"] = t.get("/sync/ratings/shows")
            data["ratings_movies"] = t.get("/sync/ratings/movies")
            log(f"[{t.name}] ✓ 评分拉取成功 (剧集: {len(data['ratings_shows'])}个, 电影: {len(data['ratings_movies'])}个)")
        except Exception as e:
            log(f"[{t.name}] ❌ 拉取评分异常: {e}")
            data["ratings_shows"], data["ratings_movies"] = [], []
        time.sleep(sleep)

    # 3. 追剧清单
    if cfg.get("sync_watchlist"):
        try:
            data["watchlist_shows"] = t.get("/sync/watchlist/shows")
            data["watchlist_movies"] = t.get("/sync/watchlist/movies")
            log(f"[{t.name}] ✓ 追剧清单拉取成功 (剧集: {len(data['watchlist_shows'])}部, 电影: {len(data['watchlist_movies'])}部)")
        except Exception as e:
            log(f"[{t.name}] ❌ 拉取追剧清单异常: {e}")
            data["watchlist_shows"], data["watchlist_movies"] = [], []
        time.sleep(sleep)

    # 4. 收藏
    if cfg.get("sync_collection"):
        try:
            data["collection_shows"] = t.get("/sync/collection/shows")
            data["collection_movies"] = t.get("/sync/collection/movies")
            log(f"[{t.name}] ✓ 收藏库拉取成功 (剧集: {len(data['collection_shows'])}部, 电影: {len(data['collection_movies'])}部)")
        except Exception as e:
            log(f"[{t.name}] ❌ 拉取收藏库异常: {e}")
            data["collection_shows"], data["collection_movies"] = [], []
        time.sleep(sleep)

    # 5. 自定义片单
    if cfg.get("sync_lists"):
        try:
            lists = t.get("/users/me/lists?extended=min")
            time.sleep(sleep)
            lists_data = {}
            for lst in lists:
                slug = lst["ids"]["slug"]
                items = t.get(f"/users/me/lists/{slug}/items?extended=min&limit=1000")
                lists_data[lst["name"]] = {"info": lst, "items": items}
                time.sleep(sleep)
            data["lists_data"] = lists_data
            log(f"[{t.name}] ✓ 自定义片单拉取成功 (共 {len(lists_data)} 个片单)")
        except Exception as e:
            log(f"[{t.name}] ❌ 拉取自定义片单异常: {e}")
            data["lists_data"] = {}

    # 6. 播放进度 (Continue Watching)
    if cfg.get("sync_playback"):
        try:
            data["playback"] = t.get("/sync/playback")
            log(f"[{t.name}] ✓ 播放进度(继续观看)拉取成功 (共 {len(data['playback'])} 项)")
            time.sleep(sleep)
        except Exception as e:
            log(f"[{t.name}] ❌ 获取播放进度失败: {e}")
            data["playback"] = []

    # 7. 隐藏项目 (按 Trakt 官方文档的标准 section 拉取，支持分页上限)
    if cfg.get("sync_hidden"):
        hidden_data = {}
        total_hidden = 0
        for section in ["progress_watched", "calendar", "progress_collected", "recommendations"]:
            try:
                res = t.get(f"/users/hidden/{section}?limit=1000")
                items = res if isinstance(res, list) else []
                hidden_data[section] = items
                total_hidden += len(items)
                time.sleep(sleep)
            except Exception as e:
                log(f"[{t.name}] ⚠️ 获取隐藏项目 [{section}] 失败: {e}")
                hidden_data[section] = []
        data["hidden"] = hidden_data
        log(f"[{t.name}] ✓ 隐藏项目拉取完成 (共 {total_hidden} 项: 进度 {len(hidden_data['progress_watched'])} / 日历 {len(hidden_data['calendar'])} / 收藏 {len(hidden_data['progress_collected'])} / 推荐 {len(hidden_data['recommendations'])})")

    # 8. 点赞片单
    if cfg.get("sync_likes"):
        try:
            liked = t.get("/users/likes/lists?extended=min")
            time.sleep(sleep)
            liked_data = {}
            for item in liked:
                lst = item.get("list", {})
                if lst and "ids" in lst:
                    user_slug = lst.get("user", {}).get("ids", {}).get("slug") or lst.get("user", {}).get("username")
                    list_slug = lst["ids"].get("slug")
                    if user_slug and list_slug:
                        items = t.get(f"/users/{user_slug}/lists/{list_slug}/items?extended=min&limit=1000")
                        liked_data[list_slug] = {"info": lst, "items": items, "user": user_slug}
                        time.sleep(sleep)
            data["liked_lists"] = liked_data
            log(f"[{t.name}] ✓ 点赞公共片单拉取成功 (共 {len(liked_data)} 个)")
        except Exception as e:
            log(f"[{t.name}] ❌ 获取点赞片单失败: {e}")
            data["liked_lists"] = {}

    log(f"[{t.name}] 所有云端数据拉取完毕。")
    return data


def build_watched_index(data):
    shows = {}
    for item in data.get("watched_shows", []):
        sid = item["show"]["ids"]["trakt"]
        for season in item.get("seasons", []):
            snum = season["number"]
            for ep in season.get("episodes", []):
                key = (snum, ep["number"])
                shows.setdefault(sid, {})[key] = {
                    "ids": item["show"]["ids"],
                    "watched_at": ep["last_watched_at"],
                }
    movies = {}
    for item in data.get("watched_movies", []):
        mid = item["movie"]["ids"]["trakt"]
        movies[mid] = {"ids": item["movie"]["ids"], "watched_at": item["last_watched_at"]}
    return shows, movies


def merge_watched_payload(src_shows, src_movies, dst_shows_idx, dst_movies_idx):
    shows_payload = []
    for sid, eps in src_shows.items():
        dst_eps = dst_shows_idx.get(sid, {})
        seasons_map = {}
        for key, info in eps.items():
            if key not in dst_eps:
                seasons_map.setdefault(key[0], []).append({
                    "number": key[1], "watched_at": info["watched_at"],
                })
        if seasons_map:
            shows_payload.append({
                "ids": list(eps.values())[0]["ids"],
                "seasons": [{"number": n, "episodes": es} for n, es in seasons_map.items()],
            })
    movies_payload = []
    for mid, info in src_movies.items():
        if mid not in dst_movies_idx:
            movies_payload.append({"ids": info["ids"], "watched_at": info["watched_at"]})
    return movies_payload, shows_payload


def build_ratings_index(data):
    shows = {}
    for x in data.get("ratings_shows", []):
        sid = x["show"]["ids"]["trakt"]
        shows[sid] = {"ids": x["show"]["ids"], "rating": x["rating"], "rated_at": x["rated_at"]}
    movies = {}
    for x in data.get("ratings_movies", []):
        mid = x["movie"]["ids"]["trakt"]
        movies[mid] = {"ids": x["movie"]["ids"], "rating": x["rating"], "rated_at": x["rated_at"]}
    return shows, movies


def diff_ratings(src_idx, dst_idx):
    out = []
    for tid, s in src_idx.items():
        d = dst_idx.get(tid)
        if d is None:
            out.append({"ids": s["ids"], "rating": s["rating"], "rated_at": s["rated_at"]})
            continue
        ts, td = parse_dt(s["rated_at"]), parse_dt(d["rated_at"])
        if ts and td and ts > td:
            out.append({"ids": s["ids"], "rating": s["rating"], "rated_at": s["rated_at"]})
    return out


def build_watchlist_index(data):
    shows = {x["show"]["ids"]["trakt"]: x["show"]["ids"] for x in data.get("watchlist_shows", [])}
    movies = {x["movie"]["ids"]["trakt"]: x["movie"]["ids"] for x in data.get("watchlist_movies", [])}
    return shows, movies


def diff_watchlist(src_shows, src_movies, dst_shows, dst_movies):
    shows_payload = [{"ids": ids} for sid, ids in src_shows.items() if sid not in dst_shows]
    movies_payload = [{"ids": ids} for mid, ids in src_movies.items() if mid not in dst_movies]
    return movies_payload, shows_payload


def build_collection_index(data):
    shows = {x["show"]["ids"]["trakt"]: x["show"]["ids"] for x in data.get("collection_shows", [])}
    movies = {x["movie"]["ids"]["trakt"]: x["movie"]["ids"] for x in data.get("collection_movies", [])}
    return shows, movies


def diff_collection(src_shows, src_movies, dst_shows, dst_movies):
    shows_payload = [{"ids": ids} for sid, ids in src_shows.items() if sid not in dst_shows]
    movies_payload = [{"ids": ids} for mid, ids in src_movies.items() if mid not in dst_movies]
    return movies_payload, shows_payload


def diff_lists(src_lists_data, dst_lists_data, dst_client: Trakt):
    for name, src_lst in src_lists_data.items():
        src_items = src_lst["items"]
        if name not in dst_lists_data:
            info = src_lst["info"]
            try:
                created = dst_client.post("/users/me/lists", {
                    "name": name,
                    "description": info.get("description", "") or "",
                    "privacy": info.get("privacy", "private"),
                    "display_numbers": info.get("display_numbers", False),
                    "allow_comments": info.get("allow_comments", True),
                })
                new_slug = created["ids"]["slug"]
                movies = [{"ids": x["movie"]["ids"]} for x in src_items if x.get("type") == "movie"]
                shows = [{"ids": x["show"]["ids"]} for x in src_items if x.get("type") == "show"]
                if movies or shows:
                    res = dst_client.post(f"/users/me/lists/{new_slug}/items", {"movies": movies, "shows": shows})
                    log(f"  lists: [成功] 在 [{dst_client.name}] 创建片单《{name}》（电影: {len(movies)}个 / 剧集: {len(shows)}部）-> 响应: {res}")
                else:
                    log(f"  lists: [成功] 在 [{dst_client.name}] 创建空片单《{name}》")
            except Exception as e:
                log(f"  lists: [失败] 创建片单《{name}》异常: {e}")
        else:
            dst_lst = dst_lists_data[name]
            dst_items = dst_lst["items"]
            dst_slug = dst_lst["info"]["ids"]["slug"]
            dst_movie_ids = {x["movie"]["ids"]["trakt"] for x in dst_items if x.get("type") == "movie"}
            dst_show_ids = {x["show"]["ids"]["trakt"] for x in dst_items if x.get("type") == "show"}
            movies = [{"ids": x["movie"]["ids"]} for x in src_items
                      if x.get("type") == "movie" and x["movie"]["ids"]["trakt"] not in dst_movie_ids]
            shows = [{"ids": x["show"]["ids"]} for x in src_items
                     if x.get("type") == "show" and x["show"]["ids"]["trakt"] not in dst_show_ids]
            if movies or shows:
                try:
                    res = dst_client.post(f"/users/me/lists/{dst_slug}/items", {"movies": movies, "shows": shows})
                    log(f"  lists: [成功] [{dst_client.name}] 的《{name}》补入新项（电影: {len(movies)}个 / 剧集: {len(shows)}部）-> 响应: {res}")
                except Exception as e:
                    log(f"  lists: [失败] 往片单《{name}》补入条目异常: {e}")


# 播放进度双向对齐 (Continue Watching) 附带详细日志
def sync_playback_progress(a: Trakt, b: Trakt, a_playback, b_playback):
    log(f"  [playback] 开始对齐 [{a.name}] -> [{b.name}] 的播放进度...")
    b_items = {}
    for item in b_playback:
        ptype = item.get("type")
        if ptype == "movie" and "movie" in item:
            mid = item["movie"]["ids"]["trakt"]
            b_items[("movie", mid)] = item.get("progress", 0)
        elif ptype == "episode" and "episode" in item:
            eid = item["episode"]["ids"]["trakt"]
            b_items[("episode", eid)] = item.get("progress", 0)

    count_synced = 0
    for item in a_playback:
        ptype = item.get("type")
        progress = item.get("progress", 0)

        if ptype == "movie" and "movie" in item:
            movie_info = item["movie"]
            mid = movie_info["ids"]["trakt"]
            if ("movie", mid) not in b_items or progress > b_items[("movie", mid)]:
                try:
                    res = b.post("/scrobble/pause", {
                        "movie": {"ids": movie_info["ids"]},
                        "progress": progress
                    })
                    count_synced += 1
                    log(f"    ✓ 成功同步电影进度 [{movie_info.get('title')}] 到 [{b.name}]，进度: {progress}% -> 响应: {res}")
                except Exception as e:
                    log(f"    ❌ 同步电影进度 [{movie_info.get('title')}] 失败: {e}")

        elif ptype == "episode" and "episode" in item:
            episode_info = item["episode"]
            show_info = item.get("show")
            eid = episode_info["ids"]["trakt"]
            if ("episode", eid) not in b_items or progress > b_items[("episode", eid)]:
                try:
                    payload = {
                        "episode": {"ids": episode_info["ids"]},
                        "progress": progress
                    }
                    if show_info:
                        payload["show"] = {"ids": show_info["ids"]}
                    res = b.post("/scrobble/pause", payload)
                    count_synced += 1
                    show_title = show_info.get('title', '未知剧集') if show_info else '未知剧集'
                    log(f"    ✓ 成功同步剧集进度 [{show_title} S{episode_info.get('season')}E{episode_info.get('number')}] 到 [{b.name}]，进度: {progress}% -> 响应: {res}")
                except Exception as e:
                    log(f"    ❌ 同步剧集进度失败: {e}")
    log(f"  [playback] 完成 [{a.name}] -> [{b.name}] 进度同步，共更新 {count_synced} 项。")


# 隐藏项目同步（使用官方标准 section 路径，带详细条目与响应日志）
def sync_hidden_items(a: Trakt, b: Trakt, a_hidden, b_hidden):
    log(f"  [hidden] 开始对齐 [{a.name}] -> [{b.name}] 的隐藏项目...")
    total_synced = 0
    for section in ["progress_watched", "calendar", "progress_collected", "recommendations"]:
        a_list = a_hidden.get(section, [])
        b_list = b_hidden.get(section, [])

        b_ids = set()
        for item in b_list:
            if item.get("type") == "movie" and "movie" in item:
                b_ids.add(("movie", item["movie"]["ids"]["trakt"]))
            elif item.get("type") == "show" and "show" in item:
                b_ids.add(("show", item["show"]["ids"]["trakt"]))

        count_hidden = 0
        for item in a_list:
            if item.get("type") == "movie" and "movie" in item:
                movie_info = item["movie"]
                mid = movie_info["ids"]["trakt"]
                title = movie_info.get('title', '未知电影')
                if ("movie", mid) not in b_ids:
                    try:
                        res = b.post(f"/users/hidden/{section}", {"movies": [{"ids": movie_info["ids"]}]})
                        count_hidden += 1
                        total_synced += 1
                        b_ids.add(("movie", mid))
                        log(f"    ✓ [hidden] 成功将电影 [{title}] 在 [{b.name}] 的 [{section}] 模块中同步隐藏 -> 响应: {res}")
                    except Exception as e:
                        log(f"    ❌ [hidden] 同步隐藏电影 [{title}] ({section}) 失败: {e}")

            elif item.get("type") == "show" and "show" in item:
                show_info = item["show"]
                sid = show_info["ids"]["trakt"]
                title = show_info.get('title', '未知剧集')
                if ("show", sid) not in b_ids:
                    try:
                        res = b.post(f"/users/hidden/{section}", {"shows": [{"ids": show_info["ids"]}]})
                        count_hidden += 1
                        total_synced += 1
                        b_ids.add(("show", sid))
                        log(f"    ✓ [hidden] 成功将剧集 [{title}] 在 [{b.name}] 的 [{section}] 模块中同步隐藏 -> 响应: {res}")
                    except Exception as e:
                        log(f"    ❌ [hidden] 同步隐藏剧集 [{title}] ({section}) 失败: {e}")

        if count_hidden > 0:
            log(f"  [hidden] [{a.name}] -> [{b.name}] ({section}) 同步完成，本模块新增隐藏 {count_hidden} 项。")
    log(f"  [hidden] 完成 [{a.name}] -> [{b.name}] 全部隐藏模块对齐，共新增隐藏 {total_synced} 项。")


# 点赞片单同步附带详细日志
def sync_liked_lists(a: Trakt, b: Trakt, a_likes, b_likes):
    log(f"  [likes] 开始对齐 [{a.name}] -> [{b.name}] 的点赞片单...")
    count_liked = 0
    for slug, data in a_likes.items():
        if slug not in b_likes:
            info = data["info"]
            items = data["items"]
            list_name = info.get('name', slug)
            try:
                res = b.post(f"/users/likes/lists/{data['user']}/{slug}", {})
                count_liked += 1
                log(f"  [likes] [成功] [{b.name}] 成功点赞/关注来自 [{data['user']}] 的公共片单《{list_name}》 -> 响应: {res}")
            except Exception as like_err:
                log(f"  [likes] ⚠️ 直接点赞片单《{list_name}》未成功，尝试在 [{b.name}] 创建镜像片单... 原因: {like_err}")
                try:
                    created = b.post("/users/me/lists", {
                        "name": list_name,
                        "description": info.get("description", "") or "",
                        "privacy": info.get("privacy", "private"),
                    })
                    new_slug = created["ids"]["slug"]
                    movies = [{"ids": x["movie"]["ids"]} for x in items if x.get("type") == "movie"]
                    shows = [{"ids": x["show"]["ids"]} for x in items if x.get("type") == "show"]
                    if movies or shows:
                        res = b.post(f"/users/me/lists/{new_slug}/items", {"movies": movies, "shows": shows})
                        log(f"  [likes] [成功] 在 [{b.name}] 成功镜像创建点赞片单《{list_name}》（电影: {len(movies)}个 / 剧集: {len(shows)}部）-> 响应: {res}")
                    else:
                        log(f"  [likes] [成功] 在 [{b.name}] 成功镜像创建空片单《{list_name}》")
                    count_liked += 1
                except Exception as ex:
                    log(f"  [likes] [失败] 镜像创建点赞片单《{list_name}》彻底失败: {ex}")
    log(f"  [likes] 完成 [{a.name}] -> [{b.name}] 点赞片单同步，共处理 {count_liked} 个片单。")


def sync_pair(a: Trakt, b: Trakt, a_data, b_data, cfg):
    log(f"---------- 开始双向同步: [{a.name}] <=> [{b.name}] ----------")
    sleep = cfg["request_sleep"]

    # 1. 观看历史
    if cfg["sync_watched"]:
        try:
            a_shows, a_movies = build_watched_index(a_data)
            b_shows, b_movies = build_watched_index(b_data)
            m_ab, s_ab = merge_watched_payload(a_shows, a_movies, b_shows, b_movies)
            m_ba, s_ba = merge_watched_payload(b_shows, b_movies, a_shows, a_movies)
            if m_ab or s_ab:
                res = b.post("/sync/history", {"movies": m_ab, "shows": s_ab})
                log(f"  watched [{a.name}] -> [{b.name}]: 补入电影 {len(m_ab)}部, 剧集 {len(s_ab)}项 -> 响应: {res}")
            else:
                log(f"  watched [{a.name}] -> [{b.name}]: 无需补入")

            if m_ba or s_ba:
                res = a.post("/sync/history", {"movies": m_ba, "shows": s_ba})
                log(f"  watched [{b.name}] -> [{a.name}]: 补入电影 {len(m_ba)}部, 剧集 {len(s_ba)}项 -> 响应: {res}")
            else:
                log(f"  watched [{b.name}] -> [{a.name}]: 无需补入")
        except Exception as e:
            log(f"  ❌ watched 同步发生异常: {e}")
        time.sleep(sleep)

    # 2. 评分
    if cfg["sync_ratings"]:
        try:
            a_rs, a_rm = build_ratings_index(a_data)
            b_rs, b_rm = build_ratings_index(b_data)
            ab_movies = diff_ratings(a_rm, b_rm)
            ab_shows = diff_ratings(a_rs, b_rs)
            ba_movies = diff_ratings(b_rm, a_rm)
            ba_shows = diff_ratings(b_rs, a_rs)
            if ab_movies or ab_shows:
                res = b.post("/sync/ratings", {"movies": ab_movies, "shows": ab_shows})
                log(f"  ratings [{a.name}] -> [{b.name}]: 同步评分 电影{len(ab_movies)} 剧集{len(ab_shows)} -> 响应: {res}")
            else:
                log(f"  ratings [{a.name}] -> [{b.name}]: 无需补入")
            if ba_movies or ba_shows:
                res = a.post("/sync/ratings", {"movies": ba_movies, "shows": ba_shows})
                log(f"  ratings [{b.name}] -> [{a.name}]: 同步评分 电影{len(ba_movies)} 剧集{len(ba_shows)} -> 响应: {res}")
            else:
                log(f"  ratings [{b.name}] -> [{a.name}]: 无需补入")
        except Exception as e:
            log(f"  ❌ ratings 同步发生异常: {e}")
        time.sleep(sleep)

    # 3. 追剧清单
    if cfg["sync_watchlist"]:
        try:
            a_ws, a_wm = build_watchlist_index(a_data)
            b_ws, b_wm = build_watchlist_index(b_data)
            m_ab, s_ab = diff_watchlist(a_ws, a_wm, b_ws, b_wm)
            m_ba, s_ba = diff_watchlist(b_ws, b_wm, a_ws, a_wm)
            if m_ab or s_ab:
                res = b.post("/sync/watchlist", {"movies": m_ab, "shows": s_ab})
                log(f"  watchlist [{a.name}] -> [{b.name}]: 补充清单 电影{len(m_ab)} 剧集{len(s_ab)} -> 响应: {res}")
            else:
                log(f"  watchlist [{a.name}] -> [{b.name}]: 无需补入")
            if m_ba or s_ba:
                res = a.post("/sync/watchlist", {"movies": m_ba, "shows": s_ba})
                log(f"  watchlist [{b.name}] -> [{a.name}]: 补充清单 电影{len(m_ba)} 剧集{len(s_ba)} -> 响应: {res}")
            else:
                log(f"  watchlist [{b.name}] -> [{a.name}]: 无需补入")
        except Exception as e:
            log(f"  ❌ watchlist 同步发生异常: {e}")
        time.sleep(sleep)

    # 4. 收藏
    if cfg.get("sync_collection"):
        try:
            a_cs, a_cm = build_collection_index(a_data)
            b_cs, b_cm = build_collection_index(b_data)
            m_ab, s_ab = diff_collection(a_cs, a_cm, b_cs, b_cm)
            m_ba, s_ba = diff_collection(b_cs, b_cm, a_cs, a_cm)
            if m_ab or s_ab:
                res = b.post("/sync/collection", {"movies": m_ab, "shows": s_ab})
                log(f"  collection [{a.name}] -> [{b.name}]: 补充收藏 电影{len(m_ab)} 剧集{len(s_ab)} -> 响应: {res}")
            else:
                log(f"  collection [{a.name}] -> [{b.name}]: 无需补入")
            if m_ba or s_ba:
                res = a.post("/sync/collection", {"movies": m_ba, "shows": s_ba})
                log(f"  collection [{b.name}] -> [{a.name}]: 补充收藏 电影{len(m_ba)} 剧集{len(s_ba)} -> 响应: {res}")
            else:
                log(f"  collection [{b.name}] -> [{a.name}]: 无需补入")
        except Exception as e:
            log(f"  ❌ collection 同步发生异常: {e}")
        time.sleep(sleep)

    # 5. 自定义片单
    if cfg.get("sync_lists"):
        try:
            a_lists = a_data.get("lists_data", {})
            b_lists = b_data.get("lists_data", {})
            diff_lists(a_lists, b_lists, b)
            diff_lists(b_lists, a_lists, a)
        except Exception as e:
            log(f"  ❌ lists 同步发生异常: {e}")
        time.sleep(sleep)

    # 6. 播放进度 (继续观看)
    if cfg.get("sync_playback"):
        try:
            a_pb = a_data.get("playback", [])
            b_pb = b_data.get("playback", [])
            sync_playback_progress(a, b, a_pb, b_pb)
            sync_playback_progress(b, a, b_pb, a_pb)
        except Exception as e:
            log(f"  ❌ playback 同步异常: {e}")
        time.sleep(sleep)

    # 7. 隐藏项目
    if cfg.get("sync_hidden"):
        try:
            sync_hidden_items(a, b, a_data.get("hidden", {}), b_data.get("hidden", {}))
            sync_hidden_items(b, a, b_data.get("hidden", {}), a_data.get("hidden", {}))
        except Exception as e:
            log(f"  ❌ hidden 同步异常: {e}")
        time.sleep(sleep)

    # 8. 点赞片单
    if cfg.get("sync_likes"):
        try:
            sync_liked_lists(a, b, a_data.get("liked_lists", {}), b_data.get("liked_lists", {}))
            sync_liked_lists(b, a, b_data.get("liked_lists", {}), a_data.get("liked_lists", {}))
        except Exception as e:
            log(f"  ❌ likes 同步异常: {e}")
        time.sleep(sleep)

    log(f"---------- 片段双向同步完成: [{a.name}] <=> [{b.name}] ----------")


def run_sync(dry_run=False):
    if not SYNC_LOCK.acquire(blocking=False):
        log("⚠️ 已有其他同步任务正在运行中，跳过本轮触发。")
        return
    try:
        LAST_SYNC_STATUS["running"] = True
        LAST_SYNC_STATUS["last_error"] = None
        cfg = load_config()
        accounts = load_accounts()

        master_name = cfg["master_account"]
        slave_names = list(cfg["slave_accounts"])

        if not master_name:
            log("❌ 未指定主账号 (master_account)，请前往 Web 网页端配置并保存。")
            return
        if master_name not in accounts:
            log(f"❌ 主账号 [{master_name}] 未在已授权账号列表中找到，请先完成该账号授权。")
            return

        clients = {master_name: Trakt(master_name, accounts[master_name])}
        for s in slave_names:
            if s in accounts:
                clients[s] = Trakt(s, accounts[s])
            else:
                log(f"⚠️ 从账号 [{s}] 未找到授权记录，将被跳过。")

        log(f"🚀 开始执行星型双向同步，当前有效客户端数量: {len(clients)} (主账号: {master_name}, 从账号: {slave_names})")
        
        # 拉取所有客户端数据
        datas = {}
        for name, c in clients.items():
            try:
                datas[name] = fetch_all(c, cfg)
            except Exception as e:
                log(f"❌ 拉取账号 [{name}] 的数据时发生严重异常: {e}")
                datas[name] = {}

        master = clients[master_name]
        for sname in slave_names:
            if sname not in clients or sname not in datas:
                continue
            try:
                sync_pair(master, clients[sname], datas[master_name], datas[sname], cfg)
            except Exception as e:
                err_msg = f"[{master_name}] <=> [{sname}] 同步失败: {e}"
                log(f"❌ {err_msg}")
                LAST_SYNC_STATUS["last_error"] = err_msg

        save_accounts(accounts)
        LAST_SYNC_STATUS["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        log("🎉 本轮星型双向同步流程全部顺利结束。")
    except Exception as e:
        err_msg = f"同步过程发生全局异常: {e}"
        LAST_SYNC_STATUS["last_error"] = err_msg
        log(f"❌ {err_msg}")
    finally:
        LAST_SYNC_STATUS["running"] = False
        SYNC_LOCK.release()