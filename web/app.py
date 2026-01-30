import os
import sys
import time
import ujson
import aiohttp
import asyncio

from pathlib import Path
from typing import List
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.constants import STEAM_API_BASE
from src.utils.i18n import t

project_root = Path(__file__)
sys.path.insert(0, str(project_root))


def get_base_path():
    """获取运行时的基础路径"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    elif getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        return Path(os.path.dirname(os.path.abspath(__file__)))


def get_web_path(base_path: Path):
    """
    智能获取 web 资源目录
    兼容开发环境和打包环境(--add-data "web;web")
    """
    web_path = base_path / "web"
    if web_path.exists():
        return web_path

    return base_path


base_path = get_base_path()
web_path = get_web_path(base_path)

try:
    from src.main import OnekeyApp
    from src.config import ConfigManager
except ImportError as e:
    print(t("error.import", error=str(e)))
    print(t("error.ensure_root"))
    sys.exit(1)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except BaseException:
                pass


class WebOnekeyApp:
    def __init__(self, manager: ConnectionManager):
        self.onekey_app = None
        self.current_task = None
        self.task_status = "idle"
        self.task_progress = []
        self.task_result = None
        self.manager = manager

    def init_app(self):
        try:
            self.onekey_app = OnekeyApp()
            return True
        except Exception as e:
            return False, str(e)

    async def run_unlock_task(self, app_id: str, tool_type: str, dlc: bool):
        try:
            self.task_status = "running"
            self.task_progress = []

            self.onekey_app = OnekeyApp()

            self._add_progress_handler()

            result = await self.onekey_app.run(app_id, tool_type, dlc)

            if result:
                self.task_status = "completed"
                self.task_result = {
                    "success": True,
                    "message": "游戏解锁配置成功！重启Steam后生效",
                }
            else:
                self.task_status = "error"
                self.task_result = {"success": False, "message": "配置失败"}

        except Exception as e:
            self.task_status = "error"
            self.task_result = {"success": False, "message": f"配置失败: {str(e)}"}
        finally:
            if hasattr(self, "onekey_app") and self.onekey_app:
                try:
                    if hasattr(self.onekey_app, "client"):
                        await self.onekey_app.client.close()
                except BaseException:
                    pass
            self.onekey_app = None

    def _add_progress_handler(self):
        if self.onekey_app and self.onekey_app.logger:
            original_info = self.onekey_app.logger.info
            original_warning = self.onekey_app.logger.warning
            original_error = self.onekey_app.logger.error

            def info_with_progress(msg):
                self.task_progress.append(
                    {"type": "info", "message": str(msg), "timestamp": time.time()}
                )
                asyncio.create_task(
                    self.manager.broadcast(
                        ujson.dumps(
                            {
                                "type": "task_progress",
                                "data": {"type": "info", "message": str(msg)},
                            }
                        )
                    )
                )
                return original_info(msg)

            def warning_with_progress(msg):
                self.task_progress.append(
                    {"type": "warning", "message": str(msg), "timestamp": time.time()}
                )
                asyncio.create_task(
                    self.manager.broadcast(
                        ujson.dumps(
                            {
                                "type": "task_progress",
                                "data": {"type": "warning", "message": str(msg)},
                            }
                        )
                    )
                )
                return original_warning(msg)

            def error_with_progress(msg):
                self.task_progress.append(
                    {"type": "error", "message": str(msg), "timestamp": time.time()}
                )
                asyncio.create_task(
                    self.manager.broadcast(
                        ujson.dumps(
                            {
                                "type": "task_progress",
                                "data": {"type": "error", "message": str(msg)},
                            }
                        )
                    )
                )
                return original_error(msg)

            self.onekey_app.logger.info = info_with_progress
            self.onekey_app.logger.warning = warning_with_progress
            self.onekey_app.logger.error = error_with_progress


app = FastAPI(title="Onekey")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()

config = ConfigManager()

static_dir = web_path / config.app_config.language / "static"
template_dir = web_path / config.app_config.language / "templates"

if not static_dir.exists():
    print(f"Warning: Static dir not found at {static_dir}")

app.mount(
    "/static",
    StaticFiles(directory=str(static_dir)),
    name="static",
)
templates = Jinja2Templates(
    directory=str(template_dir)
)

web_app = WebOnekeyApp(manager)


@app.get("/")
async def index(request: Request):
    """主页"""
    config = ConfigManager()
    if not config.app_config.key:
        return RedirectResponse(request.url_for("oobe"))
    else:
        return templates.TemplateResponse("index.html", {"request": request})


@app.get("/oobe")
async def oobe(request: Request):
    """OOBE页面"""
    return templates.TemplateResponse("oobe.html", {"request": request})


@app.post("/api/init")
async def init_app():
    """初始化应用"""
    result = web_app.init_app()
    if isinstance(result, tuple):
        return JSONResponse({"success": False, "message": result[1]})
    return JSONResponse({"success": True})


@app.get("/api/config")
async def get_config():
    """获取配置信息"""
    try:
        config = ConfigManager()
        return JSONResponse(
            {
                "success": True,
                "config": {
                    "steam_path": str(config.steam_path) if config.steam_path else "",
                    "debug_mode": config.app_config.debug_mode,
                },
            }
        )
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})


@app.post("/api/start_unlock")
async def start_unlock(request: Request):
    """开始解锁任务"""
    data = await request.json()
    app_id = data.get("app_id", "").strip()
    tool_type = data.get("tool_type", "steamtools")
    dlc = data.get("dlc")

    if not app_id:
        return JSONResponse({"success": False, "message": "请输入有效的App ID"})

    app_id_list = [id for id in app_id.split("-") if id.isdigit()]
    if not app_id_list:
        return JSONResponse({"success": False, "message": "App ID格式无效"})

    if web_app.task_status == "running":
        return JSONResponse({"success": False, "message": "已有任务正在运行"})

    try:
        await web_app.run_unlock_task(app_id_list[0], tool_type, dlc)
    except Exception as e:
        web_app.task_status = "error"
        web_app.task_result = {
            "success": False,
            "message": f"任务执行失败: {str(e)}",
        }

    return JSONResponse({"success": True, "message": "任务已开始"})


@app.get("/api/task_status")
async def get_task_status():
    """获取任务状态"""
    return JSONResponse(
        {
            "status": web_app.task_status,
            "progress": (web_app.task_progress[-10:] if web_app.task_progress else []),
            "result": web_app.task_result,
        }
    )


@app.get("/about")
async def about_page(request: Request):
    """关于页面"""
    return templates.TemplateResponse("about.html", {"request": request})


@app.get("/settings")
async def settings_page(request: Request):
    """设置页面"""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.post("/api/config/update")
async def update_config(request: Request):
    """更新配置"""
    try:
        data = await request.json()

        if not isinstance(data, dict):
            return {"success": False, "message": "无效的配置数据"}

        config_manager = ConfigManager()

        new_config = {
            "KEY": data.get("key", ""),
            "Port": config_manager.app_config.port,
            "Custom_Steam_Path": data.get("steam_path", ""),
            "Debug_Mode": data.get("debug_mode", False),
            "Logging_Files": data.get("logging_files", True),
            "Show_Console": data.get("show_console", True),
            "Language": data.get("language", "zh"),
        }

        import ujson

        config_path = config_manager.config_path
        with open(config_path, "w", encoding="utf-8") as f:
            ujson.dump(new_config, f, indent=2, ensure_ascii=False)

        return {"success": True, "message": "配置已保存"}

    except Exception as e:
        return {"success": False, "message": f"保存配置失败: {str(e)}"}


@app.post("/api/config/reset")
async def reset_config():
    """重置配置为默认值"""
    try:
        from src.config import DEFAULT_CONFIG
        import ujson

        config_manager = ConfigManager()
        config_path = config_manager.config_path

        with open(config_path, "w", encoding="utf-8") as f:
            ujson.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)

        return {"success": True, "message": "配置已重置为默认值"}

    except Exception as e:
        return {"success": False, "message": f"重置配置失败: {str(e)}"}


@app.get("/api/config/detailed")
async def get_detailed_config():
    """获取详细配置信息"""
    try:
        config = ConfigManager()
        return {
            "success": True,
            "config": {
                "steam_path": str(config.steam_path) if config.steam_path else "",
                "debug_mode": config.app_config.debug_mode,
                "logging_files": config.app_config.logging_files,
                "show_console": config.app_config.show_console,
                "steam_path_exists": (
                    config.steam_path.exists() if config.steam_path else False
                ),
                "key": getattr(config.app_config, "key", ""),
                "language": config.app_config.language,
            },
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/getKeyInfo")
async def get_key_info(request: Request):
    """获取卡密信息"""
    try:
        data = await request.json()
        key = data.get("key", "").strip()

        if not key:
            return JSONResponse({"success": False, "message": "卡密不能为空"})

        async with aiohttp.ClientSession(conn_timeout=10.0) as client:
            response = await client.post(
                url=f"{STEAM_API_BASE}/getKeyInfo",
                json={"key": key},
                headers={"Content-Type": "application/json"},
            )

            if response.status == 200:
                result = ujson.loads(await response.content.read())
                return JSONResponse(result)
            else:
                return JSONResponse({"success": False, "message": "卡密验证服务不可用"})
    except aiohttp.ConnectionTimeoutError:
        return JSONResponse({"success": False, "message": "验证超时，请检查网络连接"})
    except Exception as e:
        return JSONResponse({"success": False, "message": f"验证失败: {str(e)}"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 端点"""
    await manager.connect(websocket)
    try:
        await websocket.send_text(
            ujson.dumps({"type": "connected", "data": {"message": "已连接到服务器"}})
        )

        while True:
            data = await websocket.receive_text()
            message = ujson.loads(data)
            if message.get("type") == "ping":
                await websocket.send_text(
                    ujson.dumps({"type": "pong", "data": {"timestamp": time.time()}})
                )
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print(t("web.client_disconnected"))
    except Exception as e:
        print(t("web.websocket_error", error=str(e)))
        manager.disconnect(websocket)


print(t("web.starting"))