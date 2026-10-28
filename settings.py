"""Runtime configuration and an offline check: python settings.py."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def number(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        raise ValueError(f"{name} 必须为 {low}..{high} 之间的整数") from None
    if not low <= value <= high:
        raise ValueError(f"{name} 必须为 {low}..{high} 之间的整数")
    return value


def flag(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} 必须为 true 或 false（也支持 1/0、yes/no、on/off）")


def https_url(name, default, *, origin_only=False):
    value = os.getenv(name, default).strip()
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and "?" not in value and "#" not in value
                 and not any(char.isspace() or ord(char) < 32 for char in value)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
        if origin_only:
            valid = (valid and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
                     and parsed.path in ("", "/") and not parsed.params)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{name} 必须为有效的 HTTPS 地址，不能含账号或查询参数" +
                         ("，不能含路径" if origin_only else ""))
    return value


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(Path(__file__).with_name("data")))))
    job_timeout: int = field(default_factory=lambda: number("TASK_TIMEOUT", 1200, 120, 1800))
    solve_timeout: int = field(default_factory=lambda: number("SOLVE_TIMEOUT", 600, 60, 1200))
    verify_timeout: int = field(default_factory=lambda: number("VERIFY_TIMEOUT", 300, 30, 600))
    verify: bool = field(default_factory=lambda: flag("VERIFY_SOLUTION", True))
    sympy: bool = field(default_factory=lambda: flag("SYMPY_ENABLED", True))
    search: bool = field(default_factory=lambda: flag("SEARCH_ENABLED", True))
    active_delivery: bool = field(default_factory=lambda: flag("ACTIVE_DELIVERY", False))
    max_queue: int = field(default_factory=lambda: number("MAX_QUEUE", 5, 1, 20))
    max_user_tasks: int = field(default_factory=lambda: number("MAX_USER_TASKS", 2, 1, 5))
    max_daily: int = field(default_factory=lambda: number("MAX_DAILY_TASKS", 50, 1, 500))
    result_ttl: int = field(default_factory=lambda: number("RESULT_TTL_DAYS", 7, 1, 30))
    disk_limit_mb: int = field(default_factory=lambda: number("RESULT_MAX_MB", 512, 64, 4096))
    search_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    search_endpoint: str = field(default_factory=lambda: https_url("DEEPSEEK_SEARCH_ENDPOINT", "https://api.deepseek.com/v1/tools/search"))


def validate_environment(*, require_credentials=True):
    """Validate locally without contacting providers or displaying secret values."""
    settings = Settings()
    if require_credentials:
        missing = [name for name in ("QQ_APPID", "QQ_SECRET", "DEEPSEEK_API_KEY")
                   if not os.getenv(name, "").strip()]
        if missing:
            raise ValueError("请先配置 .env：" + ", ".join(missing))
    for name in ("QQ_APPID", "QQ_SECRET", "DEEPSEEK_API_KEY"):
        value = os.getenv(name, "")
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError(f"{name} 不能含首尾空白或控制字符")
    base = https_url("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    https_url("QQ_API_BASE_URL", "https://api.bot.qq.com", origin_only=True)
    effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
    if effort not in ("low", "high", "max"):
        raise ValueError("DEEPSEEK_REASONING_EFFORT 必须为 low、high 或 max")
    for name in ("SOLVE_MAX_TOKENS", "VERIFY_MAX_TOKENS"):
        if name in os.environ:
            number(name, 131072 if effort == "max" else 65536, 8192, 131072)
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    vision = os.getenv("VISION_MODEL", "deepseek-flash").strip()
    if not model or not vision:
        raise ValueError("DEEPSEEK_MODEL 和 VISION_MODEL 不能为空")
    if urlparse(base).hostname == "api.deepseek.com":
        if model not in ("deepseek-flash", "deepseek-v4-pro"):
            raise ValueError("DEEPSEEK_MODEL 请使用官方现行名称 deepseek-flash 或 deepseek-v4-pro")
        if vision != "deepseek-flash":
            raise ValueError("VISION_MODEL 请使用支持图片的 deepseek-flash")
    if "DATA_DIR" in os.environ and not os.environ["DATA_DIR"].strip():
        raise ValueError("DATA_DIR 不能为空；使用默认目录时请删除这一行")
    warnings = []
    if settings.solve_timeout + (settings.verify_timeout if settings.verify else 0) + 120 > settings.job_timeout:
        warnings.append("TASK_TIMEOUT 较小：识题、搜索及排版还需要时间，阶段预算可能无法用完")
    if settings.max_user_tasks > settings.max_queue:
        warnings.append("MAX_USER_TASKS 大于 MAX_QUEUE，实际先受全局队列限制")
    if settings.active_delivery:
        warnings.append("ACTIVE_DELIVERY 已启用，需要 QQ 实际主动消息权限")
    if not os.getenv("ALLOWED_GROUPS", "").strip() and not os.getenv("ALLOWED_USERS", "").strip():
        warnings.append("群和私聊白名单均为空：接入范围由 QQ 平台权限决定")
    obsolete = ("PDF_INPUT_PAGES", "MAX_PAGES", "PDF_MAX_PAGES", "PDF_ENABLED", "PDF_MAX_MB", "PDF_DPI",
                "MAX_IMAGE_MB", "TEXT_FILE_MAX_MB", "FILE_MAX_MB", "SYMPY_TIMEOUT", "BOCHA_API_KEY", "BOCHA_ENDPOINT")
    ignored = [name for name in obsolete if name in os.environ]
    if ignored:
        warnings.append("升级版不读取以下旧配置，可删除：" + ", ".join(ignored))
    return warnings


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="离线检查机器人配置，不连接 QQ 或模型接口")
    parser.add_argument("--allow-missing-credentials", action="store_true", help="仅检查模板，允许三个凭据留空")
    args = parser.parse_args()
    try:
        notices = validate_environment(require_credentials=not args.allow_missing_credentials)
    except ValueError as exc:
        raise SystemExit("配置检查失败：" + str(exc))
    print("模板配置检查通过（未检查凭据）。" if args.allow_missing_credentials else
          "配置检查通过（离线；未验证密钥、余额或 QQ 权限）。")
    for notice in notices:
        print("提示：" + notice)
