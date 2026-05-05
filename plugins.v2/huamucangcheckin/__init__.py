import re
import requests

from datetime import datetime
from typing import Any, List, Dict, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.log import logger
from app.core.config import settings

try:
    from app.schemas.types import NotificationType
except Exception:
    NotificationType = None


class HuamucangCheckIn(_PluginBase):
    # 插件名称
    plugin_name = "聚影签到"

    # 插件描述
    plugin_desc = "用于 share.huamucang.top 聚影网站每日签到"

    # 插件图标
    plugin_icon = "https://share.huamucang.top/favicon.ico"

    # 插件版本
    plugin_version = "1.0.0"

    # 插件作者
    plugin_author = "ChatGPT"

    # 作者主页
    author_url = "https://share.huamucang.top"

    # 插件配置前缀
    plugin_config_prefix = "huamucangcheckin_"

    # 加载顺序
    plugin_order = 30

    # 可使用的用户级别
    auth_level = 1

    # 默认配置
    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _cookie = ""
    _user_agent = ""

    # 站点地址
    _base_url = "https://share.huamucang.top"
    _checkin_url = "https://share.huamucang.top/checkin"

    # 调度器
    _scheduler = None

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 8 * * *")
            self._cookie = config.get("cookie", "")
            self._user_agent = config.get("user_agent", "")

        if not self._enabled:
            logger.info("聚影签到插件未启用")
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            logger.info("聚影签到插件配置为立即运行一次")
            self._scheduler.add_job(
                func=self.checkin,
                trigger="date",
                run_date=datetime.now(),
                name="聚影签到立即运行"
            )

            self._onlyonce = False
            self.__update_config()

        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self.checkin,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="聚影自动签到"
                )
                logger.info(f"聚影签到服务已启动，Cron：{self._cron}")
            except Exception as e:
                logger.error(f"聚影签到 Cron 表达式错误：{self._cron}，错误：{e}")

        if self._scheduler.get_jobs():
            self._scheduler.start()

    def checkin(self):
        """
        执行签到
        """
        if not self._cookie:
            msg = "聚影签到失败：未配置 Cookie"
            logger.warning(msg)
            self.__notify("聚影签到", msg)
            return

        logger.info("开始执行聚影签到")

        headers = {
            "User-Agent": self._user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self._base_url + "/",
            "Cookie": self._cookie
        }

        session = requests.Session()

        try:
            response = session.get(
                self._checkin_url,
                headers=headers,
                timeout=20,
                allow_redirects=True
            )

            status_code = response.status_code
            text = response.text or ""

            logger.info(f"聚影签到请求完成，状态码：{status_code}")

            result_msg = self.__parse_result(status_code, text, response.url)

            logger.info(result_msg)
            self.__notify("聚影签到", result_msg)

        except requests.exceptions.RequestException as e:
            msg = f"聚影签到请求异常：{e}"
            logger.error(msg)
            self.__notify("聚影签到失败", msg)
        except Exception as e:
            msg = f"聚影签到发生未知异常：{e}"
            logger.error(msg)
            self.__notify("聚影签到失败", msg)

    def __parse_result(self, status_code: int, text: str, final_url: str) -> str:
        """
        解析签到结果
        """
        if status_code in [401, 403]:
            return "聚影签到失败：Cookie 可能已失效或无权限访问"

        if status_code >= 500:
            return f"聚影签到失败：服务器异常，状态码 {status_code}"

        if status_code >= 400:
            return f"聚影签到失败：请求异常，状态码 {status_code}"

        # 如果被重定向到登录页，通常说明 Cookie 失效
        if "login" in final_url.lower() or "signin" in final_url.lower():
            return f"聚影签到失败：可能未登录或 Cookie 已失效，当前地址：{final_url}"

        lower_text = text.lower()

        # 常见成功关键字
        success_keywords = [
            "签到成功",
            "打卡成功",
            "checkin success",
            "success",
            "已签到",
            "今日已签到",
            "already checked",
            "already check",
        ]

        # 常见失败或登录失效关键字
        fail_keywords = [
            "请先登录",
            "未登录",
            "登录",
            "login",
            "unauthorized",
            "forbidden",
            "csrf",
            "token",
        ]

        for keyword in success_keywords:
            if keyword.lower() in lower_text:
                clean_text = self.__clean_text(text)
                if clean_text:
                    return f"聚影签到完成：{clean_text[:300]}"
                return "聚影签到完成"

        for keyword in fail_keywords:
            if keyword.lower() in lower_text:
                clean_text = self.__clean_text(text)
                if clean_text:
                    return f"聚影签到可能失败：{clean_text[:300]}"
                return "聚影签到可能失败：页面提示需要登录或权限不足"

        # 没有明确提示时，只要 200 就认为请求成功，但提醒用户确认
        if status_code == 200:
            clean_text = self.__clean_text(text)
            if clean_text:
                return f"聚影签到请求成功，但未识别明确结果：{clean_text[:300]}"
            return "聚影签到请求成功，但页面无明确返回内容"

        return f"聚影签到完成，状态码：{status_code}"

    @staticmethod
    def __clean_text(html: str) -> str:
        """
        简单清理 HTML，提取可读文本
        """
        if not html:
            return ""

        text = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def __notify(self, title: str, text: str):
        """
        发送通知
        """
        if not self._notify:
            return

        try:
            if NotificationType:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=title,
                    text=text
                )
            else:
                self.post_message(
                    title=title,
                    text=text
                )
        except Exception as e:
            logger.warning(f"聚影签到发送通知失败：{e}")

    def __update_config(self):
        """
        更新插件配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "cookie": self._cookie,
            "user_agent": self._user_agent
        })

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """
        服务信息
        """
        if self._enabled and self._cron:
            return [{
                "id": "HuamucangCheckIn",
                "name": "聚影自动签到",
                "trigger": self._cron,
                "func": self.checkin,
                "kwargs": {}
            }]
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        """
        远程命令
        """
        return [{
            "cmd": "/huamucang_checkin",
            "event": "huamucang_checkin",
            "desc": "聚影签到",
            "category": "签到"
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        API 接口
        """
        return [{
            "path": "/checkin",
            "endpoint": self.checkin,
            "methods": ["GET"],
            "summary": "执行聚影签到"
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置页面
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 4
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 4
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 4
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "签到周期 Cron",
                                            "placeholder": "0 8 * * *",
                                            "hint": "默认每天 08:00 执行"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cookie",
                                            "label": "Cookie",
                                            "placeholder": "请填写登录 share.huamucang.top 后浏览器中的 Cookie",
                                            "rows": 5
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "user_agent",
                                            "label": "User-Agent",
                                            "placeholder": "可选，不填则使用默认浏览器 UA"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "使用前请先在浏览器登录 https://share.huamucang.top，然后复制 Cookie 到插件配置中。签到地址为 https://share.huamucang.top/checkin。"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "cookie": "",
            "user_agent": ""
        }

    def get_page(self) -> List[dict]:
        """
        插件详情页
        """
        return [
            {
                "component": "VCard",
                "props": {
                    "variant": "outlined"
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "聚影签到"
                    },
                    {
                        "component": "VCardText",
                        "text": "用于 share.huamucang.top 聚影网站每日自动签到。请在插件配置中填写登录后的 Cookie。"
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        停止插件服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"聚影签到服务停止失败：{e}")