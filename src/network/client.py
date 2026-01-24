"""HTTP客户端模块"""

import aiohttp
from typing import Optional, Dict


class HttpClient:
    """HTTP客户端封装"""

    def __init__(self):
        self._client = aiohttp.ClientSession(conn_timeout=60.0)

    async def get(
        self, url: str, headers: Optional[Dict] = None
    ) -> aiohttp.ClientResponse:
        """GET请求"""
        return await self._client.get(url, headers=headers)

    async def close(self):
        """关闭客户端"""
        await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
