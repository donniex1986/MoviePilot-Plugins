import asyncio
import copy
import math
import ssl
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import websockets
from fnos import FnosClient, ResourceMonitor, Store, SystemInfo

from app import schemas
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class _DirectFnosClient(FnosClient):
    """连接 fnOS 时绕过 websockets 的自动代理发现。"""

    async def connect(
        self,
        endpoint: str,
        timeout: float = 3.0,
        use_ssl: bool = False,
        skip_ssl_verify: bool = True,
    ) -> bool:
        parsed_endpoint, actual_use_ssl = self._parse_endpoint(endpoint, use_ssl)
        self.endpoint = parsed_endpoint
        self.use_ssl = actual_use_ssl
        self.skip_ssl_verify = skip_ssl_verify

        protocol = "wss" if actual_use_ssl else "ws"
        uri = f"{protocol}://{parsed_endpoint}/websocket?type={self.type}"
        ssl_context = None
        if actual_use_ssl:
            ssl_context = ssl.create_default_context()
            if skip_ssl_verify:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        try:
            self.ws = await websockets.connect(uri, ssl=ssl_context, proxy=None)
            self.message_task = asyncio.create_task(self._message_handler())
            self.connect_future = asyncio.get_running_loop().create_future()
            await self._send_first_request()
            await asyncio.wait_for(self.connect_future, timeout=timeout)
            return True
        except asyncio.TimeoutError as err:
            self.connected = False
            raise RuntimeError("连接超时") from err
        except Exception:
            self.connected = False
            raise


class FNOSHomePage(_PluginBase):
    """飞牛 fnOS HomePage 自定义 API 插件。"""

    plugin_name = "飞牛 fnOS HomePage"
    plugin_desc = "将飞牛 fnOS 系统、网络和存储状态转换为 Homepage customapi 接口。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/homepage.png"
    plugin_version = "1.0.3"
    plugin_author = "Donniex"
    author_url = "https://github.com/donniex1986/MoviePilot-Plugins"
    plugin_config_prefix = "fnoshomepage_"
    plugin_order = 33
    plugin_label = "工具,NAS,监控,Homepage"
    auth_level = 1
    plugin_doc_url = "https://github.com/donniex1986/MoviePilot-Plugins/tree/main/plugins.v2/fnoshomepage"

    _enabled = False
    _endpoint = ""
    _username = ""
    _password = ""
    _use_ssl = False
    _verify_ssl = False
    _request_timeout = 10
    _cache_ttl = 30
    _last_error = ""
    _snapshot: Optional[Dict[str, Any]] = None
    _snapshot_updated_at = 0.0

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._endpoint = ""
        self._username = ""
        self._password = ""
        self._use_ssl = False
        self._verify_ssl = False
        self._request_timeout = 10
        self._cache_ttl = 30
        self._last_error = ""
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_updated_at = 0.0

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._endpoint = str(config.get("endpoint") or "").strip().rstrip("/")
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._use_ssl = bool(config.get("use_ssl", False))
        self._verify_ssl = bool(config.get("verify_ssl", False))
        self._request_timeout = self._positive_int(config.get("request_timeout"), 10)
        self._cache_ttl = self._positive_int(config.get("cache_ttl"), 30)

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
                "summary": "飞牛 fnOS 总览",
                "description": "返回 Homepage customapi 可用的飞牛系统、网络和存储总览。",
            },
            {
                "path": "/system",
                "endpoint": self.system,
                "methods": ["GET"],
                "summary": "飞牛 fnOS 系统状态",
                "description": "返回飞牛主机名、运行时间、CPU 和内存状态。",
            },
            {
                "path": "/storage",
                "endpoint": self.storage,
                "methods": ["GET"],
                "summary": "飞牛 fnOS 存储状态",
                "description": "返回飞牛存储卷的容量、使用量和 RAID 状态。",
            },
        ]

    def summary(self, apikey: str) -> Any:
        """返回飞牛系统总览。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            return self._load_snapshot()
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"FNOSHomePage 获取总览失败: {err}")
            return self._error_payload(str(err))

    def system(self, apikey: str) -> Any:
        """返回飞牛系统状态。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            snapshot = self._load_snapshot()
            keys = (
                "status", "hostname", "uptime_seconds", "cpu_percent",
                "memory_used_bytes", "memory_total_bytes", "memory_percent",
                "updated_at", "stale", "error",
            )
            return {key: snapshot[key] for key in keys if key in snapshot}
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"FNOSHomePage 获取系统状态失败: {err}")
            return self._error_payload(str(err))

    def storage(self, apikey: str) -> Any:
        """返回飞牛存储状态。"""
        if not self._check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        try:
            snapshot = self._load_snapshot()
            keys = (
                "status", "storage_used_bytes", "storage_total_bytes",
                "storage_percent", "volume_count", "volumes", "updated_at", "stale", "error",
            )
            return {key: snapshot[key] for key in keys if key in snapshot}
        except Exception as err:
            self._last_error = str(err)
            logger.warning(f"FNOSHomePage 获取存储状态失败: {err}")
            return self._error_payload(str(err))

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "use_ssl", "label": "使用 WSS 加密连接"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "endpoint",
                                            "label": "fnOS 地址",
                                            "placeholder": "192.168.1.10:5666",
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
                                        "props": {"model": "username", "label": "管理员用户名"},
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "管理员密码",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "request_timeout",
                                            "label": "请求超时秒数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "cache_ttl", "label": "缓存秒数", "type": "number"},
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
                            "text": "fnOS WebSocket 默认端口为 5666。请使用具备系统与存储读取权限的管理员账号；当前版本暂不支持需要二次验证的登录。",
                        },
                    },
                ],
            }
        ]
        return form, {
            "enabled": False,
            "endpoint": "",
            "username": "",
            "password": "",
            "use_ssl": False,
            "verify_ssl": False,
            "request_timeout": 10,
            "cache_ttl": 30,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页面。"""
        api_base = "/api/v1/plugin/FNOSHomePage"
        examples = self._homepage_examples(api_base)
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info" if not self._last_error else "warning",
                    "variant": "tonal",
                    "text": (
                        f"fnOS 地址: {self._endpoint or '-'}；最近错误: "
                        f"{self._last_error or '无'}"
                    ),
                },
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

    def _load_snapshot(self) -> Dict[str, Any]:
        """读取缓存或连接 fnOS 获取最新状态。"""
        now = time.time()
        if self._snapshot and now - self._snapshot_updated_at < self._cache_ttl:
            return copy.deepcopy(self._snapshot)

        try:
            if not self._enabled:
                raise RuntimeError("插件未启用")
            if not self._endpoint or not self._username or not self._password:
                raise ValueError("请配置 fnOS 地址、管理员用户名和密码")
            snapshot = self._run_async(self._fetch_snapshot())
            snapshot["updated_at"] = self._now_text()
            snapshot["stale"] = False
            self._snapshot = snapshot
            self._snapshot_updated_at = time.time()
            self._last_error = ""
            return copy.deepcopy(snapshot)
        except Exception as err:
            self._last_error = str(err)
            if self._snapshot:
                snapshot = copy.deepcopy(self._snapshot)
                snapshot["stale"] = True
                snapshot["error"] = str(err)
                snapshot["message"] = f"fnOS 请求失败: {err}"
                return snapshot
            raise

    async def _fetch_snapshot(self) -> Dict[str, Any]:
        """登录 fnOS 并读取系统、网络与存储数据。"""
        client = _DirectFnosClient()
        try:
            await client.connect(
                self._endpoint,
                timeout=self._request_timeout,
                use_ssl=self._use_ssl,
                skip_ssl_verify=not self._verify_ssl,
            )
            login = await client.login(self._username, self._password, timeout=self._request_timeout)
            if login.get("twofaRequired") or login.get("twofaSetupRequired"):
                raise RuntimeError("fnOS 账号要求二次验证；当前插件版本暂不支持该登录方式")
            if login.get("result") != "succ":
                reason = (
                    login.get("message")
                    or login.get("msg")
                    or login.get("errmsg")
                    or "fnOS 未提供登录失败原因"
                )
                raise RuntimeError(f"fnOS 登录失败: {reason}")

            info = SystemInfo(client)
            monitor = ResourceMonitor(client)
            store = Store(client)
            timeout = float(self._request_timeout)
            hostname, uptime, cpu, memory, network, storage = await asyncio.gather(
                info.get_host_name(timeout=timeout),
                info.get_uptime(timeout=timeout),
                monitor.cpu(timeout=timeout),
                monitor.memory(timeout=timeout),
                monitor.net(timeout=timeout),
                store.general(timeout=timeout),
            )
            return self._build_snapshot(hostname, uptime, cpu, memory, network, storage)
        finally:
            await client.close()

    @classmethod
    def _build_snapshot(
        cls,
        hostname_response: Dict[str, Any],
        uptime_response: Dict[str, Any],
        cpu_response: Dict[str, Any],
        memory_response: Dict[str, Any],
        network_response: Dict[str, Any],
        storage_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        """将 fnOS 响应转换为稳定的 Homepage 字段。"""
        hostname_data = cls._response_data(hostname_response, "主机名")
        uptime_data = cls._response_data(uptime_response, "运行时间")
        cpu_data = cls._response_data(cpu_response, "CPU")
        memory_data = cls._response_data(memory_response, "内存")
        network_data = cls._response_data(network_response, "网络")
        storage_data = cls._response_data(storage_response, "存储")

        cpu = cpu_data.get("cpu", {})
        cpu_busy = cpu.get("busy", {}) if isinstance(cpu, dict) else {}
        cpu_percent = cls._required_number(cpu_busy.get("all"), "CPU 使用率")

        memory = memory_data.get("mem", memory_data.get("memory", {}))
        if isinstance(memory, dict) and isinstance(memory.get("mem"), dict):
            memory = memory["mem"]
        if not isinstance(memory, dict):
            memory = {}
        memory_total = cls._required_number(memory.get("total"), "内存总量")
        memory_used_value = memory.get("used")
        if memory_used_value is None and memory.get("free") is not None:
            memory_used_value = memory_total - cls._required_number(memory.get("free"), "可用内存")
        memory_used = cls._required_number(memory_used_value, "已用内存")
        if memory_used > memory_total:
            raise ValueError("fnOS 已用内存超过内存总量")

        uptime_info = uptime_data.get("uptime", {})
        uptime_seconds = cls._required_number(
            uptime_info.get("uptime") if isinstance(uptime_info, dict) else uptime_info,
            "运行时间",
        )
        hostname = cls._first_value(hostname_data, ("hostName", "hostname", "name"))

        interfaces = network_data.get("net", {}).get("ifs", [])
        if not isinstance(interfaces, list):
            raise ValueError("fnOS 网络响应中的 ifs 不是列表")
        normalized_interfaces = []
        for interface in interfaces:
            if not isinstance(interface, dict):
                raise ValueError("fnOS 网络接口数据格式无效")
            normalized_interfaces.append(
                {
                    "name": interface.get("name", ""),
                    "upload_bytes_per_sec": cls._required_number(
                        interface.get("transmit"), "网络上传速率"
                    ),
                    "download_bytes_per_sec": cls._required_number(
                        interface.get("receive"), "网络下载速率"
                    ),
                }
            )

        volumes = storage_data.get("array", [])
        if not isinstance(volumes, list):
            raise ValueError("fnOS 存储响应中的 array 不是列表")
        normalized_volumes = []
        for volume in volumes:
            if not isinstance(volume, dict):
                raise ValueError("fnOS 存储卷数据格式无效")
            total = cls._required_number(volume.get("fssize"), "存储卷总量")
            free = cls._required_number(volume.get("frsize"), "存储卷可用量")
            if free > total:
                raise ValueError("fnOS 存储卷可用量超过总量")
            used = total - free
            raid = volume.get("md") or []
            raid_status = raid[0].get("arrayState") if raid and isinstance(raid[0], dict) else None
            normalized_volumes.append(
                {
                    "name": volume.get("name", ""),
                    "mountpoint": volume.get("mountpoint", ""),
                    "raid_level": volume.get("level", ""),
                    "raid_status": raid_status or "",
                    "used_bytes": used,
                    "total_bytes": total,
                    "used_percent": round(used / total * 100, 2) if total > 0 else 0,
                }
            )

        memory_percent = round(memory_used / memory_total * 100, 2) if memory_total > 0 else 0
        storage_total = sum(volume["total_bytes"] for volume in normalized_volumes)
        storage_used = sum(volume["used_bytes"] for volume in normalized_volumes)
        network_upload = sum(item["upload_bytes_per_sec"] for item in normalized_interfaces)
        network_download = sum(item["download_bytes_per_sec"] for item in normalized_interfaces)

        return {
            "status": "online",
            "message": "fnOS 连接正常",
            "hostname": str(hostname or ""),
            "uptime_seconds": uptime_seconds,
            "cpu_percent": cpu_percent,
            "memory_used_bytes": memory_used,
            "memory_total_bytes": memory_total,
            "memory_percent": memory_percent,
            "upload_bytes_per_sec": network_upload,
            "download_bytes_per_sec": network_download,
            "network_interfaces": normalized_interfaces,
            "storage_used_bytes": storage_used,
            "storage_total_bytes": storage_total,
            "storage_percent": round(storage_used / storage_total * 100, 2) if storage_total > 0 else 0,
            "volume_count": len(normalized_volumes),
            "volumes": normalized_volumes,
        }

    @staticmethod
    def _response_data(response: Dict[str, Any], label: str) -> Dict[str, Any]:
        """验证 fnOS 响应并取出数据字段。"""
        if not isinstance(response, dict):
            raise ValueError(f"fnOS {label}响应格式无效")
        if response.get("result") != "succ":
            message = response.get("message") or response.get("msg") or "请求失败"
            raise RuntimeError(f"fnOS 获取{label}失败: {message}")
        data = response.get("data")
        return data if isinstance(data, dict) else response

    @staticmethod
    def _first_value(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
        """从响应中查找第一个非空主机名字段。"""
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return ""

    @staticmethod
    def _required_number(value: Any, label: str) -> float:
        """将 fnOS 数值字段转换为有限数字，缺失时明确报错。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"fnOS 响应缺少有效的{label}")
        if not math.isfinite(number):
            raise ValueError(f"fnOS 响应中的{label}不是有限数字")
        if number < 0:
            raise ValueError(f"fnOS 响应中的{label}不能为负数")
        return number

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """解析正整数配置。"""
        try:
            number = int(value)
            return number if number > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _run_async(coroutine: Any) -> Any:
        """在同步插件 API 中执行协程，并兼容已有事件循环的调用环境。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        result: List[Any] = []
        errors: List[Exception] = []

        def run() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except Exception as err:
                errors.append(err)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join()
        if errors:
            raise errors[0]
        return result[0]

    @staticmethod
    def _now_text() -> str:
        """返回当前时间文本。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _error_payload(cls, message: str) -> Dict[str, Any]:
        """构建稳定错误响应。"""
        return {
            "status": "error",
            "message": f"fnOS 请求失败: {message}",
            "updated_at": cls._now_text(),
            "stale": False,
        }

    @staticmethod
    def _homepage_examples(api_base: str) -> str:
        """生成 Homepage YAML 示例。"""
        return f"""- fnOS:
    icon: mdi-nas
    href: <FNOS_LAN_URL>
    widgets:
      - type: customapi
        url: http://moviepilot:3001{api_base}/summary?apikey={{{{HOMEPAGE_VAR_MP_APIKEY}}}}
        refreshInterval: 10000
        mappings:
          - field: message
            label: 连接状态
          - field: hostname
            label: NAS
          - field: cpu_percent
            label: CPU %
          - field: memory_percent
            label: 内存 %
          - field: uptime_seconds
            label: 运行时间(秒)
      - type: customapi
        url: http://moviepilot:3001{api_base}/summary?apikey={{{{HOMEPAGE_VAR_MP_APIKEY}}}}
        refreshInterval: 10000
        mappings:
          - field: message
            label: 连接状态
          - field: storage_percent
            label: 存储使用率 %
          - field: storage_used_bytes
            label: 已用字节
          - field: storage_total_bytes
            label: 总容量字节
          - field: download_bytes_per_sec
            label: 下载字节/秒
          - field: upload_bytes_per_sec
            label: 上传字节/秒"""
