from functools import lru_cache
import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Department Platform API"
    app_env: str = "development"
    database_url: str = "sqlite:///./department_platform.db"
    cors_origins: str = "http://localhost:5173"

    # ---- 对外发布（2026-10-27 增加）----
    # 监听地址：默认只听本机，改成 0.0.0.0 才会被其他机器访问。
    # 故意不默认放开 —— 装到云服务器上忘记设密码就整台机器裸奔了。
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    # 服务在 HTTPS 后面跑时必须设 true，否则登录 cookie 会以明文过网络
    cookie_secure: bool = False
    # 生产环境关掉 /docs 与 /openapi.json：那会把所有接口和参数列给陌生人看
    expose_api_docs: bool = True
    # 数据分析引擎的内嵌端口（只在 127.0.0.1 上，由 /engine 反向代理访问）
    engine_port: int = 8765
    # 登录失败限流：同一 IP / 同一账号在窗口期内允许的失败次数
    login_max_failures: int = 8
    login_failure_window_seconds: int = 600

    # ---- 钉钉 ----
    dingtalk_corp_id: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    # 逗号分隔：todo(待办) / notify(工作通知) / ding(DING) / calendar(日程)
    dingtalk_message_kinds: str = "todo,calendar,trip"
    dingtalk_user_id: str = ""  # 留空则用前端免登 authCode 换取的 userid

    # ---- 公司 OA（泛微 e-cology，oa.maccura.com）----
    # 实测该站所有 /api/* 都返回同一鉴权错误，无法探测真实端点，
    # 因此登录路径/待办路径/字段映射全部显式配置，不写死默认值。
    oa_base_url: str = ""
    oa_auth_mode: str = "password"  # password | token
    oa_username: str = ""
    oa_password: str = ""
    oa_token: str = ""
    oa_auth_header: str = ""  # token 模式下自定义请求头名，如 X-Token
    oa_login_path: str = ""   # 如 login/CheckLogin.jsp
    oa_todo_path: str = ""    # 待办列表接口路径，如 api/workflow/paService.do
    oa_todo_query_params: str = ""  # 形如 "userId=xxx&pageSize=20"
    oa_rows_path: str = ""    # 列表在响应中的位置，如 "result.todos"
    oa_field_map: str = ""    # JSON：{"title":"subject","due":"finishTime","creator":"creator"}
    oa_default_title: str = "OA 待办"
    oa_timeout_seconds: float = 10.0
    oa_max_items: int = 30

    # ---- Salesforce（真实例：maccuracn.my.sfcrmproducts.cn）----
    salesforce_auth_mode: str = "totp"  # totp | oauth
    salesforce_instance_url: str = ""
    salesforce_api_version: str = "v62.0"
    salesforce_org_id: str = ""
    salesforce_username: str = ""
    salesforce_password: str = ""
    salesforce_totp_secret: str = ""  # Authenticator App 的 base32 手动输入密钥
    salesforce_access_token: str = ""
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_refresh_token: str = ""
    salesforce_token: str = ""  # 自研 CRM 场景下的固定令牌
    salesforce_timeout_seconds: float = 20.0
    salesforce_max_items: int = 30
    salesforce_window_days: int = 90

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def dingtalk_message_kinds_list(self) -> list[str]:
        return [k.strip() for k in self.dingtalk_message_kinds.split(",") if k.strip()]

    def oa_rows_path_list(self) -> list[str]:
        return [p.strip() for p in self.oa_rows_path.split(",") if p.strip()]

    @property
    def oa_query_params(self) -> dict[str, str]:
        """把 "a=1&b=2" 解析成 dict。"""
        result: dict[str, str] = {}
        for chunk in self.oa_todo_query_params.split("&"):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                if key.strip():
                    result[key.strip()] = value.strip()
        return result

    @property
    def oa_fields(self) -> dict[str, str]:
        """解析 OA_FIELD_MAP JSON；写坏时返回空 dict 而不是崩。"""
        if not self.oa_field_map.strip():
            return {}
        try:
            data = json.loads(self.oa_field_map)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
