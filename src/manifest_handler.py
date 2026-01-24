import asyncio
import io
import struct
import zipfile
from pathlib import Path
from typing import List, Optional, Callable, Awaitable

from .constants import STEAM_CACHE_CDN_LIST
from .logger import Logger
from .models import ManifestInfo, SteamAppManifestInfo
from .network.client import HttpClient
from .utils.i18n import t


class ManifestHandler:
    """清单处理器"""

    def __init__(self, client: HttpClient, logger: Logger, steam_path: Path):
        self.client = client
        self.logger = logger
        self.steam_path = steam_path
        self.depot_cache = steam_path / "depotcache"
        self.depot_cache.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(10)

    async def download_manifest(self, manifest_info: ManifestInfo) -> Optional[bytes]:
        """下载清单文件 (增加信号量限制)"""
        async with self.semaphore:
            for _ in range(3):
                for cdn in STEAM_CACHE_CDN_LIST:
                    url = cdn + manifest_info.url
                    try:
                        r = await self.client.get(url)
                        if r.status == 200:
                            return await r.content.read()
                    except Exception as e:
                        self.logger.debug(
                            t("manifest.download.failed", url=url, error=e)
                        )
            return None

    @staticmethod
    def _serialize_manifest_data(content: bytes) -> bytes:
        magic_signature = struct.pack("<I", 0x71F617D0)
        payload = content

        if len(content) >= 4 and content[:4] == magic_signature:
            payload = content[8:]
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    payload = zf.read("z")
            except (zipfile.BadZipFile, KeyError):
                pass

        return magic_signature + struct.pack("<I", len(payload)) + payload

    def process_manifest(
        self, manifest_data: bytes, manifest_info: ManifestInfo, remove_old: bool = True
    ) -> bool:
        try:
            depot_id = manifest_info.depot_id
            manifest_id = manifest_info.manifest_id

            _ = bytes.fromhex(manifest_info.depot_key)
            serialized_data = self._serialize_manifest_data(manifest_data)
            manifest_path = self.depot_cache / f"{depot_id}_{manifest_id}.manifest"

            if remove_old:
                for file in self.depot_cache.iterdir():
                    if file.suffix == ".manifest":
                        parts = file.stem.split("_")
                        if (
                            len(parts) == 2
                            and parts[0] == str(depot_id)
                            and parts[1] != str(manifest_id)
                        ):
                            file.unlink(missing_ok=True)
                            self.logger.info(t("manifest.delete_old", name=file.name))

            with open(manifest_path, "wb") as f:
                f.write(serialized_data)

            self.logger.debug(
                t(
                    "manifest.process.success",
                    depot_id=depot_id,
                    manifest_id=manifest_id,
                )
            )
            return True

        except Exception as e:
            self.logger.error(t("manifest.process.failed", error=e))
            return False

    async def _process_single_task(
        self,
        manifest_info: ManifestInfo,
        progress_callback: Optional[Callable[[str, int, int], Awaitable[None]]] = None,
        current_idx: int = 0,
        total: int = 0,
    ) -> Optional[ManifestInfo]:
        """处理单个清单的任务封装"""
        manifest_path = (
            self.depot_cache
            / f"{manifest_info.depot_id}_{manifest_info.manifest_id}.manifest"
        )

        if manifest_path.exists():
            if progress_callback:
                await progress_callback(
                    f"Exists: {manifest_info.depot_id}", current_idx, total
                )
            return manifest_info

        manifest_data = await self.download_manifest(manifest_info)

        if manifest_data and self.process_manifest(manifest_data, manifest_info):
            if progress_callback:
                await progress_callback(
                    f"Downloaded: {manifest_info.depot_id}", current_idx, total
                )
            return manifest_info
        else:
            self.logger.error(
                t(
                    "manifest.downloading.failed",
                    depot_id=manifest_info.depot_id,
                    manifest_id=manifest_info.manifest_id,
                )
            )
            if progress_callback:
                await progress_callback(
                    f"Failed: {manifest_info.depot_id}", current_idx, total
                )
            return None

    async def process_manifests(
        self,
        manifests: SteamAppManifestInfo,
        on_progress: Optional[Callable[[str, int, int], Awaitable[None]]] = None,
    ) -> List[ManifestInfo]:
        """批量并发处理清单"""
        all_manifests = manifests.mainapp + manifests.dlcs
        total = len(all_manifests)

        tasks = []
        for idx, manifest_info in enumerate(all_manifests, 1):
            tasks.append(
                self._process_single_task(manifest_info, on_progress, idx, total)
            )

        self.logger.info(t("manifest.start_batch", count=total))
        results = await asyncio.gather(*tasks)

        processed = [res for res in results if res is not None]
        return processed
