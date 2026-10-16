"""Background driver for the annual-report workforce connector: downloads (threads) feed OCR (processes)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait

from .http import Http
from .workforce import ACCOUNT_COPY, _init_worker, backend_name, ocr_pdf_for_workforce, pdf_fingerprint


SECONDS_PER_FILING_PER_WORKER = 50.0  # measured: ~2-3 OCR pages per filing on one CPU thread


def default_workers() -> int:
    return max(1, min(12, (os.cpu_count() or 2) - 2))


def estimated_capacity(workers: int, seconds_available: float) -> int:
    """How many filings OCR can finish before the deadline (conservative)."""
    return max(0, int(workers * max(0.0, seconds_available) / SECONDS_PER_FILING_PER_WORKER))


class WorkforceRunner:
    def __init__(self, http: Http, jobs: list[tuple[str, str]], request_cap: int, workers: int | None = None, log=print,
                 cache_dir: Path | None = None):
        self.http = http
        self.cache_dir = cache_dir
        self.jobs = jobs  # (org, year) in priority order
        self.request_cap = request_cap
        self.workers = workers or default_workers()
        self.log = log
        self.results: dict[str, dict] = {}
        self.status = {"backend": None, "planned": len(jobs), "downloaded": 0, "accepted": 0, "skipped_reason": None}
        self._thread = threading.Thread(target=self._run, name="workforce", daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop_and_wait(self, timeout: float) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, timeout))

    def join_until(self, seconds: float) -> None:
        """Let OCR keep working until the deadline, then stop it and collect what finished."""
        self._thread.join(timeout=max(0.0, seconds))
        if self._thread.is_alive():
            self.stop_and_wait(15.0)

    # OCR results are cached by PDF content hash: an unchanged filing gives the identical claim on refresh.
    def _cache_get(self, sha: str | None) -> dict | None:
        if not self.cache_dir or not sha:
            return None
        path = self.cache_dir / f"{sha}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    def _cache_put(self, result: dict) -> None:
        if not self.cache_dir or not result.get("fingerprint") or result.get("status") not in ("accepted", "no_statement"):
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            keep = {k: v for k, v in result.items() if k not in ("url", "retrieved_at", "year", "cache_hit")}
            (self.cache_dir / f"{result['fingerprint']}.json").write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # Brreg throttles the filing-copy endpoint (HTTP 429 above ~30 requests/minute): pace request starts.
    MIN_INTERVAL_S = 2.1
    _pace_lock = threading.Lock()
    _last_start = 0.0

    def _paced_get(self, url: str):
        with WorkforceRunner._pace_lock:
            wait_s = WorkforceRunner._last_start + self.MIN_INTERVAL_S - time.monotonic()
            if wait_s > 0:
                time.sleep(wait_s)
            WorkforceRunner._last_start = time.monotonic()
        return self.http.get(url, "official_annual_account_copy", timeout=60, max_bytes=25_000_000, reserved="workforce")

    def _download(self, org: str, year: str):
        url = ACCOUNT_COPY.format(org=org, year=year)
        res = self._paced_get(url)
        if not res.ok and res.status != 404 and not res.blocked and not self.http.budget.past_soft_deadline():
            time.sleep(10 if res.status == 429 else 2)  # one retry on throttling or a transient error
            retry = self._paced_get(url)
            if not retry.blocked:
                res = retry
        return org, year, url, res

    def _run(self) -> None:
        backend = backend_name()
        self.status["backend"] = backend
        if not backend:
            self.status["skipped_reason"] = "No OCR backend (tesseract or rapidocr-onnxruntime) is available in this environment."
            self.log("workforce: no OCR backend available; family reported as blocked")
            return
        budget = self.http.budget
        try:
            pool = ProcessPoolExecutor(max_workers=self.workers, initializer=_init_worker, initargs=(backend,))
        except Exception as exc:  # pragma: no cover - platform specific
            self.status["skipped_reason"] = f"OCR worker pool could not start ({type(exc).__name__})."
            return
        downloads = ThreadPoolExecutor(max_workers=4)
        pending_dl = set()
        pending_ocr = {}
        queue = list(self.jobs)
        requested = 0
        started = time.monotonic()
        try:
            while (queue or pending_dl or pending_ocr) and not self._stop.is_set():
                if budget.past_soft_deadline():
                    break
                # Keep a small download lead ahead of OCR capacity.
                while queue and len(pending_dl) < 4 and len(pending_ocr) < self.workers * 2 and requested < self.request_cap:
                    org, year = queue.pop(0)
                    pending_dl.add(downloads.submit(self._download, org, year))
                    requested += 1
                if not queue or requested >= self.request_cap:
                    queue = [] if requested >= self.request_cap else queue
                waitables = list(pending_dl) + list(pending_ocr)
                if not waitables:
                    break
                done, _ = wait(waitables, timeout=2.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    if fut in pending_dl:
                        pending_dl.discard(fut)
                        org, year, url, res = fut.result()
                        if res.ok and res.body.startswith(b"%PDF"):
                            self.status["downloaded"] += 1
                            cached = self._cache_get(pdf_fingerprint(res.body))
                            if cached is not None:
                                cached.update(url=url, retrieved_at=res.retrieved_at, year=year, cache_hit=True, sha256=res.sha256)
                                self.results[org] = cached
                                self.status["accepted"] += cached.get("status") == "accepted"
                                continue
                            ocr = pool.submit(ocr_pdf_for_workforce, org, year, res.body)
                            pending_ocr[ocr] = (org, year, url, res.retrieved_at)
                        else:
                            status = "not_found" if res.status == 404 else ("blocked" if res.blocked else "download_failed")
                            self.results[org] = {"status": status, "http_status": res.status, "url": url}
                    else:
                        org, year, url, retrieved_at = pending_ocr.pop(fut)
                        try:
                            result = fut.result()
                        except Exception as exc:
                            result = {"status": "ocr_error", "error": type(exc).__name__}
                        result.update(url=url, retrieved_at=retrieved_at, year=year)
                        self.results[org] = result
                        self._cache_put(result)
                        self.status["accepted"] += result.get("status") == "accepted"
        finally:
            for fut in pending_ocr:
                fut.cancel()
            downloads.shutdown(wait=False, cancel_futures=True)
            pool.shutdown(wait=False, cancel_futures=True)
            self.status["elapsed_s"] = round(time.monotonic() - started, 1)
            self.status["processed"] = len(self.results)
            self.log(f"workforce: {self.status['accepted']} statements from {self.status['downloaded']} filings "
                     f"({len(self.results)} processed, backend {backend}, {self.workers} workers, {self.status['elapsed_s']}s)")
