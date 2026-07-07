"""extension 分组 MCP 工具:get_extension_download —— 把插件包信息递给操作者。

远程 agent 调此工具,拿到下载地址 + 版本 + 安装步骤 + apikey 引导语,即可指导操作者
把"装好即用"的 chrome 插件装上并连回本服务。

关键:库里只存 apikey 的 hash,拿不到明文,故本工具不返回明文 apikey,只返回引导语——
让操作者填"连接本服务的同一把 apikey";忘了就由管理员 rotate_operator_apikey 重置。
"""

from fastmcp import FastMCP

from app import __version__
from app.core.config import settings

# apikey 引导语:不回传明文(库内只存 hash),引导操作者复用连接本服务的同一把 key。
_APIKEY_HINT = (
    "在插件里填入你连接本服务的同一把 apikey(创建/轮换 operator 时一次性显示的那串);"
    "忘了就让管理员用 rotate_operator_apikey 重置后重新下发。"
)

# 中文安装步骤:下载 → 解压 → 开发者模式加载 → 填配置 → 无痕模式需手动勾选启用。
_INSTALL_STEPS = [
    "下载插件包:点击 download_url 下载 extension.zip。",
    "解压 extension.zip 到一个固定目录(不要放临时目录,重装后目录还在才不用重加载)。",
    "打开 chrome://extensions,右上角开启「开发者模式」。",
    "点「加载已解压的扩展程序」,选中上一步解压出来的目录。",
    "打开插件弹窗,填入 serverUrl(本服务地址)与 apikey(见 apikey_hint)。",
    "若要在无痕模式使用,进插件详情页勾选「在无痕模式下启用」。",
]


def register_extension(mcp: FastMCP) -> None:
    """把 extension 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    def get_extension_download() -> dict:
        """返回 chrome 插件包下载地址、版本、安装步骤与 apikey 引导语。

        download_url 指向白名单放行的 /downloads/extension.zip(无需 apikey 即可下载);
        apikey_hint 是引导语而非明文 key(库内只存 hash,无法回取)。
        """
        return {
            "download_url": f"{settings.PUBLIC_BASE_URL}/downloads/extension.zip",
            "version": __version__,
            "apikey_hint": _APIKEY_HINT,
            "install_steps": _INSTALL_STEPS,
        }
