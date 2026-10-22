"""Stream verification: does each stream play, with audio and video."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable

from zone_new_companion.models import Credentials, MediaItem
from zone_new_companion.services.base import PortalService
from zone_new_companion.services.libmpv import USER_AGENT
from zone_new_companion.services.logger_service import logger_service
from zone_new_companion.services.network import create_session
from zone_new_companion.services.stream_probe import ProbeResult, probe_stream

# Each probe decodes a few seconds of stream; more at once costs CPU and
# trips the connection limit most IPTV accounts have.
MAX_PARALLEL_PROBES = 4
RETRY_PAUSE_SECONDS = 2


@dataclass(slots=True)
class VerificationResult:
    """Verification result for one media item."""

    item_id: str
    status: str


class StreamVerifier:
    """Verify streams by playing a few seconds of each (see stream_probe)."""

    def __init__(self) -> None:
        self._session = create_session()
        # The player's user agent, so the fallback probe is served what playback gets.
        self._session.headers["User-Agent"] = USER_AGENT

    def verify_items(
        self,
        service: PortalService,
        credentials: Credentials,
        items: Iterable[MediaItem],
        workers: int = MAX_PARALLEL_PROBES,
        progress_callback=None,
    ) -> dict[str, str]:
        """Verify items concurrently and return status map keyed by item id."""
        target_items = [item for item in items if item.item_type in {"channel", "vod", "episode"}]
        if not target_items:
            return {}

        total = len(target_items)
        workers = max(1, min(workers, MAX_PARALLEL_PROBES))
        logger_service.info(f"Starting verification of {total} streams with {workers} workers")
        status_by_id: dict[str, str] = {}
        refused: list[MediaItem] = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._probe_item, service, credentials, item): item
                for item in target_items
            }
            for future in as_completed(futures):
                item = futures[future]
                result = future.result()
                status_by_id[item.id] = result.status
                if result.quick_refusal and workers > 1:
                    refused.append(item)
                self._report(item, result.status, len(status_by_id), total, progress_callback)

        # Accounts limited to one connection refuse the parallel probes; give
        # those streams a second chance on their own. Only when something
        # played, though: then the account works and refusals mean the limit.
        if not any(status.startswith("OK") for status in status_by_id.values()):
            refused = []
        if refused:
            logger_service.info(f"Retrying {len(refused)} refused streams one at a time")
        for item in refused:
            # Servers free the previous connection's slot only after a moment.
            time.sleep(RETRY_PAUSE_SECONDS)
            status = self._probe_item(service, credentials, item).status
            status_by_id[item.id] = status
            self._report(item, status, total, total, progress_callback)

        logger_service.info(f"Verification complete: {total} streams processed")
        return status_by_id

    def verify_item(
        self,
        service: PortalService,
        credentials: Credentials,
        item: MediaItem,
    ) -> VerificationResult:
        return VerificationResult(item_id=item.id, status=self._probe_item(service, credentials, item).status)

    def _probe_item(self, service: PortalService, credentials: Credentials, item: MediaItem) -> ProbeResult:
        try:
            stream_url = item.stream_url or service.resolve_stream_url(credentials, item)
            return probe_stream(stream_url, self._session)
        except Exception as e:  # noqa: BLE001 - one bad item must not stop the batch
            logger_service.debug(f"Verification failed for {item.name}: {e}")
            return ProbeResult(failure="Verify Error")

    @staticmethod
    def _report(item: MediaItem, status: str, done: int, total: int, progress_callback) -> None:
        logger_service.info(f"Stream {done}/{total} ({done / total * 100:.1f}%) - {item.name}: {status}")
        if progress_callback:
            progress_callback(item.id, status, done, total)
