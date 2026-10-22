import asyncio
import random

import httpx

RETRY_DELAYS = (0.5, 1.0, 2.0)


def make_client(**kw):
    return httpx.AsyncClient(**kw)


async def _request_with_retry(client, method, url, **kw):
    last_exc = None
    for i, delay in enumerate(RETRY_DELAYS):
        try:
            resp = await client.request(method, url, **kw)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i < len(RETRY_DELAYS) - 1:
                    await asyncio.sleep(delay + random.random() * 0.25)
                    continue
            return resp
        except httpx.TransportError as e:
            last_exc = e
            if i < len(RETRY_DELAYS) - 1:
                await asyncio.sleep(delay + random.random() * 0.25)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return resp


async def get_with_retry(client, url, **kw):
    return await _request_with_retry(client, "GET", url, **kw)


async def post_with_retry(client, url, **kw):
    return await _request_with_retry(client, "POST", url, **kw)