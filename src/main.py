from typing import List, Dict, Tuple, Optional, Callable, Awaitable

import ujson

from .config import ConfigManager
from .constants import STEAM_API_BASE
from .logger import Logger
from .manifest_handler import ManifestHandler
from .models import DepotInfo, ManifestInfo, SteamAppInfo, SteamAppManifestInfo
from .network.client import HttpClient
from .utils.i18n import t


class OnekeyApp:
    """Onekey主应用"""

    def __init__(self):
        self.config = ConfigManager()
        self.logger = Logger(
            "Onekey",
            debug_mode=self.config.app_config.debug_mode,
            log_file=self.config.app_config.logging_files,
        )
        self.client = HttpClient()

    async def fetch_key(self) -> bool:
        """获取并验证卡密信息"""
        try:
            response = await self.client._client.post(
                f"{STEAM_API_BASE}/getKeyInfo",
                json={"key": self.config.app_config.key},
            )
            body = ujson.loads(await response.content.read())

            if not body["info"]:
                self.logger.error(t("api.key_not_exist"))
                return False

            key_type = body["info"]["type"]
            self.logger.info(t("api.key_type", type=t(f"key_type.{key_type}")))

            if key_type != "permanent":
                self.logger.info(t("api.key_expires", time=body["info"]["expiresAt"]))
            return True
        except Exception as e:
            self.logger.error(t("api.key_info_failed", error=str(e)))
            return True

    async def fetch_app_data(
        self, app_id: str, and_dlc: bool = True
    ) -> Tuple[SteamAppInfo, SteamAppManifestInfo]:
        """
        从API获取应用数据 (增加详细错误处理)
        """
        main_app_manifests = []
        dlc_manifests = []

        self.logger.info(t("api.fetching_game", app_id=app_id))

        try:
            response = await self.client._client.post(
                f"{STEAM_API_BASE}/getGame",
                json={"app_id": int(app_id), "dlc": and_dlc},
                headers={"X-Api-Key": self.config.app_config.key},
            )
            content_bytes = await response.content.read()

        except Exception as e:
            raise Exception(f"Network Error: {str(e)}")

        try:
            data = ujson.loads(content_bytes)
        except ValueError:
            self.logger.error(f"Invalid JSON response: {content_bytes[:100]}")
            raise Exception(f"API Error (HTTP {response.status}): Invalid Response")

        if response.status != 200:
            error_msg = data.get("msg") or data.get("detail") or "Unknown Error"
            self.logger.error(t("api.request_failed", code=response.status))
            raise Exception(f"API Error: {error_msg}")

        if data.get("code", 200) != 200:
            error_msg = data.get("msg", "Unknown Business Error")
            self.logger.error(f"API Business Error: {error_msg}")
            raise Exception(f"Server Error: {error_msg}")

        if not data or "data" not in data:
            app_data = data.get("data", data)
        else:
            app_data = data["data"]

        if not app_data:
            self.logger.error(t("api.no_manifest"))
            raise Exception("No game data found in response")

        self.logger.info(t("api.game_name", name=app_data.get("name", "Unknown")))

        for item in app_data.get("gameManifests", []):
            manifest = ManifestInfo(
                app_id=item["app_id"],
                depot_id=item["depot_id"],
                depot_key=item["depot_key"],
                manifest_id=item["manifest_id"],
                url=item["url"],
            )
            main_app_manifests.append(manifest)

        if and_dlc:
            for item in app_data.get("dlcManifests", []):
                for manifests in item.get("manifests", []):
                    manifest = ManifestInfo(
                        app_id=manifests["app_id"],
                        depot_id=manifests["depot_id"],
                        depot_key=manifests["depot_key"],
                        manifest_id=manifests["manifest_id"],
                        url=manifests["url"],
                    )
                    dlc_manifests.append(manifest)

        return SteamAppInfo(
            int(app_id),
            app_data.get("name", ""),
            app_data.get("totalDLCCount", app_data.get("dlcCount", 0)),
            app_data.get("depotCount", 0),
            app_data.get("workshopDecryptionKey", "None"),
        ), SteamAppManifestInfo(mainapp=main_app_manifests, dlcs=dlc_manifests)

    def prepare_depot_data(
        self, manifests: List[ManifestInfo]
    ) -> Tuple[List[DepotInfo], Dict[str, List[str]]]:
        """准备仓库数据"""
        depot_data = []
        depot_dict = {}

        for manifest in manifests:
            if manifest.depot_id not in depot_dict:
                depot_dict[manifest.depot_id] = {
                    "key": manifest.depot_key,
                    "manifests": [],
                }
            depot_dict[manifest.depot_id]["manifests"].append(manifest.manifest_id)

        for depot_id, info in depot_dict.items():
            depot_info = DepotInfo(
                depot_id=depot_id,
                decryption_key=info["key"],
                manifest_ids=info["manifests"],
            )
            depot_data.append(depot_info)

        return depot_data, depot_dict

    async def run(
        self,
        app_id: str,
        tool_type: str,
        dlc: bool,
        status_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> bool:
        """
        status_callback: 接收 {'step': str, 'msg': str, 'progress': int}
        """

        async def emit(step: str, msg: str, progress: int = -1):
            if status_callback:
                await status_callback({"step": step, "msg": msg, "progress": progress})

        try:
            if not self.config.steam_path:
                await emit("error", t("task.no_steam_path"))
                return False

            await emit("auth", "Validating Key...")
            await self.fetch_key()

            await emit("fetch", f"Fetching info for {app_id}...")

            app_info, manifests = await self.fetch_app_data(app_id, dlc)

            if not manifests.mainapp and not manifests.dlcs:
                raise Exception("Manifest list is empty")

            manifest_handler = ManifestHandler(
                self.client, self.logger, self.config.steam_path
            )

            async def download_progress(current_msg: str, current: int, total: int):
                percent = int((current / total) * 100) if total > 0 else 0
                await emit("download", current_msg, percent)

            await emit("download", "Starting download...", 0)

            processed_manifests = await manifest_handler.process_manifests(
                manifests, on_progress=download_progress
            )

            if not processed_manifests:
                raise Exception(t("task.no_manifest_processed"))

            await emit("config", "Configuring Tools...", 99)
            depot_data, _ = self.prepare_depot_data(processed_manifests)

            if tool_type == "steamtools":
                from .tools.steamtools import SteamTools

                tool = SteamTools(self.config.steam_path)
                success = await tool.setup(depot_data, app_info)
            elif tool_type == "greenluma":
                from .tools.greenluma import GreenLuma

                tool = GreenLuma(self.config.steam_path)
                success = await tool.setup(depot_data, app_id)
            else:
                raise Exception("Invalid tool type")

            if success:
                self.logger.info(t("tool.config_success"))
                await emit("finish", "Success! Please restart Steam.", 100)
                return True
            else:
                raise Exception("Tool configuration failed")

        except Exception as e:
            error_msg = str(e)
            self.logger.error(t("task.run_error", error=error_msg))
            await emit("error", error_msg)
            return False
        finally:
            await self.client.close()
