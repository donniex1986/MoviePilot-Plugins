import re
import json
import requests
import os
import threading

from datetime import datetime
from typing import Any, List, Dict, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.log import logger

try:
    from app.schemas.types import NotificationType
except Exception:
    NotificationType = None


class HuamucangCheckIn(_PluginBase):
    # 插件名称
    plugin_name = "聚影签到"

    # 插件描述
    plugin_desc = "用于聚影网站每日自动签到，请在插件配置中填写用户名和密码或备用 Cookie。"

    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/donniex1986/MoviePilot-Plugins/main/plugins.v2/huamucangcheckin/icons/taosync-2.png"

    # 插件版本
    plugin_version = "1.0.15"

    # 插件作者
    plugin_author = "Donniex"

    # 作者主页
    author_url = "https://www.jying.top"

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
    _username = ""
    _password = ""
    _user_agent = ""
    _checkin_history: List[Dict[str, Any]] = []

    # 站点地址
    _base_url = "https://www.jying.top"
    _checkin_url = "https://www.jying.top/api/app/checkin/do/"

    # 不再使用插件内部 BackgroundScheduler，统一交给 MoviePilot get_service 调度
    _scheduler = None

    # 签到执行锁，防止定时、API、手动命令等入口同时触发
    _checkin_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        # 设置编码环境变量以避免乱码
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        os.environ.setdefault("LANG", "C.UTF-8")
        os.environ.setdefault("LC_ALL", "C.UTF-8")

        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 8 * * *")
            self._cookie = config.get("cookie", "")
            self._username = config.get("username", "")
            self._password = config.get("password", "")
            self._user_agent = config.get("user_agent", "")
            self._checkin_history = config.get("checkin_history", []) or []

        if not self._enabled:
            logger.info("聚影签到插件未启用")
            return

        logger.info(f"聚影签到插件已启用，Cron：{self._cron}")

        # 立即运行一次
        # 注意：这里不再使用 BackgroundScheduler，避免和 get_service 重复调度
        if self._onlyonce:
            logger.info("聚影签到插件配置为立即运行一次")

            try:
                threading.Thread(
                    target=self.checkin,
                    name="HuamucangCheckInOnlyOnce",
                    daemon=True
                ).start()
            except Exception as e:
                logger.error(f"聚影签到立即运行启动失败：{e}")

            self._onlyonce = False
            self.__update_config()

    def checkin(self):
        """
        执行签到入口，带锁防止重复触发
        """
        if not self._checkin_lock.acquire(blocking=False):
            logger.info("聚影签到正在执行中，跳过本次重复触发")
            return

        try:
            self.__do_checkin()
        finally:
            self._checkin_lock.release()

    def __do_checkin(self):
        """
        实际执行签到
        """
        if not self._cookie and (not self._username or not self._password):
            msg = "聚影签到失败：未配置 Cookie 或用户名/密码"
            logger.warning(msg)
            self.__notify("聚影签到失败", msg)
            return

        logger.info("开始执行聚影签到")

        if self.__already_signed_in_today():
            msg = "今日已签到，跳过重复签到"
            logger.info(msg)
            return

        headers = {
            "User-Agent": self._user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self._base_url + "/checkin",
            "Origin": self._base_url,
            "X-Requested-With": "XMLHttpRequest"
        }

        session = requests.Session()
        cookie_dict = self.__parse_cookie_string(self._cookie)
        if cookie_dict:
            session.cookies.update(cookie_dict)
            csrf_token = cookie_dict.get("csrftoken") or cookie_dict.get("csrfToken")
            if csrf_token:
                headers["X-CSRFToken"] = csrf_token
            logger.debug("使用Cookie登录模式")

        if not cookie_dict and self._username and self._password:
            logger.debug("使用用户名密码登录模式")
            login_success, login_token = self.__login(session, headers)
            if not login_success:
                msg = "聚影签到失败：用户名/密码登录失败，请检查配置"
                logger.warning(msg)
                self.__save_checkin_history(msg)
                self.__notify("聚影签到失败", msg)
                return
            if login_token:
                headers["X-App-User-Token"] = login_token
                logger.debug(f"已设置X-App-User-Token: {login_token[:20]}...")

        csrf_token = self.__get_csrf_token(session)
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
            logger.debug(f"已设置X-CSRFToken: {csrf_token[:20]}...")

        logger.debug(f"当前会话Cookie: {list(session.cookies.keys())}")
        logger.debug(
            f"请求头包含认证信息: "
            f"X-App-User-Token={bool(headers.get('X-App-User-Token'))}, "
            f"X-CSRFToken={bool(headers.get('X-CSRFToken'))}"
        )

        try:
            logger.debug(f"开始发送签到请求到: {self._checkin_url}")
            logger.debug(f"请求头: {headers}")
            response = session.post(
                self._checkin_url,
                headers=headers,
                timeout=20,
                allow_redirects=True
            )

            # 修复中文乱码
            self.__fix_response_encoding(response)

            status_code = response.status_code
            text = response.text or ""

            logger.info(f"聚影签到请求完成，状态码：{status_code}")
            logger.debug(f"响应头: {dict(response.headers)}")
            logger.debug(f"响应内容: {text[:500]}...")

            # 如果 401 未登录且配置了用户名密码，尝试登录重试
            if status_code == 401 and self._username and self._password:
                logger.info("检测到401未登录，尝试用户名密码登录重试")
                login_success, login_token = self.__login(session, headers.copy())
                if login_success:
                    if login_token:
                        headers["X-App-User-Token"] = login_token
                        logger.debug(f"重试时设置X-App-User-Token: {login_token[:20]}...")

                    csrf_token = self.__get_csrf_token(session)
                    if csrf_token:
                        headers["X-CSRFToken"] = csrf_token
                        logger.debug(f"重试时更新X-CSRFToken: {csrf_token[:20]}...")

                    logger.debug("重新发送签到请求")
                    try:
                        response = session.post(
                            self._checkin_url,
                            headers=headers,
                            timeout=20,
                            allow_redirects=True
                        )
                        self.__fix_response_encoding(response)
                        status_code = response.status_code
                        text = response.text or ""
                        logger.info(f"重试聚影签到请求完成，状态码：{status_code}")
                        logger.debug(f"重试响应头: {dict(response.headers)}")
                        logger.debug(f"重试响应内容: {text[:500]}...")
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"重试签到请求异常: {e}")
                else:
                    logger.warning("用户名密码登录失败，无法重试")

            result_msg = self.__parse_json_result(response)
            if not result_msg:
                result_msg = self.__parse_result(status_code, text, response.url)

            logger.info(f"解析结果: {result_msg}")
            self.__save_checkin_history(result_msg)
            self.__notify("聚影签到", result_msg)

        except requests.exceptions.RequestException as e:
            msg = f"聚影签到请求异常：{e}"
            logger.error(msg)
            self.__save_checkin_history(msg)
            self.__notify("聚影签到失败", msg)
        except Exception as e:
            msg = f"聚影签到发生未知异常：{e}"
            logger.error(msg)
            self.__save_checkin_history(msg)
            self.__notify("聚影签到失败", msg)

    @staticmethod
    def __fix_response_encoding(response):
        """
        修复 requests 对中文页面的错误编码判断
        """
        try:
            apparent_encoding = response.apparent_encoding

            if apparent_encoding:
                response.encoding = apparent_encoding

            if not response.encoding:
                response.encoding = "utf-8"

            if response.encoding.lower() in ["iso-8859-1", "latin-1", "ascii"]:
                response.encoding = "utf-8"

        except Exception:
            response.encoding = "utf-8"

    def __parse_json_result(self, response) -> str:
        """
        解析 JSON 签到结果
        """
        try:
            content_type = response.headers.get("Content-Type", "")
            text = response.text or ""
            raw_text = text.strip()

            logger.debug(f"解析签到结果，Content-Type: {content_type}")
            logger.debug(f"签到响应文本: {raw_text[:500]}...")

            if not raw_text:
                return ""

            is_json = False

            if "application/json" in content_type.lower():
                is_json = True

            if raw_text.startswith("{") or raw_text.startswith("["):
                is_json = True

            if not is_json:
                logger.debug("响应不是JSON格式")
                return ""

            try:
                data = response.json()
                logger.debug(f"解析后的JSON数据: {data}")
            except Exception as e:
                logger.warning(f"JSON解析失败: {e}")
                data = json.loads(raw_text)

            if isinstance(data, list):
                return f"聚影签到返回：{data}"

            if not isinstance(data, dict):
                return f"聚影签到返回：{data}"

            code = data.get("code")
            status = data.get("status")
            msg = (
                data.get("msg")
                or data.get("message")
                or data.get("error")
                or data.get("detail")
                or ""
            )
            data_value = data.get("data")

            logger.info(f"签到结果解析: code={code}, status={status}, msg={msg}")

            result_text = msg or str(data_value) or str(data)

            success_codes = [0, 1, 200, "0", "1", "200", True, "success", "ok"]

            if code in success_codes or status in success_codes:
                return f"聚影签到成功：{result_text}"

            if any(keyword in str(result_text) for keyword in [
                "成功",
                "已签到",
                "今日已签到",
                "今天已签到",
                "签到过",
                "already",
                "success"
            ]):
                return f"聚影签到完成：{result_text}"

            if any(keyword in str(result_text).lower() for keyword in [
                "login",
                "unauthorized",
                "forbidden",
                "csrf",
                "token"
            ]):
                return f"聚影签到失败：{result_text}"

            return f"聚影签到返回：{result_text}"

        except Exception as e:
            logger.error(f"解析签到结果异常: {e}")
            return ""

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

        final_url_lower = final_url.lower()

        if any(x in final_url_lower for x in [
            "login",
            "signin",
            "auth"
        ]):
            return f"聚影签到失败：可能未登录或 Cookie 已失效，当前地址：{final_url}"

        clean_text = self.__clean_text(text)
        lower_text = clean_text.lower()

        success_keywords = [
            "签到成功",
            "打卡成功",
            "领取成功",
            "获得",
            "积分",
            "经验",
            "金币",
            "今日已签到",
            "今天已签到",
            "已经签到",
            "已签到",
            "签到过",
            "success",
            "checkin success",
            "checked",
            "already checked",
            "already"
        ]

        fail_keywords = [
            "请先登录",
            "未登录",
            "登录后",
            "用户登录",
            "login",
            "unauthorized",
            "forbidden",
            "csrf",
            "token",
            "失败",
            "error"
        ]

        for keyword in success_keywords:
            if keyword.lower() in lower_text:
                return f"聚影签到完成：{clean_text[:300]}" if clean_text else "聚影签到完成"

        for keyword in fail_keywords:
            if keyword.lower() in lower_text:
                return f"聚影签到可能失败：{clean_text[:300]}" if clean_text else "聚影签到可能失败：页面提示需要登录或权限不足"

        if "聚影" in clean_text and "网盘资源收集者" in clean_text:
            return "聚影签到请求成功，但页面未返回明确签到结果。请到网站个人中心确认是否签到成功。"

        if status_code == 200:
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
        text = re.sub(r"<!--[\s\S]*?-->", "", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&nbsp;", " ")
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<")
        text = text.replace("&gt;", ">")
        text = text.replace("&quot;", "\"")
        text = text.replace("&#39;", "'")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def __parse_cookie_string(cookie_str: str) -> Dict[str, str]:
        if not cookie_str:
            return {}

        cookies = {}
        for part in cookie_str.split(";"):
            if not part:
                continue
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
        return cookies

    @staticmethod
    def __get_csrf_token(session: requests.Session) -> str:
        """
        安全获取CSRF令牌，避免多个同名cookie的异常
        """
        try:
            return session.cookies.get("csrftoken") or session.cookies.get("csrfToken")
        except Exception:
            try:
                for cookie in session.cookies:
                    if cookie.name.lower() in ["csrftoken", "csrftoken"]:
                        return cookie.value
            except Exception:
                pass
            return None

    def __already_signed_in_today(self) -> bool:
        """
        检查当天是否已执行成功签到，避免重复请求
        """
        if not isinstance(self._checkin_history, list) or not self._checkin_history:
            return False

        today = datetime.now().strftime("%Y-%m-%d")
        last_entry = self._checkin_history[-1]
        if last_entry.get("date") != today:
            return False

        result = (last_entry.get("result") or "").lower()
        if not result:
            return False

        success_keywords = [
            "签到成功",
            "已签到",
            "签到完成",
            "今日已签到",
            "今天已签到",
            "已经签到",
            "签到过",
            "请求成功",
            "页面未返回明确签到结果",
            "already",
            "success",
            "checked"
        ]
        return any(keyword.lower() in result for keyword in success_keywords)

    def __format_latest_result(self, text: str) -> str:
        """
        清理最新结果显示，去掉重复前缀信息
        """
        if not text:
            return text

        prefixes = [
            "聚影签到完成：",
            "聚影签到失败：",
            "聚影签到返回：",
            "聚影签到可能失败：",
            "聚影签到请求成功：",
            "聚影签到成功："
        ]
        for prefix in prefixes:
            if text.startswith(prefix):
                return text[len(prefix):].strip()

        return text

    def __login(self, session: requests.Session, headers: Dict[str, str]) -> Tuple[bool, str]:
        """
        使用用户名/密码登录聚影站点，获取有效登录令牌或 Cookie
        """
        logger.info("开始执行登录流程")
        headers = headers.copy()
        headers["Referer"] = self._base_url + "/"

        try:
            session.get(self._base_url, headers=headers, timeout=20)
            logger.debug("已访问主页获取初始Cookie")
        except requests.exceptions.RequestException:
            pass

        try:
            session.get(
                self._base_url.rstrip("/") + "/api/csrf/",
                headers=headers,
                timeout=20,
                allow_redirects=True
            )
            logger.debug("已访问CSRF接口")
        except requests.exceptions.RequestException:
            pass

        csrf_token = self.__get_csrf_token(session)
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
            logger.debug(f"登录前CSRF令牌: {csrf_token[:20]}...")

        login_paths = [
            "/api/app/login/",
            "/api/auth/login/",
            "/api/user/login/",
            "/auth/login/",
            "/accounts/login/",
            "/login/"
        ]

        login_data_variants = [
            {"username": self._username, "password": self._password},
            {"email": self._username, "password": self._password},
            {"account": self._username, "password": self._password}
        ]

        for login_path in login_paths:
            login_url = self._base_url.rstrip("/") + login_path
            logger.debug(f"尝试登录路径: {login_path}")
            for login_data in login_data_variants:
                for use_json in (False, True):
                    try:
                        request_headers = headers.copy()
                        if use_json:
                            request_headers["Content-Type"] = "application/json"
                            response = session.post(
                                login_url,
                                headers=request_headers,
                                json=login_data,
                                timeout=20,
                                allow_redirects=True
                            )
                            logger.debug(f"使用JSON格式尝试登录: {login_url}")
                        else:
                            response = session.post(
                                login_url,
                                headers=request_headers,
                                data=login_data,
                                timeout=20,
                                allow_redirects=True
                            )
                            logger.debug(f"使用Form格式尝试登录: {login_url}")

                        self.__fix_response_encoding(response)
                        logger.debug(f"登录响应状态码: {response.status_code}")
                        logger.debug(f"登录响应内容: {response.text[:200]}...")

                        token = self.__extract_login_token(response)
                        if self.__login_success(response, session):
                            if token:
                                logger.info(f"聚影登录成功，登录地址：{login_url}，已获取 X-App-User-Token")
                                return True, token
                            logger.info(f"聚影登录成功，登录地址：{login_url}")
                            return True, ""
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"登录请求异常: {e}")
                        continue

        logger.error("所有登录尝试均失败")
        return False, ""

    @staticmethod
    def __extract_login_token(response: requests.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                token = data.get("token")
                if not token and isinstance(data.get("data"), dict):
                    token = data["data"].get("token")
                if token:
                    return str(token)
        except Exception:
            pass

        token = response.headers.get("x-refreshed-token") or response.headers.get("X-App-User-Token")
        return token if isinstance(token, str) else ""

    @staticmethod
    def __login_success(response: requests.Response, session: requests.Session) -> bool:
        logger.debug(f"检查登录是否成功，状态码: {response.status_code}")
        if response.status_code in (200, 302):
            cookies = session.cookies.get_dict()
            logger.debug(f"当前会话Cookie: {list(cookies.keys())}")
            if cookies and any(name.lower() in [
                "sessionid", "auth_token", "token", "jwt", "sess", "userid"
            ] for name in cookies):
                logger.debug("通过Cookie检查登录成功")
                return True

            try:
                data = response.json()
                logger.debug(f"登录响应JSON: {data}")
                if isinstance(data, dict):
                    code = data.get("code")
                    status = data.get("status")
                    logger.debug(f"登录响应code: {code}, status: {status}")
                    if code in [0, 1, 200, "0", "1", "200", True, "success", "ok"]:
                        logger.debug("通过code检查登录成功")
                        return True
                    if status in [0, 1, 200, "0", "1", "200", "success", "ok"]:
                        logger.debug("通过status检查登录成功")
                        return True
            except Exception as e:
                logger.warning(f"解析登录响应JSON失败: {e}")

            text = (response.text or "").lower()
            logger.debug(f"登录响应文本: {text[:200]}...")
            if any(keyword in text for keyword in ["登录成功", "welcome", "success"]):
                logger.debug("通过文本关键字检查登录成功")
                return True
            if any(keyword in text for keyword in ["用户名或密码错误", "invalid", "error", "fail", "failed"]):
                logger.debug("通过文本关键字检查登录失败")
                return False

        logger.debug("登录检查未通过任何条件")
        return False

    def __save_checkin_history(self, result_msg: str):
        """
        保存每天的签到记录
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            now_time = datetime.now().strftime("%H:%M:%S")
            if not isinstance(self._checkin_history, list):
                self._checkin_history = []

            if self._checkin_history and self._checkin_history[-1].get("date") == today:
                self._checkin_history[-1]["result"] = result_msg
                self._checkin_history[-1]["time"] = now_time
            else:
                self._checkin_history.append({
                    "date": today,
                    "time": now_time,
                    "result": result_msg
                })

            self._checkin_history = self._checkin_history[-30:]
            self.__update_config()
        except Exception:
            pass

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
            "username": self._username,
            "password": self._password,
            "user_agent": self._user_agent,
            "checkin_history": self._checkin_history
        })

    def get_state(self) -> bool:
        """
        插件状态
        """
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """
        服务信息

        注意：
        这里交给 MoviePilot 统一注册定时任务。
        插件内部不再启动 BackgroundScheduler，避免重复执行。
        """
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception:
                trigger = self._cron

            return [{
                "id": "HuamucangCheckInService",
                "name": "聚影自动签到",
                "trigger": trigger,
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
                                    "cols": 12,
                                    "md": 6
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username",
                                            "label": "用户名",
                                            "placeholder": "请输入 www.jying.top 登录用户名"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 6
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "type": "password",
                                            "placeholder": "请输入 www.jying.top 登录密码"
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
                                            "label": "Cookie (备用)",
                                            "placeholder": "可选，已登录 Cookie 作为备用登录方式",
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
                            "text": "推荐填写用户名和密码进行登录；也可填写已登录 Cookie 作为备用。签到地址为 https://www.jying.top/checkin。"
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
            "username": "",
            "password": "",
            "user_agent": ""
        }

    def get_page(self) -> List[dict]:
        """
        插件详情页
        """
        records = []
        if isinstance(self._checkin_history, list) and self._checkin_history:
            for item in reversed(self._checkin_history[-7:]):
                records.append({
                    "date": item.get("date", ""),
                    "time": item.get("time", ""),
                    "result": item.get("result", "")
                })

        summary_text = "用于聚影网站每日自动签到，请在插件配置中填写用户名和密码或备用 Cookie。"
        if not records:
            summary_text = "暂无签到记录。启用插件并执行签到后，历史数据将在这里显示。"

        latest_text = self.__format_latest_result(records[0]["result"]) if records else "暂时没有结果数据。"

        page = [
            {
                "component": "VCard",
                "props": {
                    "variant": "outlined"
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "聚影签到数据面板"
                    },
                    {
                        "component": "VCardText",
                        "text": summary_text
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "历史记录"
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": f"最近 {len(records)} 条签到记录"
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "最新结果"
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": latest_text
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

        if records:
            page.append({
                "component": "VCard",
                "props": {
                    "variant": "outlined"
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "签到历史"
                    },
                    *[
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "tonal",
                                "class": "mb-3"
                            },
                            "content": [
                                {
                                    "component": "VRow",
                                    "content": [
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 4},
                                            "content": [
                                                {
                                                    "component": "VCardTitle",
                                                    "text": record["date"]
                                                },
                                                {
                                                    "component": "VCardText",
                                                    "text": record["time"]
                                                }
                                            ]
                                        },
                                        {
                                            "component": "VCol",
                                            "props": {"cols": 8},
                                            "content": [
                                                {
                                                    "component": "VCardText",
                                                    "text": self.__format_latest_result(record["result"])
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                        for record in records
                    ]
                ]
            })

        return page

    def stop_service(self):
        """
        停止插件服务

        当前插件不再使用内部 BackgroundScheduler，
        定时任务统一交给 MoviePilot 的 get_service 管理。
        """
        self._scheduler = None
