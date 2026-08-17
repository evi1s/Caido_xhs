#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Caido 流量监听器（v8 — 配置文件驱动版）
========================================================
功能：
  1. 监听 Caido 代理流量（HTTP 轮询，增量处理）
  2. 按 config.yaml 中的 targets 过滤目标请求
  3. 从请求头 / 响应体提取指定字段（xy-platform-info / JSON 等）
  4. 自动生成 fingerprint / x_legacy_fid（算法与客户端一致）
  5. 写入 MongoDB（按 dedup_key 去重，同一值只保留最新一条）
  6. token 到期前自动用 PAT 续期（免运维）

配置：config.yaml（同目录），修改后重启生效。
依赖：pip install pymongo pyyaml websocket-client（websocket-client 仅 token 续期用）
"""
import os
import re
import json
import subprocess
import gzip
import time
import uuid
import base64
import hashlib
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import yaml
from pymongo import MongoClient, ASCENDING

# ------------------------------------------------------------
# 配置加载：config.yaml + 环境变量覆盖
# ------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_FILE", "/app/config.yaml")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 环境变量覆盖（Docker 部署时常用）
    env_map = {
        "CAIDO_URL": ("caido", "url"),
        "MONGO_HOST": ("mongo", "host"),
        "MONGO_PORT": ("mongo", "port"),
        "MONGO_USERNAME": ("mongo", "username"),
        "MONGO_PASSWORD": ("mongo", "password"),
        "MONGO_AUTH_SOURCE": ("mongo", "auth_source"),
        "MONGO_DB": ("mongo", "database"),
        "MONGO_COLLECTION": ("mongo", "collection"),
        "CAIDO_PAT": ("auth", "pat"),
        "TOKENS_FILE": ("auth", "tokens_file"),
    }
    for env, (sec, key) in env_map.items():
        if os.environ.get(env):
            cfg.setdefault(sec, {})[key] = os.environ[env]

    # 类型修正
    try:
        cfg["mongo"]["port"] = int(cfg["mongo"]["port"])
    except Exception:
        pass
    return cfg


CONFIG = load_config()


def cfg_get(*path, default=None):
    cur = CONFIG
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


# ------------------------------------------------------------
# 日志
# ------------------------------------------------------------
log_level = getattr(logging, str(cfg_get("log_level", default="INFO")).upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("caido-listener")

# ------------------------------------------------------------
# 常量
# ------------------------------------------------------------
CAIDO_URL = cfg_get("caido", "url", default="http://127.0.0.1:8080")
TOKENS_FILE = cfg_get("auth", "tokens_file", default="/app/tokens.json")
LAST_ID_FILE = cfg_get("last_id_file", default="/app/last_id.json")

LIST_QUERY = "query { requests(last: %d) { edges { node { id host method path } } } }"
REQ_QUERY = """query GetReq($id: ID!) {
  request(id: $id) {
    id host method path query isTls sni createdAt
    raw
    response { id statusCode createdAt raw }
  }
}"""


# ------------------------------------------------------------
# token 管理
# ------------------------------------------------------------
def load_tokens():
    """读取 tokens.json，兼容 accessToken / access_token 两种键"""
    try:
        with open(TOKENS_FILE) as f:
            raw = json.load(f)
    except Exception:
        return None
    return {
        "accessToken": raw.get("accessToken") or raw.get("access_token"),
        "refreshToken": raw.get("refreshToken") or raw.get("refresh_token"),
        "expiresAt": raw.get("expiresAt") or raw.get("expires_at"),
    }


def ensure_token(tokens):
    """token 即将过期时自动用 PAT 续期（token_fetcher.py 已内置 PAT 与 device flow）"""
    try:
        exp = (tokens or {}).get("expiresAt") or ""
        if not exp:
            return tokens
        et = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if (et - datetime.now(timezone.utc)).total_seconds() < 86400:
            log.info("token 即将过期(%s)，自动续期...", exp)
            subprocess.run(["python3", "/app/token_fetcher.py"], timeout=120,
                           cwd="/app", capture_output=True)
            tokens = load_tokens()
            log.info("自动续期完成，新 token 有效期至 %s",
                     (tokens or {}).get("expiresAt"))
    except Exception as e:
        log.warning("自动续期检查失败: %s", str(e)[:150])
    return tokens


# ------------------------------------------------------------
# GraphQL / 解码工具
# ------------------------------------------------------------
def http_graphql(query, variables=None, token=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(CAIDO_URL + "/graphql", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"http_error": str(e)}


def b64decode_bytes(s):
    if not s:
        return b""
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        try:
            return base64.b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            return b""


def decode_body(raw_b64):
    """解码 raw → (headers_dict, body_bytes)，处理 gzip"""
    data = b64decode_bytes(raw_b64)
    if not data:
        return {}, b""
    sep = b"\r\n\r\n"
    if sep in data:
        head, body = data.split(sep, 1)
    elif b"\n\n" in data:
        head, body = data.split(b"\n\n", 1)
    else:
        head, body = data, b""
    headers = {}
    try:
        lines = head.decode("utf-8", "ignore").split("\r\n")
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
    except Exception:
        pass
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    return headers, body


def parse_json_multi(body):
    if not body:
        return None
    try:
        txt = body.decode("utf-8", "ignore").strip()
        if txt.startswith("\ufeff"):
            txt = txt[1:]
        return json.loads(txt)
    except Exception:
        return None


def json_path(data, path):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


# ------------------------------------------------------------
# 提取
# ------------------------------------------------------------
def extract_field(headers, body, location):
    if location.startswith("response_json:"):
        path = location.split(":", 1)[1]
        obj = parse_json_multi(body)
        return json_path(obj, path) if obj else None
    if location.startswith("request_header_form:"):
        parts = location.split(":")
        hname, key = parts[1], parts[2]
        hval = headers.get(hname.lower())
        if not hval:
            return None
        for pair in hval.split("&"):
            kv = pair.split("=", 1)
            if len(kv) == 2:
                k = urllib.parse.unquote(kv[0])
                v = urllib.parse.unquote(kv[1])
                if k == key:
                    return v
        return None
    if location.startswith("request_header:"):
        hname = location.split(":", 1)[1]
        return headers.get(hname.lower())
    return None


def extract_all(headers, body):
    extracted, sources = {}, {}
    for fname, locs in (cfg_get("extract_fields", default={}) or {}).items():
        for loc in locs:
            val = extract_field(headers, body, loc)
            if val not in (None, ""):
                pp = (cfg_get("field_postprocess", default={}) or {}).get(fname, {})
                if "strip_prefix" in pp and isinstance(val, str) and val.startswith(pp["strip_prefix"]):
                    val = val[len(pp["strip_prefix"]):]
                extracted[fname] = val
                sources[fname] = loc
                break
    return extracted, sources


# ------------------------------------------------------------
# 生成字段（算法与目标 App 客户端一致）
# ------------------------------------------------------------
def generate_xhs_fingerprint(seed):
    """小红书指纹：上海时区时间戳 + md5(seed) + '00' + md5('shumei_ios_sec_key_'+key)[:14]"""
    tz = timezone(timedelta(hours=8))
    ts = datetime.now(tz).strftime("%Y%m%d%H%M%S")
    seed_md5 = hashlib.md5(str(seed).encode()).hexdigest()
    key = ts + seed_md5 + "00"
    return key + hashlib.md5(("shumei_ios_sec_key_" + key).encode()).hexdigest()[:14]


def generate_xhs_fid():
    return f"{int(time.time())}-0-0-{hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()}"


def apply_generated_fields(doc):
    for fname, gen in (cfg_get("generated_fields", default={}) or {}).items():
        if not gen or not gen.get("enabled"):
            continue
        gtype = gen.get("type", "")
        if gtype == "xhs_fingerprint":
            seed = doc.get(gen.get("input", ""), "")
            if seed:
                doc[fname] = generate_xhs_fingerprint(seed)
        elif gtype == "xhs_fid":
            doc[fname] = generate_xhs_fid()


# ------------------------------------------------------------
# 文档组装
# ------------------------------------------------------------
def build_doc(extracted):
    doc = {k: v for k, v in extracted.items() if v not in (None, "")}
    apply_generated_fields(doc)
    for k, v in (cfg_get("fixed_fields", default={}) or {}).items():
        if k not in doc:
            doc[k] = v
    return doc


# ------------------------------------------------------------
# 请求处理
# ------------------------------------------------------------
def handle_request(col, tokens, node):
    host = (node.get("host") or "").lower()
    path = node.get("path") or ""
    targets = cfg_get("targets", default=[]) or []
    if not any(host == t["host"].lower() and path.startswith(t["path_prefix"])
               for t in targets):
        return

    rid = node.get("id")
    if not rid:
        return

    full = None
    poll_int = cfg_get("polling", "detail_poll_interval", default=0.5)
    poll_max = cfg_get("polling", "detail_poll_max", default=12)
    for _ in range(poll_max):
        try:
            r = http_graphql(REQ_QUERY, {"id": rid}, token=tokens.get("accessToken"), timeout=10)
            full = r.get("data", {}).get("request")
            if full and full.get("response"):
                break
        except Exception as e:
            log.warning("查询异常: %s", str(e)[:120])
        time.sleep(poll_int)
    if not full:
        log.warning("未拿到完整请求: id=%s", rid)
        return

    req_headers, _ = decode_body(full.get("raw") or "")
    resp_raw = (full.get("response") or {}).get("raw") or ""
    _, resp_body = decode_body(resp_raw)

    extracted, sources = extract_all(req_headers, resp_body)
    if not extracted:
        log.info("命中目标但未提取到值: id=%s %s %s", rid, host, path)
        return

    doc = build_doc(extracted)

    dedup_key = cfg_get("dedup_key", default="userid")
    dedup_val = doc.get(dedup_key, "")
    if not dedup_val:
        log.info("无去重键(%s)跳过写入: id=%s", dedup_key, rid)
        return

    try:
        col.update_one({dedup_key: dedup_val}, {"$set": doc}, upsert=True)
        log.info("已写入 %s=%s: %s", dedup_key, dedup_val, json.dumps(doc, ensure_ascii=False))
    except Exception as e:
        log.error("写入失败 %s=%s: %s", dedup_key, dedup_val, str(e)[:200])


# ------------------------------------------------------------
# HTTP 轮询
# ------------------------------------------------------------
def load_last_id():
    try:
        with open(LAST_ID_FILE) as f:
            return int(json.load(f).get("last_id", 0))
    except Exception:
        return 0


def save_last_id(n):
    try:
        with open(LAST_ID_FILE, "w") as f:
            json.dump({"last_id": n}, f)
    except Exception:
        pass


def poll_loop(tokens, col):
    last_id = load_last_id()
    scan_int = cfg_get("polling", "scan_interval", default=2.0)
    batch = cfg_get("polling", "batch_size", default=30)
    log.info("HTTP 轮询启动，起始 last_id=%s", last_id)
    while True:
        tokens = ensure_token(tokens)
        try:
            q = LIST_QUERY % batch
            r = http_graphql(q, token=tokens.get("accessToken"), timeout=10)
            if r.get("http_error"):
                log.error("轮询请求失败: %s", r["http_error"][:150])
                time.sleep(scan_int)
                continue
            edges = r.get("data", {}).get("requests", {}).get("edges", [])
            if not edges:
                time.sleep(scan_int)
                continue
            new_nodes = []
            for e in edges:
                n = e.get("node") or {}
                try:
                    nid = int(n.get("id", 0))
                except Exception:
                    continue
                if nid > last_id:
                    new_nodes.append(n)
            if new_nodes:
                log.info("发现 %d 个新请求（last_id=%s）", len(new_nodes), last_id)
                for n in sorted(new_nodes, key=lambda x: int(x.get("id", 0))):
                    try:
                        handle_request(col, tokens, n)
                    except Exception as e:
                        log.error("处理请求异常 id=%s: %s", n.get("id"), str(e)[:150])
                new_max = max(int(n.get("id", 0)) for n in new_nodes)
                if new_max > last_id:
                    last_id = new_max
                    save_last_id(last_id)
        except Exception as e:
            log.error("轮询异常: %s", str(e)[:200])
        time.sleep(scan_int)


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def main():
    tokens = load_tokens()
    if not tokens or not tokens.get("accessToken"):
        log.error("token 加载失败（%s），先运行 token_fetcher.py", TOKENS_FILE)
        return

    m = cfg_get("mongo", default={})
    try:
        client = MongoClient(
            host=m.get("host", "127.0.0.1"),
            port=int(m.get("port", 27017)),
            username=m.get("username"),
            password=m.get("password"),
            authSource=m.get("auth_source", "admin"),
            serverSelectionTimeoutMS=5000,
        )
        db = client[m.get("database", "datademo")]
        col = db[m.get("collection", "devicedemo")]
        col.find_one()
        log.info("MongoDB 连接成功: %s:%s/%s.%s",
                 m.get("host"), m.get("port"), m.get("database"), m.get("collection"))
    except Exception as e:
        log.error("MongoDB 连接失败: %s", str(e)[:200])
        return

    dedup_key = cfg_get("dedup_key", default="userid")
    try:
        col.create_index([(dedup_key, ASCENDING)], unique=True, name=f"{dedup_key}_unique")
        log.info("唯一索引就绪: %s", dedup_key)
    except Exception as e:
        log.warning("建索引提示: %s", str(e)[:150])

    poll_loop(tokens, col)


if __name__ == "__main__":
    main()
