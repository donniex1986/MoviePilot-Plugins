import json
import hashlib
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests

from app import schemas
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class IKuaiHomePage(_PluginBase):
    """爱快本地路由 HomePage 自定义 API 插件。"""

    plugin_name = "iKuai HomePage"
    plugin_desc = "将爱快本地路由监控数据转换为 Homepage customapi 接口。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/homepage.png"
    plugin_version = "1.5"
    plugin_author = "Donniex，qqcomeup"
    author_url = "https://github.com/ikuaidev/ikuai-cli"
    plugin_config_prefix = "ikuaihomepage_"
    plugin_order = 32
    plugin_label = "工具,网络,监控,Homepage"
    auth_level = 1
    plugin_doc_url = "https://github.com/qqcomeup/MoviePilot-Plugins/tree/main/plugins.v2/ikuaihomepage"

    _enabled = False
    _source_plugin_id = "IkuaiRouterBackup"
    _router_url = ""
    _auth_mode = "ikuairouterbackup"
    _api_base_path = "/api/v4.0"
    _api_token = ""
    _web_username = ""
    _web_password = ""
    _request_timeout = 5
    _cache_ttl = 30
    _verify_ssl = False
    _user_limit = 20
    _last_error = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._source_plugin_id = "IkuaiRouterBackup"
        self._router_url = ""
        self._auth_mode = "ikuairouterbackup"
        self._api_base_path = "/api/v4.0"
        self._api_token = ""
        self._web_username = ""
        self._web_password = ""
        self._request_timeout = 5
        self._cache_ttl = 30
        self._verify_ssl = False
        self._user_limit = 20
        self._last_error = ""
        self._session = requests.Session()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_updated_at: Dict[str, float] = {}
        self._web_logged_in = False
        self._router_backup_client = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._source_plugin_id = str(config.get("source_plugin_id") or "IkuaiRouterBackup").strip()
        auth_mode = str(config.get("auth_mode") or "ikuairouterbackup").strip().lower()
        self._auth_mode = auth_mode if auth_mode in ("ikuairouterbackup", "token") else "ikuairouterbackup"
        self._router_url = self._normalize_router_url(str(config.get("router_url") or ""))
        self._api_base_path = self._normalize_api_base_path(str(config.get("api_base_path") or "/api/v4.0"))
        self._api_token = str(config.get("api_token") or "").strip()
        self._request_timeout = self._positive_int(config.get("request_timeout"), 5)
        self._cache_ttl = self._positive_int(config.get("cache_ttl"), 30)
        self._verify_ssl = bool(config.get("verify_ssl", False))
        self._user_limit = self._positive_int(config.get("user_limit"), 20)
        self._load_source_plugin_config()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/summary",
                "endpoint": self.summary,
                "methods": ["GET"],
                "summary": "爱快总览",
                "description": "返回 Homepage customapi 可用的爱快路由总览数据。",
            },
            {
                "path": "/system",
                "endpoint": self.system,
                "methods": ["GET"],
                "summary": "爱快系统状态",
                "description": "返回爱快本地路由系统状态。",
            },
            {
                "path": "/interfaces",
                "endpoint": self.interfaces,
                "methods": ["GET"],
                "summary": "爱快接口状态",
                "description": "返回爱快本地路由接口状态和流量。",
            },
            {
                "path": "/users",
                "endpoint": self.users,
                "methods": ["GET"],
                "summary": "爱快在线客户端",
                "description": "返回爱快本地路由在线客户端统计。",
            },
        ]

    def summary(self, apikey: str) -> Any:
        """返回爱快总览数据。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            system_raw, system_stale = self._load_cached("system", lambda: self._request_router("/monitoring/system"))
            traffic_raw, traffic_stale = self._load_cached(
                "interfaces_traffic", lambda: self._request_router("/monitoring/interfaces-traffic")
            )
            users_raw, users_stale = self._load_cached(
                "users_1", lambda: self._request_router("/monitoring/clients-online", {"page": 1, "limit": 1})
            )
            return self._build_summary(system_raw, traffic_raw, users_raw, system_stale or traffic_stale or users_stale)
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"IKuaiHomePage 获取总览失败: {err}")
            return self._error_payload(str(err))

    def system(self, apikey: str) -> Any:
        """返回爱快系统状态。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            raw, stale = self._load_cached("system", lambda: self._request_router("/monitoring/system"))
            return self._build_system(raw, stale=stale)
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"IKuaiHomePage 获取系统状态失败: {err}")
            return self._error_payload(str(err))

    def interfaces(self, apikey: str) -> Any:
        """返回爱快接口状态和流量。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            status_raw, status_stale = self._load_cached(
                "interfaces_status", lambda: self._request_router("/monitoring/interfaces-status")
            )
            traffic_raw, traffic_stale = self._load_cached(
                "interfaces_traffic", lambda: self._request_router("/monitoring/interfaces-traffic")
            )
            return self._build_interfaces(status_raw, traffic_raw, stale=status_stale or traffic_stale)
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"IKuaiHomePage 获取接口状态失败: {err}")
            return self._error_payload(str(err))

    def users(self, apikey: str, limit: Any = None) -> Any:
        """返回爱快在线客户端统计。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        user_limit = self._positive_int(limit, self._user_limit)
        try:
            raw, stale = self._load_cached(
                f"users_{user_limit}",
                lambda: self._request_router("/monitoring/clients-online", {"page": 1, "limit": user_limit}),
            )
            return self._build_users(raw, stale=stale)
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"IKuaiHomePage 获取在线客户端失败: {err}")
            return self._error_payload(str(err))

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "verify_ssl", "label": "校验 SSL 证书"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_token",
                                            "label": "爱快个人 API Token",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_base_path",
                                            "label": "爱快 API 路径前缀",
                                            "placeholder": "/api/v4.0",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "auth_mode",
                                            "label": "爱快认证方式",
                                            "items": [
                                                {"title": "来源插件用户名密码", "value": "ikuairouterbackup"},
                                                {"title": "个人 API Token", "value": "token"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "router_url",
                                            "label": "爱快路由地址",
                                            "placeholder": "http://192.168.1.1",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "source_plugin_id",
                                            "label": "爱快来源插件 ID",
                                            "placeholder": "IkuaiRouterBackup",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "request_timeout", "label": "请求超时秒数", "type": "number"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "cache_ttl", "label": "缓存秒数", "type": "number"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "user_limit", "label": "在线客户端数量", "type": "number"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "选择“个人 API Token”时填写路由地址和 Token；使用来源插件用户名密码时，将从来源插件读取连接配置。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "auth_mode": "ikuairouterbackup",
            "source_plugin_id": "IkuaiRouterBackup",
            "router_url": "",
            "api_token": "",
            "api_base_path": "/api/v4.0",
            "request_timeout": 5,
            "cache_ttl": 30,
            "verify_ssl": False,
            "user_limit": 20,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页面。"""
        api_base = "/api/v1/plugin/IKuaiHomePage"
        examples = self._homepage_examples(api_base)
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"路由地址: {self._router_url or '-'}；最近错误: {self._last_error or '无'}",
                },
            },
            {
                "component": "div",
                "props": {
                    "class": "d-flex flex-wrap ga-2 mb-4",
                },
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "href": self.plugin_doc_url,
                            "target": "_blank",
                            "variant": "tonal",
                            "color": "error",
                            "prepend-icon": "mdi-open-in-new",
                        },
                        "text": "跳转到使用说明",
                    }
                ],
            },
            {
                "component": "VTextarea",
                "props": {
                    "model-value": examples,
                    "label": "Homepage YAML 示例",
                    "readonly": True,
                    "rows": 18,
                },
            },
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    def _check_apikey(self, apikey: str) -> bool:
        """检查 MoviePilot API Token。"""
        return bool(apikey) and apikey == settings.API_TOKEN

    def _load_source_plugin_config(self) -> None:
        """读取 IkuaiRouterBackup 插件中的爱快连接配置。"""
        source_config = {}
        if self._auth_mode != "token" or not self._router_url:
            try:
                source_config = self.get_config(self._source_plugin_id) or {}
            except Exception as err:
                if self._auth_mode != "token":
                    self._last_error = f"读取 {self._source_plugin_id} 配置失败: {err}"

        if not self._router_url:
            self._router_url = self._normalize_router_url(str(source_config.get("ikuai_url") or ""))
        self._web_username = str(source_config.get("ikuai_username") or "").strip()
        self._web_password = str(source_config.get("ikuai_password") or "")
        if self._auth_mode == "token":
            if not self._router_url:
                self._last_error = "未配置爱快路由地址"
            elif not self._api_token:
                self._last_error = "未配置爱快个人 API Token"
            return
        if not source_config:
            self._last_error = f"未读取到 {self._source_plugin_id} 配置"
        elif not self._web_username or not self._web_password:
            self._last_error = f"{self._source_plugin_id} 未配置爱快后台用户名或密码"

    @staticmethod
    def _normalize_router_url(value: str) -> str:
        """规范化爱快路由地址。"""
        router_url = str(value or "").strip().rstrip("/")
        if not router_url:
            return ""
        if not router_url.startswith(("http://", "https://")):
            router_url = f"http://{router_url}"
        return router_url

    @staticmethod
    def _normalize_api_base_path(value: str) -> str:
        """规范化爱快本地 API 前缀。"""
        api_base_path = str(value or "").strip().strip("/")
        if not api_base_path:
            return "/api/v4.0"
        return "/" + api_base_path.rstrip("/")

    @staticmethod
    def _sanitize_json_body(raw: bytes) -> bytes:
        """将部分固件返回的裸 nil 值归一为 null。"""
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"(?<=[:\[,])\s*nil\s*(?=[,\]}])", "null", text)
        return text.encode("utf-8")

    def _request_local_api(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """请求爱快本地 API 并返回数据载荷。"""
        if not self._router_url or not self._api_token:
            raise ValueError("未配置路由地址或 API Token")

        api_path = "/" + str(path or "").lstrip("/")
        if not api_path.startswith("/api/"):
            api_path = self._api_base_path + api_path
        url = self._router_url + api_path
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_token}",
        }
        headers["Authorization"] = f"Bearer {self._api_token}"
        response = self._session.request(
            "GET",
            url,
            params=params or None,
            headers=headers,
            timeout=self._request_timeout,
            verify=self._verify_ssl,
        )
        body = self._sanitize_json_body(response.content)
        try:
            envelope = json.loads(body.decode("utf-8"))
        except Exception as err:
            raise ValueError(f"HTTP {response.status_code}: 非 JSON 响应") from err

        code = envelope.get("code", 0)
        if response.status_code >= 400 or code not in (0, 20000):
            message = envelope.get("message") or envelope.get("msg") or response.text
            raise RuntimeError(f"HTTP {response.status_code} code={code}: {message}")

        if "data" in envelope and envelope.get("data") is not None:
            return envelope.get("data")
        if "results" in envelope and envelope.get("results") is not None:
            return envelope.get("results")
        return envelope

    def _request_router(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """按当前认证方式请求爱快路由器。"""
        if self._auth_mode == "token":
            return self._request_local_api(path, params=params)
        return self._request_web_api(path, params=params)

    def _request_web_api(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """通过爱快后台会话接口读取监控数据。"""
        api_path = "/" + str(path or "").lstrip("/")
        if api_path == "/monitoring/system":
            results = self._call_web_action("homepage", param={"TYPE": "sysstat"})
            return self._normalize_web_system(self._as_dict(results).get("sysstat", {}))
        if api_path == "/monitoring/interfaces-status":
            results = self._call_web_action("lan", param={"TYPE": "ether_info,snapshoot,wan_vlan_fail,stream"})
            return self._normalize_web_interfaces_status(results)
        if api_path == "/monitoring/interfaces-traffic":
            results = self._call_web_action("monitor_iface", param={"TYPE": "iface_check,iface_stream"})
            return self._as_list(self._as_dict(results).get("iface_stream", []))
        if api_path == "/monitoring/clients-online":
            limit = self._positive_int((params or {}).get("limit"), self._user_limit)
            return self._call_web_action(
                "monitor_lanip",
                param={
                    "TYPE": "data,total",
                    "ORDER_BY": "upload",
                    "ORDER": "desc",
                    "limit": f"0,{limit}",
                },
            )
        raise ValueError(f"后台账号密码模式不支持该接口: {api_path}")

    def _web_login(self) -> None:
        """登录爱快后台并保持会话 Cookie。"""
        if self._web_logged_in:
            return
        if not self._router_url or not self._web_username or not self._web_password:
            raise ValueError("未配置路由地址、后台用户名或后台密码")

        if self._login_with_router_backup_client():
            return

        login_url = urljoin(self._router_url + "/", "Action/login")
        password_md5 = hashlib.md5(self._web_password.encode("utf-8")).hexdigest()
        response = self._session.post(
            login_url,
            data=json.dumps({"username": self._web_username, "passwd": password_md5}),
            headers={"Content-Type": "application/json"},
            timeout=self._request_timeout,
            verify=self._verify_ssl,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"爱快后台登录失败: HTTP {response.status_code}")

        sess_key = response.cookies.get("sess_key")
        if not sess_key:
            match = re.search(r"sess_key=([^;]+)", response.headers.get("Set-Cookie", ""))
            sess_key = match.group(1) if match else ""
        if not sess_key:
            raise RuntimeError("爱快后台登录失败: 未返回 sess_key")

        cookie = f"username={quote(self._web_username)}; sess_key={sess_key}; login=1"
        self._session.headers.update({"Cookie": cookie})
        self._web_logged_in = True

    def _login_with_router_backup_client(self) -> bool:
        """优先复用 IkuaiRouterBackup 的客户端登录实现。"""
        try:
            from app.plugins.ikuairouterbackup.ikuai.client import IkuaiClient
        except Exception:
            return False

        try:
            client = IkuaiClient(self._router_url, self._web_username, self._web_password, self.plugin_name)
            if not client.login():
                return False
            self._router_backup_client = client
            self._session = client.session
            self._web_logged_in = True
            return True
        except Exception as err:
            logger.warning(f"IKuaiHomePage 复用 IkuaiRouterBackup 客户端登录失败: {err}")
            return False

    def _call_web_action(self, func_name: str, action: str = "show", param: Optional[Dict[str, Any]] = None) -> Any:
        """调用爱快后台 Action/call 接口并返回 results。"""
        self._web_login()
        call_url = urljoin(self._router_url + "/", "Action/call")
        payload = {"func_name": func_name, "action": action}
        if param is not None:
            payload["param"] = param
        response = self._session.post(
            call_url,
            data=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": self._router_url,
                "Referer": self._router_url + "/",
            },
            timeout=self._request_timeout,
            verify=self._verify_ssl,
        )
        try:
            envelope = response.json()
        except Exception as err:
            raise ValueError(f"HTTP {response.status_code}: 非 JSON 响应") from err
        code = envelope.get("code", envelope.get("Result", 0))
        message = envelope.get("message") or envelope.get("ErrMsg") or envelope.get("errmsg") or response.text
        if response.status_code >= 400 or code not in (0, 30000):
            if code in (1002, 1003):
                self._web_logged_in = False
            raise RuntimeError(f"HTTP {response.status_code} code={code}: {message}")
        if "results" in envelope and envelope.get("results") is not None:
            return envelope.get("results")
        if "Data" in envelope and envelope.get("Data") is not None:
            return envelope.get("Data")
        return envelope

    def _normalize_web_system(self, raw: Any) -> Dict[str, Any]:
        """将爱快后台首页系统信息转换为统一系统字段。"""
        data = self._as_dict(raw)
        memory = self._as_dict(data.get("memory"))
        verinfo = self._as_dict(data.get("verinfo"))
        stream = self._as_dict(data.get("stream"))
        online_user = self._as_dict(data.get("online_user"))
        cpu_values = self._as_list(data.get("cpu"))
        cpu = cpu_values[0] if cpu_values else data.get("cpu")
        payload = {
            "name": data.get("hostname") or "iKuai",
            "hostname": data.get("hostname") or "iKuai",
            "cpu": cpu,
            "memory": memory.get("used"),
            "uptime": data.get("uptime"),
            "version": verinfo.get("version") or verinfo.get("verstring"),
            "wan_ip": data.get("ip_addr"),
            "online_users": online_user.get("count"),
        }
        if stream:
            payload["upload"] = stream.get("upload", 0)
            payload["download"] = stream.get("download", 0)
        return payload

    def _normalize_web_interfaces_status(self, raw: Any) -> List[Dict[str, Any]]:
        """将爱快后台接口状态转换为列表。"""
        data = self._as_dict(raw)
        items: List[Dict[str, Any]] = []
        for item in self._as_list(data.get("snapshoot_wan")):
            row = self._as_dict(item).copy()
            row["type"] = "wan"
            row["name"] = row.get("interface")
            row["status"] = "up" if row.get("internet") or row.get("ip_addr") else "-"
            items.append(row)
        for item in self._as_list(data.get("snapshoot_lan")):
            row = self._as_dict(item).copy()
            row["type"] = "lan"
            row["name"] = row.get("interface")
            row["status"] = "up"
            items.append(row)
        return items

    def _load_cached(self, key: str, loader) -> Tuple[Any, bool]:
        """按 key 加载带短缓存的数据。"""
        now = time.time()
        if key in self._cache and now - self._cache_updated_at.get(key, 0) < self._cache_ttl:
            return self._cache[key], False
        try:
            data = loader()
            self._cache[key] = data
            self._cache_updated_at[key] = now
            return data, False
        except Exception:
            if key in self._cache:
                return self._cache[key], True
            raise

    def _build_system(self, raw: Any, stale: bool = False) -> Dict[str, Any]:
        """构建系统状态响应。"""
        data = self._as_dict(raw)
        cpu = self._pick(data, "cpu", "cpu_usage", "cpu_percent", default=0)
        memory = self._pick(data, "memory", "mem", "mem_used", "memory_percent", default=0)
        disk = self._pick(data, "disk", "disk_used", "disk_percent", default=0)
        uptime = self._pick(data, "uptime", "sysuptime", "run_time", default=0)
        return {
            "status": "ok",
            "router_name": str(self._pick(data, "name", "hostname", "router_name", default="iKuai") or "iKuai"),
            "cpu": self._format_percent(cpu),
            "memory": self._format_percent(memory),
            "disk": self._format_percent(disk),
            "uptime": self._format_uptime(uptime),
            "version": str(self._pick(data, "version", "firmware", "router_ver", default="-") or "-"),
            "wan_ip": str(self._pick(data, "wan_ip", "reflexive_address", "ip_addr", default="-") or "-"),
            "cpu_percent": self._to_int(cpu),
            "memory_percent": self._to_int(memory),
            "disk_percent": self._to_int(disk),
            "uptime_seconds": self._to_int(uptime),
            "updated_at": self._now_text(),
            "stale": stale,
        }

    def _build_interfaces(self, status_raw: Any, traffic_raw: Any, stale: bool = False) -> Dict[str, Any]:
        """构建接口状态响应。"""
        statuses = self._as_list(status_raw)
        traffic = self._as_list(traffic_raw)
        traffic_by_name = {str(self._pick(item, "name", "interface", "iface", default="")): self._as_dict(item) for item in traffic}
        items = []
        total_up = 0
        total_down = 0
        wan_count = 0

        for item in statuses or traffic:
            status_item = self._as_dict(item)
            name = str(self._pick(status_item, "name", "interface", "iface", default="") or "")
            traffic_item = traffic_by_name.get(name, status_item)
            upload = self._to_int(self._pick(traffic_item, "upload", "up", "up_speed", default=0))
            download = self._to_int(self._pick(traffic_item, "download", "down", "down_speed", default=0))
            total_up += upload
            total_down += download
            iface_type = str(self._pick(status_item, "type", "device", "role", default="")).lower()
            if "wan" in name.lower() or "wan" in iface_type:
                wan_count += 1
            items.append(
                {
                    "name": name or "-",
                    "ip": str(self._pick(status_item, "ip", "ip_addr", "address", default="-") or "-"),
                    "status": str(self._pick(status_item, "status", "link", default="-") or "-"),
                    "up_speed": self._format_speed(upload),
                    "down_speed": self._format_speed(download),
                }
            )

        primary = next((item for item in items if "wan" in item["name"].lower()), items[0] if items else {})
        return {
            "status": "ok",
            "interfaces": len(items),
            "wan_count": wan_count,
            "up_speed": self._format_speed(total_up),
            "down_speed": self._format_speed(total_down),
            "up_speed_bps": total_up,
            "down_speed_bps": total_down,
            "primary_interface": primary.get("name", "-"),
            "primary_ip": primary.get("ip", "-"),
            "items": items,
            "updated_at": self._now_text(),
            "stale": stale,
        }

    def _build_users(self, raw: Any, stale: bool = False) -> Dict[str, Any]:
        """构建在线客户端响应。"""
        raw_dict = self._as_dict(raw)
        users = self._as_list(raw)
        total_count = self._to_int(self._pick(raw_dict, "total", "count", default=len(users)))
        items = []
        total_upload = 0
        total_download = 0
        top_user = "-"
        top_download = -1

        for user in users:
            data = self._as_dict(user)
            upload = self._to_int(self._pick(data, "upload", "up", default=0))
            download = self._to_int(self._pick(data, "download", "down", default=0))
            total_upload += upload
            total_download += download
            name = str(self._pick(data, "hostname", "name", "username", "comment", default="-") or "-")
            if download > top_download:
                top_download = download
                top_user = name
            items.append(
                {
                    "name": name,
                    "hostname": str(self._pick(data, "hostname", "name", default=name) or name),
                    "ip": str(self._pick(data, "ip", "ip_addr", "ip_addr_int", default="-") or "-"),
                    "mac": str(self._pick(data, "mac", default="-") or "-"),
                    "interface": str(self._pick(data, "interface", "device", default="-") or "-"),
                    "upload": self._format_speed(upload),
                    "download": self._format_speed(download),
                    "uptime": self._format_uptime(self._pick(data, "uptime", "connect_time", default=0)),
                }
            )

        return {
            "status": "ok",
            "online_users": total_count,
            "online_users_count": total_count,
            "total_upload": self._format_bytes(total_upload),
            "total_download": self._format_bytes(total_download),
            "top_user": top_user,
            "top_user_down": self._format_speed(max(top_download, 0)),
            "users": items,
            "updated_at": self._now_text(),
            "stale": stale,
        }

    def _build_summary(self, system_raw: Any, traffic_raw: Any, users_raw: Any, stale: bool = False) -> Dict[str, Any]:
        """构建总览响应。"""
        system_payload = self._build_system(system_raw, stale=stale)
        traffic_payload = self._build_interfaces([], traffic_raw, stale=stale)
        users_payload = self._build_users(users_raw, stale=stale)
        return {
            "status": "ok",
            "router_name": system_payload.get("router_name", "iKuai"),
            "online_users": users_payload.get("online_users", 0),
            "online_users_count": users_payload.get("online_users_count", 0),
            "cpu": system_payload.get("cpu", "0%"),
            "memory": system_payload.get("memory", "0%"),
            "uptime": system_payload.get("uptime", "-"),
            "up_speed": traffic_payload.get("up_speed", "0 B/s"),
            "down_speed": traffic_payload.get("down_speed", "0 B/s"),
            "wan_ip": system_payload.get("wan_ip", "-"),
            "version": system_payload.get("version", "-"),
            "cpu_percent": system_payload.get("cpu_percent", 0),
            "memory_percent": system_payload.get("memory_percent", 0),
            "up_speed_bps": traffic_payload.get("up_speed_bps", 0),
            "down_speed_bps": traffic_payload.get("down_speed_bps", 0),
            "uptime_seconds": system_payload.get("uptime_seconds", 0),
            "updated_at": self._now_text(),
            "stale": stale,
        }

    @staticmethod
    def _pick(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
        """从字典中按多个候选字段取值。"""
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        return default

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        """将值转换为字典。"""
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        """将值转换为列表。"""
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("data", "results", "items", "list"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return nested
            return [value]
        return []

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """转换为正整数。"""
        try:
            number = int(value)
            return number if number > 0 else default
        except Exception:
            return default

    @staticmethod
    def _to_int(value: Any) -> int:
        """转换为整数。"""
        try:
            if isinstance(value, str) and value.endswith("%"):
                value = value[:-1]
            return int(float(value))
        except Exception:
            return 0

    @classmethod
    def _format_percent(cls, value: Any) -> str:
        """格式化百分比。"""
        return f"{cls._to_int(value)}%"

    @classmethod
    def _format_bytes(cls, value: Any) -> str:
        """格式化字节数。"""
        number = float(cls._to_int(value))
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while number >= 1024 and idx < len(units) - 1:
            number /= 1024
            idx += 1
        if idx == 0:
            return f"{int(number)} {units[idx]}"
        return f"{number:.1f} {units[idx]}"

    @classmethod
    def _format_speed(cls, value: Any) -> str:
        """格式化速率。"""
        return f"{cls._format_bytes(value)}/s"

    @classmethod
    def _format_uptime(cls, value: Any) -> str:
        """格式化运行时长。"""
        seconds = cls._to_int(value)
        if seconds <= 0:
            return "-"
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @staticmethod
    def _now_text() -> str:
        """返回当前时间文本。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _error_payload(cls, message: str, stale: bool = False) -> Dict[str, Any]:
        """构建稳定错误响应。"""
        return {
            "status": "error",
            "message": f"iKuai 本地 API 请求失败: {message}",
            "updated_at": cls._now_text(),
            "stale": stale,
        }

    @staticmethod
    def _homepage_examples(api_base: str) -> str:
        """生成 Homepage YAML 示例。"""
        return f"""- iKuai:
    icon: router.png
    href: <ROUTER_LAN_URL>
    widgets:
      - type: customapi
        url: http://moviepilot:3001{api_base}/summary?apikey={{{{HOMEPAGE_VAR_MP_APIKEY}}}}
        refreshInterval: 10000
        mappings:
          - field: online_users
            label: 在线
          - field: cpu
            label: CPU
          - field: memory
            label: 内存
          - field: version
            label: 版本
      - type: customapi
        display: list
        url: http://moviepilot:3001{api_base}/summary?apikey={{{{HOMEPAGE_VAR_MP_APIKEY}}}}
        refreshInterval: 10000
        mappings:
          - field: down_speed
            label: 下载
          - field: up_speed
            label: 上传
          - field: uptime
            label: 运行"""