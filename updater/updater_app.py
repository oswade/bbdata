"""BillBot Market Updater orchestration.

Private utility used by the dedicated Android updater app. It performs a resumable full Banking savings scan followed by a FULL Energy
scan, validates the result, builds a compact mobile snapshot,
and publishes it to a public GitHub Releases repository.

Only Python standard library modules are used so it remains portable under Chaquopy.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import billbot_core as core

APP_VERSION = "1.1.6"
STATUS_FORMAT = 1
DEFAULT_PREFIX = "market"
MAX_VERIFY_DOWNLOAD = 256 * 1024 * 1024
RESUME_FORMAT = 1
RESUME_MAX_AGE_SECONDS = 36 * 60 * 60
_RUN_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_load(text: str) -> Dict[str, Any]:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: str, value: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


class RunLogger:
    def __init__(self, log_path: str, status_path: str):
        self.log_path = os.path.abspath(log_path)
        self.status_path = os.path.abspath(status_path)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        self.run_id = str(uuid.uuid4())
        self.started = _now()
        self.state: Dict[str, Any] = {
            "format": STATUS_FORMAT,
            "run_id": self.run_id,
            "started_at": self.started,
            "updated_at": self.started,
            "state": "running",
            "stage": "Starting",
            "detail": "Preparing full Banking + Energy update",
            "percent": 0,
            "energy": {"state": "waiting", "percent": 0},
            "banking": {"state": "waiting", "percent": 0},
            "database": {"state": "waiting"},
            "upload": {"state": "waiting", "percent": 0},
            "checks": [],
            "error": "",
        }
        _atomic_json(self.status_path, self.state)
        self.log("INFO", "run", f"Update started; run_id={self.run_id}")

    def log(self, level: str, component: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "at": _now(), "level": level.upper(), "component": component,
            "message": str(message), "run_id": self.run_id,
        }
        if data:
            entry["data"] = data
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def update(self, *, stage: Optional[str] = None, detail: Optional[str] = None,
               percent: Optional[int] = None, **sections: Any) -> None:
        if stage is not None:
            self.state["stage"] = stage
        if detail is not None:
            self.state["detail"] = detail
        if percent is not None:
            self.state["percent"] = max(0, min(100, int(percent)))
        for k, v in sections.items():
            if isinstance(v, dict) and isinstance(self.state.get(k), dict):
                merged = dict(self.state[k]); merged.update(v); self.state[k] = merged
            else:
                self.state[k] = v
        self.state["updated_at"] = _now()
        _atomic_json(self.status_path, self.state)

    def check(self, name: str, ok: bool, detail: str, severity: str = "") -> None:
        severity = str(severity or ("success" if ok else "error")).strip().lower()
        if severity not in ("success", "warning", "error"):
            severity = "success" if ok else "error"
        checks = list(self.state.get("checks") or [])
        checks.append({
            "name": name, "ok": bool(ok), "severity": severity,
            "detail": str(detail), "at": _now(),
        })
        self.state["checks"] = checks[-100:]
        level = "WARN" if severity == "warning" else ("INFO" if ok else "ERROR")
        self.log(level, "check", f"{name}: {detail}")
        self.update()

    def finish(self, ok: bool, message: str, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.state["state"] = "success" if ok else "failed"
        self.state["stage"] = "Complete" if ok else "Failed"
        self.state["detail"] = message
        self.state["percent"] = 100 if ok else int(self.state.get("percent") or 0)
        self.state["finished_at"] = _now()
        self.state["error"] = "" if ok else message
        if result:
            self.state["result"] = result
        _atomic_json(self.status_path, self.state)
        self.log("INFO" if ok else "ERROR", "run", message, result)
        return {"ok": ok, "message": message, "run_id": self.run_id, "status": self.state, **(result or {})}




def _read_json_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _resume_checkpoint_path(work_dir: str) -> str:
    return str(Path(work_dir) / "resume-checkpoint.json")


def _resume_db_usable(db_path: str) -> bool:
    if not os.path.exists(db_path) or os.path.getsize(db_path) <= 0:
        return False
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            if str(con.execute("PRAGMA quick_check").fetchone()[0] or "").lower() != "ok":
                return False
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"energy_plans", "savings_products", "sync_state"}.issubset(names)
        finally:
            con.close()
    except Exception:
        return False


def _checkpoint_fresh(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) <= RESUME_MAX_AGE_SECONDS
    except Exception:
        return False


def _checkpoint_save(path: str, value: Dict[str, Any]) -> None:
    payload = dict(value)
    payload["format"] = RESUME_FORMAT
    payload["app_version"] = APP_VERSION
    payload["updated_at"] = _now()
    _atomic_json(path, payload)


def _checkpoint_clear(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalise_repository(value: str) -> str:
    repo = str(value or "").strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    repo = repo.strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def _validate_config(config: Dict[str, Any]) -> Dict[str, str]:
    repository = _normalise_repository(config.get("repository") or "")
    token = str(config.get("token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub repository must be in owner/repo format")
    if not token:
        raise ValueError("GitHub fine-grained personal access token is required")
    return {
        "repository": repository,
        "token": token,
        "api_base": "https://api.github.com",
        "public_release_base": f"https://github.com/{repository}/releases/latest/download",
        "public_manifest_url": f"https://github.com/{repository}/releases/latest/download/manifest.json",
    }


GITHUB_API_VERSION = "2026-03-10"
RELEASE_TAG_PREFIX = "billbot-market"


def _github_headers(cfg: Dict[str, str], accept: str = "application/vnd.github+json") -> Dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {cfg['token']}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": f"BillBot-Market-Updater/{APP_VERSION}",
    }


def _request_bytes(url: str, *, method: str = "GET", headers: Optional[Dict[str, str]] = None,
                   data: Optional[bytes] = None, timeout: int = 60,
                   max_read: int = MAX_VERIFY_DOWNLOAD,
                   ok_status: Tuple[int, ...] = (200,)) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(max_read + 1) if method.upper() != "HEAD" else b""
            if len(body) > max_read:
                raise ValueError(f"GitHub response exceeded {max_read:,} byte limit")
            status = int(response.status)
            if status not in ok_status:
                raise RuntimeError(f"Unexpected HTTP {status} from GitHub")
            return status, {k.lower(): v for k, v in response.headers.items()}, body
    except urllib.error.HTTPError as exc:
        body = exc.read(65536)
        detail = body.decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"GitHub HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub network error: {exc}") from exc


def _github_api_url(cfg: Dict[str, str], path: str) -> str:
    return cfg["api_base"].rstrip("/") + "/" + path.lstrip("/")


def _github_json(cfg: Dict[str, str], method: str, path_or_url: str,
                 payload: Optional[Dict[str, Any]] = None,
                 *, ok_status: Tuple[int, ...] = (200,), max_read: int = 8 * 1024 * 1024) -> Any:
    url = path_or_url if path_or_url.startswith("https://") else _github_api_url(cfg, path_or_url)
    data = None
    headers = _github_headers(cfg)
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    _, _, body = _request_bytes(url, method=method, headers=headers, data=data, timeout=90,
                                max_read=max_read, ok_status=ok_status)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("GitHub API returned non-JSON data") from exc


def _repo_path(cfg: Dict[str, str], suffix: str = "") -> str:
    return f"repos/{cfg['repository']}" + ("/" + suffix.lstrip("/") if suffix else "")


def _repository_info(cfg: Dict[str, str]) -> Dict[str, Any]:
    info = _github_json(cfg, "GET", _repo_path(cfg), ok_status=(200,))
    if not isinstance(info, dict):
        raise RuntimeError("GitHub repository lookup returned an unexpected response")
    if bool(info.get("private")):
        raise RuntimeError("GitHub data repository must be Public so BillBot can download releases without a token")
    if bool(info.get("archived")):
        raise RuntimeError("GitHub repository is archived and cannot accept releases")
    if not info.get("default_branch"):
        raise RuntimeError("GitHub repository has no default branch; initialise it with a README first")
    return info


def _list_releases(cfg: Dict[str, str]) -> list:
    value = _github_json(cfg, "GET", _repo_path(cfg, "releases?per_page=100"), ok_status=(200,))
    return value if isinstance(value, list) else []


def _create_release(cfg: Dict[str, str], *, tag: str, title: str, body: str,
                    draft: bool, prerelease: bool) -> Dict[str, Any]:
    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": bool(draft),
        "prerelease": bool(prerelease),
        "generate_release_notes": False,
    }
    if draft or prerelease:
        payload["make_latest"] = "false"
    value = _github_json(cfg, "POST", _repo_path(cfg, "releases"), payload, ok_status=(201,))
    if not isinstance(value, dict) or not value.get("id") or not value.get("upload_url"):
        raise RuntimeError("GitHub did not return a usable release object")
    return value


def _upload_release_asset(cfg: Dict[str, str], release: Dict[str, Any], name: str,
                          body: bytes, content_type: str) -> Dict[str, Any]:
    upload_base = str(release.get("upload_url") or "").split("{", 1)[0]
    if not upload_base.startswith("https://uploads.github.com/"):
        raise RuntimeError("GitHub release upload URL was missing or unexpected")
    url = upload_base + "?" + urllib.parse.urlencode({"name": name})
    headers = _github_headers(cfg)
    headers["Content-Type"] = content_type
    _, _, raw = _request_bytes(url, method="POST", headers=headers, data=body, timeout=240,
                               max_read=4 * 1024 * 1024, ok_status=(201,))
    try:
        asset = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("GitHub asset upload returned non-JSON data") from exc
    if not isinstance(asset, dict) or not asset.get("id"):
        raise RuntimeError("GitHub did not return a usable asset object")
    if int(asset.get("size") or -1) != len(body):
        raise RuntimeError(f"GitHub asset size mismatch for {name}")
    digest = str(asset.get("digest") or "")
    expected_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if digest and digest.lower() != expected_digest.lower():
        raise RuntimeError(f"GitHub asset digest mismatch for {name}")
    return asset


def _download_release_asset_authenticated(cfg: Dict[str, str], asset: Dict[str, Any],
                                          max_read: int = MAX_VERIFY_DOWNLOAD) -> bytes:
    url = str(asset.get("url") or "")
    if not url.startswith("https://api.github.com/"):
        raise RuntimeError("GitHub asset API URL was missing or unexpected")

    # GitHub may return the binary directly or redirect to a signed asset URL. Do not forward the
    # bearer token to the redirect host: capture Location first, then fetch it without Authorization.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = urllib.request.Request(url, headers=_github_headers(cfg, "application/octet-stream"), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=180) as response:
            body = response.read(max_read + 1)
            if len(body) > max_read:
                raise ValueError("Authenticated GitHub asset exceeded verification size limit")
            return body
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            detail = exc.read(65536).decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"GitHub HTTP {exc.code}: {detail}") from exc
        location = str(exc.headers.get("Location") or "")
        if not location.startswith("https://"):
            raise RuntimeError("GitHub asset redirect did not contain a valid HTTPS URL") from exc
        _, _, body = _request_bytes(
            location, method="GET", headers={"User-Agent": f"BillBot-Market-Updater/{APP_VERSION}"},
            timeout=180, max_read=max_read, ok_status=(200,)
        )
        return body


def _publish_release(cfg: Dict[str, str], release_id: int) -> Dict[str, Any]:
    value = _github_json(
        cfg, "PATCH", _repo_path(cfg, f"releases/{int(release_id)}"),
        {"draft": False, "prerelease": False, "make_latest": "true"}, ok_status=(200,)
    )
    if not isinstance(value, dict) or bool(value.get("draft")):
        raise RuntimeError("GitHub release did not publish successfully")
    return value


def _delete_release_and_tag(cfg: Dict[str, str], release_id: int, tag: str) -> None:
    try:
        _github_json(cfg, "DELETE", _repo_path(cfg, f"releases/{int(release_id)}"), ok_status=(204,))
    finally:
        if tag:
            ref = urllib.parse.quote(str(tag), safe="")
            try:
                _github_json(cfg, "DELETE", _repo_path(cfg, f"git/refs/tags/{ref}"), ok_status=(204,))
            except Exception as exc:
                if "HTTP 404" not in str(exc):
                    raise


def _public_get(url: str, *, max_read: int = MAX_VERIFY_DOWNLOAD, attempts: int = 8) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Public URL must use HTTPS")
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        req = urllib.request.Request(url, headers={
            "User-Agent": f"BillBot-Updater/{APP_VERSION}",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                body = response.read(max_read + 1)
            if len(body) > max_read:
                raise ValueError("Public verification object exceeded size limit")
            return body
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Public verification failed: {last}")


def _cleanup_old_market_releases(cfg: Dict[str, str], keep_release_id: int) -> Tuple[int, list]:
    removed = 0
    errors = []
    try:
        releases = _list_releases(cfg)
    except Exception as exc:
        return 0, [str(exc)]
    for release in releases:
        try:
            rid = int(release.get("id") or 0)
            tag = str(release.get("tag_name") or "")
            if not rid or rid == int(keep_release_id) or not tag.startswith(RELEASE_TAG_PREFIX + "-"):
                continue
            _delete_release_and_tag(cfg, rid, tag)
            removed += 1
        except Exception as exc:
            errors.append(f"{release.get('tag_name')}: {exc}")
    return removed, errors


def _free_bytes(path: str) -> int:
    usage = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".")
    return int(usage.free)


def _core_logs(db_path: str, limit: int = 1000) -> Dict[str, Any]:
    try:
        return _json_load(core.get_logs(db_path, limit))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _append_core_logs(logger: RunLogger, db_path: str, phase: str) -> None:
    logs = _core_logs(db_path, 800)
    rows = logs.get("logs") if isinstance(logs, dict) else None
    if isinstance(rows, list):
        for row in rows[-250:]:
            if isinstance(row, dict):
                logger.log(str(row.get("level") or "INFO"), f"core/{phase}", str(row.get("message") or ""))


def _validate_energy(result: Dict[str, Any], stats: Dict[str, Any], logger: RunLogger) -> None:
    endpoints = int(result.get("endpoints") or 0)
    coverage_issues = int(result.get("coverage_issues") or 0)
    detail_failures = int(result.get("detail_failures") or 0)
    validated_raw = result.get("validated_retailers")
    if validated_raw is None:
        # Compatibility for older/fake cores used by tests: when there are no unresolved Details,
        # endpoint coverage issues are the only known reason a catalogue is not validated.
        validated_retailers = max(0, endpoints - coverage_issues) if detail_failures == 0 else 0
    else:
        validated_retailers = max(0, int(validated_raw or 0))
    min_verified = max(1, (endpoints * 7 + 9) // 10) if endpoints else 1

    checks = [
        (str(result.get("mode") or "").lower() == "full", "Energy mode", f"mode={result.get('mode')}"),
        (bool(result.get("ok")), "Energy completion", f"status={result.get('status')}"),
        (endpoints > 0, "Energy endpoints", f"endpoints={endpoints}"),
        (int(stats.get("electricity_plans") or 0) > 0, "Electricity rows", f"plans={int(stats.get('electricity_plans') or 0)}"),
        (int(stats.get("gas_plans") or 0) > 0, "Gas rows", f"plans={int(stats.get('gas_plans') or 0)}"),
        (validated_retailers >= min_verified, "Energy verified catalogues",
         f"validated={validated_retailers}/{endpoints}; minimum={min_verified}"),
    ]
    failures = []
    for ok, name, detail in checks:
        logger.check(name, ok, detail)
        if not ok:
            failures.append(f"{name} ({detail})")

    # Plan Detail failures are fail-closed at retailer level in the core. Affected retailers are
    # omitted from validated_retailer_keys and therefore from the published snapshot. This means a
    # transient DNS/socket outage at one retailer can be a warning when national verified coverage
    # remains healthy, while widespread Detail loss still fails through the 70% coverage floor.
    if detail_failures:
        detail_names = [str(x).strip() for x in (result.get("detail_failure_names") or []) if str(x).strip()]
        affected = ", ".join(detail_names[:6]) or "see Energy log"
        recovered = int(result.get("detail_recovered") or 0)
        attempted = int(result.get("detail_recovery_attempted") or 0)
        detail_ok = validated_retailers >= min_verified
        detail = (
            f"WARNING: detail_failures={detail_failures}; affected={affected}; "
            f"recovered={recovered}/{attempted}; unverified retailer catalogue(s) excluded"
        )
        logger.check("Energy Detail", detail_ok, detail, severity="warning" if detail_ok else "error")
        if not detail_ok:
            failures.append(f"Energy Detail (detail_failures={detail_failures})")
    else:
        attempted = int(result.get("detail_recovery_attempted") or 0)
        recovered = int(result.get("detail_recovered") or 0)
        suffix = f"; recovered={recovered}/{attempted}" if attempted else ""
        logger.check("Energy Detail", True, f"detail_failures=0{suffix}")

    # A FULL scan can be usable even when one AER catalogue is temporarily unavailable.
    # The Energy core records exactly which retailer catalogues validated cleanly; the cloud
    # exporter publishes only those validated retailers. Coverage gaps therefore remain prominent
    # warnings while structural failures and widespread coverage loss remain fatal.
    coverage_names = [str(x).strip() for x in (result.get("coverage_failure_names") or []) if str(x).strip()]
    if coverage_issues:
        affected = ", ".join(coverage_names[:6]) or "see Energy log"
        logger.check(
            "Energy coverage", True,
            f"WARNING: coverage_issues={coverage_issues}; affected={affected}; unverified retailer data will be excluded from this snapshot",
            severity="warning",
        )
    else:
        logger.check("Energy coverage", True, "coverage_issues=0")

    if failures:
        raise RuntimeError("Energy validation failed: " + "; ".join(failures))


def _validate_banking(result: Dict[str, Any], stats: Dict[str, Any], logger: RunLogger) -> None:
    """Validate Banking without turning isolated holder outages into a global outage.

    refresh_savings is fail-closed per holder: any holder with a failed list or Product Detail is
    omitted/preserved as a whole catalogue.  A partial run is therefore publishable when enough
    independently verified holders and savings products remain; the omissions are surfaced as
    warnings and written into the manifest.
    """
    holders = int(result.get("holders") or 0)
    verified = int(result.get("verified_holders") or 0)
    rows = int(stats.get("savings_products") or 0)
    list_failures = int(result.get("list_failures") or 0)
    detail_failures = int(result.get("detail_failures") or 0)
    coverage_issues = int(result.get("coverage_issues") or 0)
    failed_names = [str(x).strip() for x in (result.get("failed_holder_names") or []) if str(x).strip()]
    coverage_ratio = (verified / holders) if holders > 0 else 0.0

    minimum_rows = 300 if holders >= 50 else 1
    hard_checks = [
        (bool(result.get("ok")), "Banking usable result", f"status={result.get('status')}"),
        (holders > 0, "Banking holders", f"holders={holders}"),
        (verified > 0, "Banking verified holders", f"verified={verified}/{holders}"),
        (coverage_ratio >= 0.70, "Banking holder coverage", f"verified={verified}/{holders} ({coverage_ratio:.1%})"),
        (rows >= minimum_rows, "Savings rows", f"products={rows}; minimum={minimum_rows}"),
    ]
    failures = []
    for ok, name, detail in hard_checks:
        logger.check(name, ok, detail)
        if not ok:
            failures.append(f"{name} ({detail})")

    if coverage_issues:
        affected = ", ".join(failed_names[:8]) or "see Banking log"
        logger.check(
            "Banking completeness", True,
            f"WARNING: {coverage_issues} holder catalogue(s) unavailable after rescue; affected={affected}; incomplete holders are excluded from this snapshot",
            severity="warning",
        )
        if list_failures:
            logger.check("Banking lists", True, f"WARNING: list_failures={list_failures}; failed holder catalogues excluded", severity="warning")
        else:
            logger.check("Banking lists", True, "list_failures=0")
        if detail_failures:
            logger.check("Banking Detail", True, f"WARNING: detail_failures={detail_failures}; affected holder catalogues excluded", severity="warning")
        else:
            logger.check("Banking Detail", True, "detail_failures=0")
    else:
        logger.check("Banking completeness", bool(result.get("complete")), f"complete={result.get('complete')}")
        logger.check("Banking lists", list_failures == 0, f"list_failures={list_failures}")
        logger.check("Banking Detail", detail_failures == 0, f"detail_failures={detail_failures}")
        if not bool(result.get("complete")):
            failures.append(f"Banking completeness (complete={result.get('complete')})")
        if list_failures:
            failures.append(f"Banking lists (list_failures={list_failures})")
        if detail_failures:
            failures.append(f"Banking Detail (detail_failures={detail_failures})")

    if failures:
        raise RuntimeError("Banking validation failed: " + "; ".join(failures))


def _sqlite_checks(db_path: str, logger: RunLogger) -> None:
    con = sqlite3.connect(db_path)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0] or "")
        logger.check("SQLite integrity", integrity.lower() == "ok", integrity)
        if integrity.lower() != "ok":
            raise RuntimeError("SQLite integrity_check failed: " + integrity)
        quick = str(con.execute("PRAGMA quick_check").fetchone()[0] or "")
        logger.check("SQLite quick check", quick.lower() == "ok", quick)
        if quick.lower() != "ok":
            raise RuntimeError("SQLite quick_check failed: " + quick)
    finally:
        con.close()


def _patch_manifest(manifest_path: str, cfg: Dict[str, str]) -> Dict[str, Any]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        raise RuntimeError("Exported manifest is not an object")
    manifest["download_url"] = cfg["public_release_base"].rstrip("/") + "/latest.db.gz"
    manifest["publisher"] = "BillBot Market Updater Android / GitHub Releases"
    manifest["publisher_version"] = APP_VERSION
    manifest["published_at"] = _now()
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, manifest_path)
    return manifest


def _run_update_locked(db_path: str, work_dir: str, status_path: str, log_path: str, config_json: str, publish: bool = True) -> str:
    previous_status = _read_json_file(status_path)
    logger = RunLogger(log_path, status_path)
    result: Dict[str, Any] = {}
    try:
        cfg = _validate_config(_json_load(config_json)) if publish else None
        if cfg:
            logger.check("GitHub configuration", True, f"repository={cfg['repository']}")
        else:
            logger.log("INFO", "storage", "Scan-only mode: publish step disabled")
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        snapshot_gz = str(work / "snapshot.db.gz")
        manifest_path = str(work / "manifest.json")
        resume_path = _resume_checkpoint_path(str(work))
        repository_identity = (cfg or {}).get("repository", "") if cfg else "scan-only"

        checkpoint = _read_json_file(resume_path)
        checkpoint_valid = bool(
            checkpoint
            and int(checkpoint.get("format") or 0) == RESUME_FORMAT
            and bool(checkpoint.get("publish")) == bool(publish)
            and str(checkpoint.get("repository") or "") == repository_identity
            and _checkpoint_fresh(resume_path)
            and _resume_db_usable(str(db))
        )

        # Upgrade-resume path for a run started by v1.1.5. That version had no checkpoint file,
        # but its status/result and working DB are sufficient to preserve a completed Energy scan.
        # This lets the first v1.1.6 run continue with Banking instead of throwing away ~9 minutes
        # of already verified Energy work.
        legacy_resume = False
        if not checkpoint_valid and _resume_db_usable(str(db)):
            try:
                previous_energy = previous_status.get("energy") if isinstance(previous_status.get("energy"), dict) else {}
                energy_result = previous_energy.get("result") if isinstance(previous_energy.get("result"), dict) else {}
                previous_running = str(previous_status.get("state") or "") == "running"
                previous_updated = str(previous_status.get("updated_at") or "")
                updated_dt = dt.datetime.fromisoformat(previous_updated.replace("Z", "+00:00")) if previous_updated else None
                recent = bool(updated_dt and (dt.datetime.now(dt.timezone.utc) - updated_dt).total_seconds() <= RESUME_MAX_AGE_SECONDS)
                if previous_running and recent and energy_result.get("ok") and str(energy_result.get("mode") or "").upper() == "FULL":
                    checkpoint = {
                        "publish": bool(publish), "repository": repository_identity,
                        "banking_done": False, "energy_done": True,
                        "banking_result": {}, "energy_result": energy_result,
                        "active_stage": "banking", "legacy_resume": True,
                    }
                    _checkpoint_save(resume_path, checkpoint)
                    checkpoint_valid = True
                    legacy_resume = True
            except Exception:
                pass

        if checkpoint_valid:
            logger.log("INFO", "resume", "Resuming interrupted updater run from durable checkpoint", {
                "banking_done": bool(checkpoint.get("banking_done")),
                "energy_done": bool(checkpoint.get("energy_done")),
                "legacy_resume": legacy_resume,
            })
            logger.update(stage="Resuming interrupted update", detail="Reusing verified work already stored on this device", percent=2)
            logger.check("Resumable database loaded", True, str(db))
        else:
            checkpoint = {
                "publish": bool(publish), "repository": repository_identity,
                "banking_done": False, "energy_done": False,
                "banking_result": {}, "energy_result": {}, "active_stage": "initialising",
            }
            for candidate in [db, Path(str(db)+"-wal"), Path(str(db)+"-shm"), Path(str(db)+"-journal")]:
                try: candidate.unlink()
                except FileNotFoundError: pass
            for stale_progress in (work / "energy-progress.json", work / "banking-progress.json"):
                try: stale_progress.unlink()
                except FileNotFoundError: pass
            for p in (snapshot_gz, manifest_path):
                try: os.remove(p)
                except FileNotFoundError: pass

            free = _free_bytes(str(db))
            logger.check("Free storage", free >= 500 * 1024 * 1024, f"{free / (1024**3):.2f} GiB free")
            if free < 500 * 1024 * 1024:
                raise RuntimeError("At least 500 MB free storage is required for a safe full rebuild")

            logger.update(stage="Initialising database", detail="Creating a resumable working database", percent=2,
                          database={"state": "running"})
            init = _json_load(core.init_app(str(db)))
            if not init.get("ok"):
                raise RuntimeError("Database initialisation failed: " + str(init.get("message") or init))
            logger.check("Fresh database initialised", True, str(db))
            checkpoint["active_stage"] = "banking"
            _checkpoint_save(resume_path, checkpoint)

        # Even resumed runs need a current storage safety check before doing more network work.
        free = _free_bytes(str(db))
        if checkpoint_valid:
            logger.check("Free storage", free >= 500 * 1024 * 1024, f"{free / (1024**3):.2f} GiB free")
        if free < 500 * 1024 * 1024:
            raise RuntimeError("At least 500 MB free storage is required to continue the resumable update")

        # Banking now runs first. It also maintains holder/list/Product-Detail checkpoints inside
        # the working SQLite database, so a killed process resumes the unfinished network work.
        banking_progress = str(work / "banking-progress.json")
        if checkpoint.get("banking_done") and isinstance(checkpoint.get("banking_result"), dict):
            banking = dict(checkpoint.get("banking_result") or {})
            stats_after_banking = _json_load(core.get_stats(str(db)))
            _validate_banking(banking, stats_after_banking, logger)
            banking_coverage_issues = int(banking.get("coverage_issues") or 0)
            banking_state = "warning" if banking_coverage_issues else "success"
            logger.update(stage="Banking restored from checkpoint", detail=str(banking.get("message") or "Banking already verified"), percent=38,
                          banking={"state": banking_state, "percent": 100, "result": banking})
            logger.log("INFO", "resume", "Banking stage already complete; network scan skipped")
        else:
            checkpoint["active_stage"] = "banking"
            _checkpoint_save(resume_path, checkpoint)
            logger.update(stage="FULL Banking scan", detail="Downloading all current savings products and Product Detail", percent=5,
                          banking={"state": "running", "percent": 1})
            logger.log("INFO", "banking", "Starting resumable full Banking savings scan")
            banking_started = time.monotonic()
            banking = _json_load(core.refresh_savings(str(db), banking_progress))
            logger.log("INFO", "banking", f"Banking scan returned after {time.monotonic()-banking_started:.1f}s", banking)
            _append_core_logs(logger, str(db), "banking")
            stats_after_banking = _json_load(core.get_stats(str(db)))
            _validate_banking(banking, stats_after_banking, logger)
            banking_coverage_issues = int(banking.get("coverage_issues") or 0)
            banking_state = "warning" if banking_coverage_issues else "success"
            banking_detail = str(banking.get("message") or "Banking complete")
            if banking_coverage_issues:
                banking_detail += " Unverified holder catalogue(s) will be excluded from the published snapshot."
            logger.update(stage="Banking verified" if not banking_coverage_issues else "Banking verified with warning",
                          detail=banking_detail, percent=38,
                          banking={"state": banking_state, "percent": 100, "result": banking})
            checkpoint.update({"banking_done": True, "banking_result": banking, "active_stage": "energy"})
            _checkpoint_save(resume_path, checkpoint)

        energy_progress = str(work / "energy-progress.json")
        if checkpoint.get("energy_done") and isinstance(checkpoint.get("energy_result"), dict):
            energy = dict(checkpoint.get("energy_result") or {})
            stats_after_energy = _json_load(core.get_stats(str(db)))
            _validate_energy(energy, stats_after_energy, logger)
            energy_coverage_issues = int(energy.get("coverage_issues") or 0)
            energy_state = "warning" if energy_coverage_issues else "success"
            logger.update(stage="Energy restored from checkpoint", detail=str(energy.get("message") or "Energy already verified"), percent=80,
                          energy={"state": energy_state, "percent": 100, "result": energy})
            logger.log("INFO", "resume", "Energy stage already complete; network scan skipped")
        else:
            checkpoint["active_stage"] = "energy"
            _checkpoint_save(resume_path, checkpoint)
            logger.update(stage="FULL Energy scan", detail="Discovering and verifying the complete national Energy catalogue", percent=40,
                          energy={"state": "running", "percent": 1})
            logger.log("INFO", "energy", "Starting strict FULL Energy scan after Banking")
            energy_started = time.monotonic()
            energy = _json_load(core.sync_energy_all(str(db), "", "", energy_progress, "FULL"))
            logger.log("INFO", "energy", f"FULL Energy scan returned after {time.monotonic()-energy_started:.1f}s", energy)
            _append_core_logs(logger, str(db), "energy")
            stats_after_energy = _json_load(core.get_stats(str(db)))
            _validate_energy(energy, stats_after_energy, logger)
            energy_coverage_issues = int(energy.get("coverage_issues") or 0)
            energy_state = "warning" if energy_coverage_issues else "success"
            energy_detail = str(energy.get("message") or "Energy complete")
            if energy_coverage_issues:
                energy_detail += " Unverified retailer catalogue(s) will be excluded from the published snapshot."
            logger.update(stage="Energy verified" if not energy_coverage_issues else "Energy verified with warning",
                          detail=energy_detail, percent=80,
                          energy={"state": energy_state, "percent": 100, "result": energy})
            checkpoint.update({"energy_done": True, "energy_result": energy, "active_stage": "database"})
            _checkpoint_save(resume_path, checkpoint)

        stats = _json_load(core.get_stats(str(db)))
        logger.update(stage="Database checks", detail="Running integrity and export checks", percent=80,
                      database={"state": "running"})
        _sqlite_checks(str(db), logger)
        logger.check("Market row counts", True,
                     f"electricity={stats.get('electricity_plans')}, gas={stats.get('gas_plans')}, savings={stats.get('savings_products')}")

        public_market_base = cfg["public_release_base"] if cfg else ""
        exported = _json_load(core.export_cloud_snapshot(str(db), snapshot_gz, manifest_path, public_market_base))
        if not exported.get("ok"):
            raise RuntimeError(str(exported.get("message") or "Snapshot export failed"))
        manifest = exported.get("manifest") if isinstance(exported.get("manifest"), dict) else {}
        # Banking is sanitized before export: any holder with an unresolved list/detail failure is
        # omitted as a whole catalogue.  Record those omissions in the public manifest so clients
        # and diagnostics never mistake a warning snapshot for complete national coverage.
        manifest.update({
            "banking_status": str(banking.get("status") or "unknown"),
            "banking_coverage_issues": int(banking.get("coverage_issues") or 0),
            "banking_list_failures": int(banking.get("list_failures") or 0),
            "banking_detail_failures": int(banking.get("detail_failures") or 0),
            "verified_banking_holders": int(banking.get("verified_holders") or 0),
            "banking_holders": int(banking.get("holders") or 0),
            "excluded_unverified_banking_holders": list(banking.get("failed_holder_names") or []),
        })
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        snapshot_id = str(manifest.get("snapshot_id") or "").strip()
        if not snapshot_id:
            raise RuntimeError("Snapshot export did not produce snapshot_id")
        compressed_sha = _sha256_file(snapshot_gz)
        expected_sha = str(manifest.get("sha256") or "")
        logger.check("Snapshot SHA-256", compressed_sha == expected_sha, compressed_sha)
        if compressed_sha != expected_sha:
            raise RuntimeError("Local snapshot SHA-256 differs from manifest")
        logger.check("Snapshot size", os.path.getsize(snapshot_gz) == int(manifest.get("compressed_bytes") or -1),
                     f"{os.path.getsize(snapshot_gz):,} bytes")
        if os.path.getsize(snapshot_gz) != int(manifest.get("compressed_bytes") or -1):
            raise RuntimeError("Local snapshot size differs from manifest")
        logger.update(stage="Database ready", detail=f"Snapshot {snapshot_id[:12]}… ready" + (" to upload" if publish else " locally"), percent=86,
                      database={"state": "success", "snapshot_id": snapshot_id, "compressed_bytes": os.path.getsize(snapshot_gz), "counts": manifest.get("counts")})

        db_key = ""
        public_manifest_url = ""
        removed = 0
        cleanup_errors = []
        if publish:
            assert cfg is not None
            _repository_info(cfg)
            release = None
            release_tag = f"{RELEASE_TAG_PREFIX}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            db_key = "latest.db.gz"
            try:
                manifest = _patch_manifest(manifest_path, cfg)
                logger.update(stage="Creating GitHub release", detail=release_tag, percent=88,
                              upload={"state": "running", "percent": 8})
                release = _create_release(
                    cfg, tag=release_tag,
                    title=f"BillBot Market Data {manifest.get('generated_at') or _now()}",
                    body="Automated current Energy + Banking market snapshot published by the BillBot Market Updater.",
                    draft=True, prerelease=False,
                )

                logger.update(stage="Uploading snapshot", detail="latest.db.gz", percent=90,
                              upload={"state": "running", "percent": 25})
                snapshot_bytes = Path(snapshot_gz).read_bytes()
                db_asset = _upload_release_asset(cfg, release, "latest.db.gz", snapshot_bytes, "application/gzip")
                private_back = _download_release_asset_authenticated(cfg, db_asset, max_read=MAX_VERIFY_DOWNLOAD)
                private_sha = hashlib.sha256(private_back).hexdigest()
                logger.check("Authenticated GitHub snapshot verification", private_sha == expected_sha, private_sha)
                if private_sha != expected_sha:
                    raise RuntimeError("Uploaded GitHub snapshot failed authenticated SHA-256 verification")

                logger.update(stage="Uploading manifest", detail="manifest.json", percent=94,
                              upload={"state": "running", "percent": 55})
                manifest_bytes = Path(manifest_path).read_bytes()
                manifest_asset = _upload_release_asset(cfg, release, "manifest.json", manifest_bytes, "application/json")
                private_manifest = _download_release_asset_authenticated(cfg, manifest_asset, max_read=2 * 1024 * 1024)
                private_manifest_obj = json.loads(private_manifest.decode("utf-8"))
                private_manifest_ok = (
                    isinstance(private_manifest_obj, dict) and
                    str(private_manifest_obj.get("snapshot_id") or "") == snapshot_id and
                    str(private_manifest_obj.get("sha256") or "") == expected_sha and
                    str(private_manifest_obj.get("download_url") or "") == str(manifest.get("download_url") or "")
                )
                logger.check("Authenticated GitHub manifest verification", private_manifest_ok, "manifest.json")
                if not private_manifest_ok:
                    raise RuntimeError("Uploaded GitHub manifest did not match the local manifest")

                logger.update(stage="Publishing GitHub release", detail=release_tag, percent=96,
                              upload={"state": "running", "percent": 72})
                release = _publish_release(cfg, int(release["id"]))
                public_manifest_url = cfg["public_manifest_url"]

                logger.update(stage="Verifying public GitHub release", detail=public_manifest_url, percent=98,
                              upload={"state": "running", "percent": 88})
                public_db = _public_get(str(manifest["download_url"]), max_read=MAX_VERIFY_DOWNLOAD)
                public_sha = hashlib.sha256(public_db).hexdigest()
                logger.check("Public GitHub snapshot verification", public_sha == expected_sha, public_sha)
                if public_sha != expected_sha:
                    raise RuntimeError("Public GitHub snapshot failed SHA-256 verification")
                manifest_back = _public_get(public_manifest_url, max_read=2 * 1024 * 1024)
                live_manifest = json.loads(manifest_back.decode("utf-8"))
                manifest_ok = (
                    isinstance(live_manifest, dict) and
                    str(live_manifest.get("snapshot_id") or "") == snapshot_id and
                    str(live_manifest.get("sha256") or "") == expected_sha and
                    str(live_manifest.get("download_url") or "") == str(manifest.get("download_url") or "")
                )
                logger.check("Published GitHub manifest verification", manifest_ok, public_manifest_url)
                if not manifest_ok:
                    raise RuntimeError("Published GitHub manifest did not match the locally verified manifest")

                removed, cleanup_errors = _cleanup_old_market_releases(cfg, int(release["id"]))
                logger.log("WARN" if cleanup_errors else "INFO", "cleanup",
                           f"Removed {removed} old BillBot GitHub release(s); cleanup_errors={len(cleanup_errors)}",
                           {"errors": cleanup_errors[:10]})
                logger.update(stage="Published", detail="Energy + Banking database is live on GitHub Releases", percent=100,
                              upload={"state": "success", "percent": 100, "manifest_url": public_manifest_url,
                                      "release_url": str(release.get("html_url") or "")})
            except Exception:
                if release and release.get("id"):
                    try:
                        _delete_release_and_tag(cfg, int(release["id"]), release_tag)
                    except Exception as cleanup_exc:
                        logger.log("WARN", "cleanup", f"Could not remove failed GitHub release: {cleanup_exc}")
                raise
        else:
            logger.update(stage="Validated locally", detail="Full Energy + Banking scan and snapshot checks passed; nothing uploaded", percent=100,
                          upload={"state": "skipped", "percent": 0})

        result = {
            "energy": energy,
            "banking": banking,
            "stats": stats,
            "manifest": manifest,
            "manifest_url": public_manifest_url,
            "db_key": db_key,
            "cleanup_removed": removed,
            "cleanup_errors": cleanup_errors,
            "published": bool(publish),
        }
        coverage_issues = int(energy.get("coverage_issues") or 0)
        if publish:
            message = (
                f"Energy + Banking update published successfully with {coverage_issues} Energy coverage warning(s)"
                if coverage_issues else "Full Energy + Banking update published successfully"
            )
        else:
            message = (
                f"Energy + Banking scan validated with {coverage_issues} Energy coverage warning(s) (scan-only)"
                if coverage_issues else "Full Energy + Banking scan validated successfully (scan-only)"
            )
        _checkpoint_clear(resume_path)
        return json.dumps(logger.finish(True, message, result), separators=(",", ":"))
    except Exception as exc:
        logger.log("ERROR", "exception", repr(exc))
        return json.dumps(logger.finish(False, str(exc), result), separators=(",", ":"))



def run_update(db_path: str, work_dir: str, status_path: str, log_path: str, config_json: str, publish: bool = True) -> str:
    if not _RUN_LOCK.acquire(blocking=False):
        return json.dumps({"ok": False, "message": "Another BillBot updater run is already active", "status": "busy"}, separators=(",", ":"))
    try:
        return _run_update_locked(db_path, work_dir, status_path, log_path, config_json, bool(publish))
    finally:
        _RUN_LOCK.release()


def test_storage(config_json: str, log_path: str = "", status_path: str = "") -> str:
    cfg = _validate_config(_json_load(config_json))
    run_id = uuid.uuid4().hex[:12]
    tag = f"{RELEASE_TAG_PREFIX}-storage-test-{run_id}"
    body = ("BillBot GitHub storage test " + _now() + "\n").encode("utf-8")
    notes = []
    release = None
    try:
        info = _repository_info(cfg)
        notes.append(f"repository access ok ({info.get('full_name') or cfg['repository']})")
        release = _create_release(
            cfg, tag=tag, title="BillBot storage test", body="Temporary self-cleaning updater test.",
            draft=False, prerelease=True,
        )
        notes.append("temporary prerelease create ok")
        asset = _upload_release_asset(cfg, release, "billbot-storage-test.txt", body, "text/plain; charset=utf-8")
        notes.append("asset upload ok")
        private = _download_release_asset_authenticated(cfg, asset, max_read=4096)
        if private != body:
            raise RuntimeError("Authenticated GitHub asset read returned different content")
        notes.append("authenticated asset read ok")
        public_url = str(asset.get("browser_download_url") or "")
        public = _public_get(public_url, max_read=4096, attempts=5)
        if public != body:
            raise RuntimeError("Public GitHub release asset returned different content")
        notes.append("public asset read ok")
        _delete_release_and_tag(cfg, int(release["id"]), tag)
        release = None
        notes.append("temporary release delete ok")
        return json.dumps({
            "ok": True, "message": "GitHub storage test passed", "checks": notes,
            "public_url": public_url, "manifest_url": cfg["public_manifest_url"],
        }, separators=(",", ":"))
    except Exception as exc:
        if release and release.get("id"):
            try:
                _delete_release_and_tag(cfg, int(release["id"]), tag)
                notes.append("cleanup after failure ok")
            except Exception as cleanup_exc:
                notes.append("cleanup failed: " + str(cleanup_exc))
        return json.dumps({"ok": False, "message": str(exc), "checks": notes}, separators=(",", ":"))


def local_diagnostics(db_path: str, work_dir: str, config_json: str = "") -> str:
    result: Dict[str, Any] = {"ok": True, "at": _now(), "app_version": APP_VERSION}
    try:
        result["free_bytes"] = _free_bytes(db_path)
    except Exception as exc:
        result["free_bytes_error"] = str(exc)
    if os.path.exists(db_path):
        result["db_bytes"] = os.path.getsize(db_path)
        try:
            con = sqlite3.connect(db_path)
            result["integrity"] = str(con.execute("PRAGMA integrity_check").fetchone()[0] or "")
            con.close()
        except Exception as exc:
            result["integrity"] = "error: " + str(exc)
        result["stats"] = _json_load(core.get_stats(db_path))
    else:
        result["db_bytes"] = 0
        result["integrity"] = "no database yet"
    result["work_dir"] = work_dir
    resume_path = _resume_checkpoint_path(work_dir)
    resume = _read_json_file(resume_path)
    result["resume_checkpoint"] = {
        "present": bool(resume),
        "fresh": _checkpoint_fresh(resume_path) if resume else False,
        "banking_done": bool(resume.get("banking_done")) if resume else False,
        "energy_done": bool(resume.get("energy_done")) if resume else False,
        "active_stage": str(resume.get("active_stage") or "") if resume else "",
        "updated_at": str(resume.get("updated_at") or "") if resume else "",
    }
    if config_json:
        try:
            cfg = _validate_config(_json_load(config_json))
            result["storage_config"] = {
                "valid": True, "repository": cfg["repository"],
                "public_manifest_url": cfg["public_manifest_url"],
                "token_present": bool(cfg["token"]),
            }
        except Exception as exc:
            result["storage_config"] = {"valid": False, "error": str(exc)}
    return json.dumps(result, separators=(",", ":"))


def self_test() -> str:
    cfg = _validate_config({"repository": "example/billbot-market-data", "token": "TOKEN"})
    ok = (
        cfg["repository"] == "example/billbot-market-data" and
        cfg["public_release_base"] == "https://github.com/example/billbot-market-data/releases/latest/download" and
        cfg["public_manifest_url"].endswith("/releases/latest/download/manifest.json") and
        _normalise_repository("https://github.com/example/billbot-market-data.git") == "example/billbot-market-data"
    )
    return json.dumps({
        "ok": ok,
        "repository": cfg["repository"],
        "manifest_url": cfg["public_manifest_url"],
        "api_version": GITHUB_API_VERSION,
    }, separators=(",", ":"))

