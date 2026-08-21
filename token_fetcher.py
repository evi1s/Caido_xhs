#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
token_fetcher.py — 通过 PAT 自动获取 Caido access token（Device Authorization Flow）
用法：python3 token_fetcher.py
说明：
  - PAT 从 config.yaml 读取（auth.pat），永久有效
  - 每次运行都会重新获取 access_token + refresh_token，写入 tokens.json
  - 监听器会在 token 到期前自动调用本脚本续期（auto_renew: true）
  - 自动将 caido.url 主机名解析为 IP（绕过 Caido Host 白名单 403）
  - 支持 CAIDO_URL 环境变量覆盖（listener 调用时传入解析后的 URL）
"""
import json
import os
import time
import socket
import ipaddress
import urllib.parse
import urllib.request

import yaml
import websocket

# ---- 读取配置 ----
CONFIG_PATH = os.environ.get("CONFIG_FILE", "/app/config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}

PAT = CFG.get("auth", {}).get("pat", "")
TOKENS_FILE = CFG.get("auth", {}).get("tokens_file", "/app/tokens.json")
CLOUD = "https://api.caido.io"


def resolve_url(url):
    """主机名自动解析为 IP（Caido Host 白名单只接受 IP 形式的 Host 头）"""
    try:
        p = urllib.parse.urlparse(url)
        host = p.hostname or ""
        if not host:
            return url
        # 已是 IP 则直接用
        try:
            ipaddress.ip_address(host)
            return url
        except ValueError:
            pass
        port = p.port or (443 if p.scheme == "https" else 80)
        ips = []
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
        if ips:
            return f"{p.scheme}://{ips[0]}:{port}{p.path or ''}"
    except Exception:
        pass
    return url


# 环境变量优先（listener 调用时传入解析后的 URL），否则读 config.yaml 并自动解析
INSTANCE = os.environ.get("CAIDO_URL") or resolve_url(
    CFG.get("caido", {}).get("url", "http://127.0.0.1:8080"))
GRAPHQL_URL = INSTANCE + "/graphql"
WS_URL = INSTANCE.replace("http", "ws") + "/ws/graphql"

if not PAT:
    print("ERROR: config.yaml 中 auth.pat 为空，请填写 Caido Personal Access Token")
    raise SystemExit(1)

START_FLOW = """mutation StartAuthFlow {
  startAuthenticationFlow {
    request { id userCode }
    error { ... on AuthenticationUserError { code reason } }
  }
}"""

AUTH_TOKEN_SUB = """subscription CreatedAuthToken($requestId: ID!) {
  createdAuthenticationToken(requestId: $requestId) {
    token { accessToken expiresAt refreshToken scopes }
    error { ... on AuthenticationUserError { code reason } }
  }
}"""


def http_graphql(query, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers=headers)
    r = urllib.request.urlopen(req, timeout=15)
    d = json.loads(r.read().decode())
    if d.get("errors"):
        raise RuntimeError("GraphQL errors: " + json.dumps(d["errors"])[:300])
    return d.get("data") or {}


def cloud_get(path, query_params):
    """GET 云 API（新端点：/oauth2/device/*）"""
    url = CLOUD + path + "?" + urllib.parse.urlencode(query_params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + PAT,
        "Accept": "application/json",
    })
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)[:200]}


def cloud_post(path, query_params):
    """POST 云 API（新端点：/oauth2/device/*）"""
    url = CLOUD + path + "?" + urllib.parse.urlencode(query_params)
    req = urllib.request.Request(url, method="POST", headers={
        "Authorization": "Bearer " + PAT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)[:200]}


def ws_subscribe_token(request_id, timeout_s=20):
    """用 websocket-client 手动实现 graphql-transport-ws，订阅 createdAuthenticationToken"""
    ws = websocket.create_connection(WS_URL, timeout=timeout_s,
                                     header=["Sec-WebSocket-Protocol: graphql-transport-ws"])
    try:
        ws.send(json.dumps({"type": "connection_init", "payload": {}}))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("type") == "connection_ack":
                ws.send(json.dumps({
                    "type": "subscribe",
                    "id": "1",
                    "payload": {
                        "query": AUTH_TOKEN_SUB,
                        "variables": {"requestId": request_id},
                    },
                }))
            elif msg.get("type") == "next":
                data = msg.get("payload", {}).get("data", {}) or {}
                auth = data.get("createdAuthenticationToken") or {}
                if auth.get("token"):
                    return auth["token"]
                if auth.get("error"):
                    return {"error": auth["error"]}
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


def datetime_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main():
    for attempt in range(1, 8):
        try:
            print(f"[try {attempt}] start flow...")
            data = http_graphql(START_FLOW)
            auth_flow = data.get("startAuthenticationFlow") or {}
            req = auth_flow.get("request") or {}
            request_id = req.get("id")
            user_code = req.get("userCode", "")
            if not request_id:
                print("start flow error:", json.dumps(auth_flow.get("error", {}))[:200])
                time.sleep(3)
                continue

            # 1) 查设备信息（新端点，用 user_code），拿 scopes
            print(f"[try {attempt}] get device information (user_code={user_code})...")
            status, dev = cloud_get("/oauth2/device/information", {"user_code": user_code})
            if status != 200:
                print(f"[try {attempt}] device information status={status}: {json.dumps(dev)[:200]}")
                time.sleep(3)
                continue
            scopes = [s.get("name") for s in (dev.get("scopes") or []) if s.get("name")]
            if not scopes:
                print(f"[try {attempt}] no scopes returned: {json.dumps(dev)[:200]}")
                time.sleep(3)
                continue
            print(f"[try {attempt}] scopes: {scopes}")

            # 2) 批准设备（新端点，用 user_code + scope）
            print(f"[try {attempt}] approve device...")
            status, resp = cloud_post("/oauth2/device/approve", {
                "user_code": user_code,
                "scope": ",".join(scopes),
            })
            print(f"[try {attempt}] approve status={status}")
            if status != 200:
                print("approve resp:", json.dumps(resp)[:300])
                time.sleep(3)
                continue

            # 3) 订阅换 token
            print(f"[try {attempt}] waiting for auth token...")
            token = ws_subscribe_token(request_id)
            if token and token.get("accessToken"):
                out = {
                    "access_token": token["accessToken"],
                    "refresh_token": token.get("refreshToken", ""),
                    "expires_at": token.get("expiresAt", ""),
                    "fetched_at": datetime_now_iso(),
                }
                with open(TOKENS_FILE, "w") as f:
                    json.dump(out, f, indent=2)
                print(f"SUCCESS: token saved to {TOKENS_FILE}, expires: {out['expires_at']}")
                return
            print("[try %d] token not received yet, retry..." % attempt)
        except Exception as e:
            print(f"[try {attempt}] error: {str(e)[:200]}")
        time.sleep(3)
    print("FAILED: could not get token after retries")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
