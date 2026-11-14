"""每租户一个 WhatsApp Bridge 进程。

一个 Node 进程只能登录一个 WhatsApp 账号，所以多租户必须一租户一进程：
端口从池子里分配，会话目录落在各自的 ``<data_dir>/data/.baileys-auth``，
进程环境里带上 ``WA_TENANT_ID``，让 webhook 回调能认领到正确的租户。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import tenancy
from config import get_config
from tenancy import Tenant

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
_PORT_BASE = 3101
_PORT_MAX = 3999

_lock = threading.RLock()
_procs: Dict[str, subprocess.Popen] = {}
_ports: Dict[str, int] = {}
_pids: Dict[str, int] = {}
_tokens: Dict[str, str] = {}
# 用户主动启动过的租户才需要看门狗拉起；崩溃重启次数用于防止无限重启
_enabled: Dict[str, bool] = {}
_restarts: Dict[str, list] = {}
_MAX_RESTARTS = 3
_RESTART_WINDOW = 600.0
_watchdog: Optional[threading.Thread] = None


def _registry_path() -> Path:
    return tenancy.base_data_dir() / "bridges.json"


def _load_registry() -> None:
    path = _registry_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    for tenant_id, info in (data or {}).items():
        if not isinstance(info, dict):
            continue
        port = info.get("port")
        if isinstance(port, int):
            _ports.setdefault(tenant_id, port)
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0:
            _pids.setdefault(tenant_id, pid)
        token = info.get("token")
        if isinstance(token, str) and token:
            _tokens.setdefault(tenant_id, token)
        if info.get("enabled"):
            _enabled[tenant_id] = True


def _save_registry() -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        tid: {
            "port": port,
            "enabled": bool(_enabled.get(tid)),
            "pid": _pids.get(tid),
            "token": _tokens.get(tid),
        }
        for tid, port in _ports.items()
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


_load_registry()


def _pid_is_bridge(pid: Optional[int]) -> bool:
    """Best-effort check that a recorded PID is alive and is our bridge process."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        proc_cmd = Path(f"/proc/{pid}/cmdline")
        if proc_cmd.exists():
            cmd = proc_cmd.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            return "bridge/index.js" in cmd or "bridge\\index.js" in cmd
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=1.5,
        )
        if res.returncode == 0:
            return "bridge/index.js" in res.stdout or "bridge\\index.js" in res.stdout
    except Exception:
        pass
    return False


def _terminate_pid(pid: Optional[int]) -> None:
    if not _pid_is_bridge(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def verify_bridge_token(tenant_id: str, presented: Optional[str]) -> bool:
    """Verify X-Bridge-Token for webhook callbacks.

    If WA_WEBHOOK_SECRET is configured, or bridge_manager issued a token for this
    tenant, require an exact constant-time match.
    """
    global_secret = (os.getenv("WA_WEBHOOK_SECRET") or "").strip()
    if global_secret:
        return hmac.compare_digest(global_secret, presented or "")
    expected = _tokens.get(tenant_id)
    if not expected:
        return True
    return hmac.compare_digest(expected, presented or "")


def auth_headers(tenant: Optional[Tenant] = None) -> Dict[str, str]:
    """Return X-Bridge-Token header for outbound Python -> Bridge requests."""
    tenant = tenant or tenancy.current()
    if tenant is None:
        return {}
    token = _tokens.get(tenant.user_id)
    if not token:
        return {}
    return {"X-Bridge-Token": token}


# ── 端口 ────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _alive(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _allocate_port() -> int:
    used = set(_ports.values())
    for port in range(_PORT_BASE, _PORT_MAX + 1):
        if port in used or not _port_free(port):
            continue
        return port
    raise RuntimeError("没有可用的 bridge 端口")


def port_for(tenant_id: str) -> Optional[int]:
    return _ports.get(tenant_id)


def current_port() -> int:
    """当前请求租户的 bridge 端口；没有租户时退回配置里的默认端口。"""
    tenant = tenancy.current()
    if tenant is not None:
        port = _ports.get(tenant.user_id)
        if port:
            return port
    try:
        return get_config().bridge.port
    except Exception:
        return int(os.getenv("BRIDGE_PORT", "3001") or 3001)


# ── 进程 ────────────────────────────────────────────────────────────────

# ── 代理 ────────────────────────────────────────────────────────────────

def proxy_for(tenant: Optional[Tenant] = None) -> str:
    """解析该租户的出站代理：租户 config.yaml 优先，其次进程环境变量。

    bridge 子进程只认 HTTPS_PROXY 环境变量，所以这里必须显式解析后传进去，
    否则控制台里保存的 bridge.proxy 永远不生效。
    """
    tenant = tenant or tenancy.current()
    if tenant is not None:
        token = tenancy.set_current(tenant)
        try:
            configured = (get_config().bridge.proxy or "").strip()
        except Exception:
            configured = ""
        finally:
            tenancy.reset_current(token)
        if configured:
            return configured
    return (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip()


def _node_bin() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("找不到 node，请先安装 Node.js")
    return node


def _api_port() -> int:
    env_port = (os.getenv("API_PORT") or "").strip()
    if env_port.isdigit():
        return int(env_port)
    try:
        return get_config().api.port
    except Exception:
        return 8000


def _log_path(data_dir: Path) -> Path:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "bridge.log"


def _rotate_bridge_log(path: Path, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3) -> None:
    """Rotate oversized bridge.log before spawning a child process."""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for idx in range(backup_count, 0, -1):
            src = path if idx == 1 else path.with_name(f"{path.name}.{idx - 1}")
            dst = path.with_name(f"{path.name}.{idx}")
            if src.exists():
                src.replace(dst)
    except Exception:
        logger.warning("[bridge] 日志轮转失败: %s", path, exc_info=True)


def start(tenant: Tenant) -> dict:
    """确保该租户的 bridge 在运行，返回状态。"""
    with _lock:
        proc = _procs.get(tenant.user_id)
        port = _ports.get(tenant.user_id)

        if port and _alive(port) and (proc is None or proc.poll() is None):
            return {"online": True, "port": port, "started": False}

        if (ROOT / "bridge" / "index.js").exists() is False:
            raise RuntimeError("缺少 bridge/index.js")
        if not (ROOT / "bridge" / "node_modules").exists():
            raise RuntimeError("bridge 依赖未安装：npm --prefix bridge install")

        # 清理可能残留的孤儿 bridge 进程，防止双实例竞争同一 .baileys-auth 目录
        if proc is None:
            _terminate_pid(_pids.pop(tenant.user_id, None))
        elif proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        port = _ports.get(tenant.user_id)
        if not port or not _port_free(port):
            port = _allocate_port()
        token = _tokens.get(tenant.user_id) or secrets.token_urlsafe(24)
        env = os.environ.copy()
        env.update({
            "WHATSAPP_BOT_DATA_DIR": str(tenant.data_dir),
            "WA_TENANT_ID": tenant.user_id,
            "WA_BRIDGE_TOKEN": token,
            "BRIDGE_PORT": str(port),
            "BRIDGE_HOST": "127.0.0.1",
            "PYTHON_API": f"http://127.0.0.1:{_api_port()}",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })

        # 代理是"显式传递"的：bridge 只认 HTTPS_PROXY，不读 config.yaml
        proxy = proxy_for(tenant)
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env.pop(key, None)
        if proxy:
            env["HTTPS_PROXY"] = env["https_proxy"] = proxy
            env["HTTP_PROXY"] = env["http_proxy"] = proxy

        log_file = _log_path(tenant.data_dir)
        _rotate_bridge_log(log_file)
        with open(log_file, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [_node_bin(), str(ROOT / "bridge" / "index.js")],
                cwd=str(ROOT), env=env,
                stdout=log, stderr=subprocess.STDOUT,
            )
        _procs[tenant.user_id] = proc
        _ports[tenant.user_id] = port
        _pids[tenant.user_id] = proc.pid
        _tokens[tenant.user_id] = token
        _enabled[tenant.user_id] = True
        _save_registry()

        logger.info("[bridge] 启动租户 %s 的 bridge: pid=%s port=%s proxy=%s",
                    tenant.user_id, proc.pid, port, proxy or "直连")

        # 等端口起来（最多 8 秒）
        for _ in range(32):
            if _alive(port):
                break
            if proc.poll() is not None:
                raise RuntimeError("bridge 启动失败，查看 data/logs/bridge.log")
            time.sleep(0.25)

        return {"online": _alive(port), "port": port, "pid": proc.pid, "started": True}


def stop(tenant_id: str, preserve_port: bool = False) -> None:
    with _lock:
        proc = _procs.pop(tenant_id, None)
        old_pid = _pids.pop(tenant_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif old_pid:
            _terminate_pid(old_pid)
        if not preserve_port:
            _ports.pop(tenant_id, None)
            _tokens.pop(tenant_id, None)
        _enabled.pop(tenant_id, None)
        _restarts.pop(tenant_id, None)
        _save_registry()


def restart(tenant: Tenant) -> dict:
    """重启指定租户的 bridge（用于代理等环境变量变更后立即生效）。"""
    with _lock:
        stop(tenant.user_id, preserve_port=True)
        return start(tenant)


def status(tenant: Optional[Tenant] = None) -> dict:
    tenant = tenant or tenancy.current()
    if tenant is None:
        return {"online": False, "port": None, "reason": "no tenant"}
    port = _ports.get(tenant.user_id)
    proc = _procs.get(tenant.user_id)
    pid = None
    running = False
    if proc is not None and proc.poll() is None:
        pid = proc.pid
        running = True
    else:
        saved_pid = _pids.get(tenant.user_id)
        if _pid_is_bridge(saved_pid):
            pid = saved_pid
            running = True
    online = bool(port) and _alive(port)
    return {
        "online": online,
        "port": port,
        "pid": pid,
        "running": running,
    }


def shutdown_all() -> None:
    for tenant_id in list(set(_procs) | set(_pids)):
        try:
            stop(tenant_id)
        except Exception:
            logger.exception("[bridge] 停止租户 %s 失败", tenant_id)


# ── 看门狗 ──────────────────────────────────────────────────────────────

def _tenant_for(user_id: str) -> Optional[Tenant]:
    import accounts

    user = accounts.get_user(user_id)
    if not user:
        return None
    return Tenant(user_id=user["id"], email=user["email"],
                  data_dir=Path(user["data_dir"]))


def reconcile() -> list:
    """把"用户主动启动过、但进程已挂掉"的 bridge 拉起来。

    崩溃重启有频率限制：10 分钟内超过 3 次就停止守护并记录日志，
    避免 Node 启动失败时无限重启。
    """
    results = []
    now = time.time()
    for tenant_id in list(_enabled):
        if not _enabled.get(tenant_id):
            continue
        port = _ports.get(tenant_id)
        if port and _alive(port):
            continue

        history = [t for t in _restarts.get(tenant_id, []) if now - t < _RESTART_WINDOW]
        if len(history) >= _MAX_RESTARTS:
            logger.error("[bridge] 租户 %s 10 分钟内重启 %d 次，停止自动拉起",
                         tenant_id, len(history))
            _enabled.pop(tenant_id, None)
            _save_registry()
            results.append({"tenant": tenant_id, "action": "disabled"})
            continue

        tenant = _tenant_for(tenant_id)
        if tenant is None:
            logger.warning("[bridge] 租户 %s 已不存在，清理", tenant_id)
            _enabled.pop(tenant_id, None)
            _ports.pop(tenant_id, None)
            _save_registry()
            continue

        history.append(now)
        _restarts[tenant_id] = history
        try:
            status = start(tenant)
            logger.info("[bridge] 看门狗重启租户 %s: port=%s", tenant_id, status.get("port"))
            results.append({"tenant": tenant_id, "action": "restarted", **status})
        except Exception as exc:
            logger.error("[bridge] 重启租户 %s 失败: %s", tenant_id, exc)
            results.append({"tenant": tenant_id, "action": "failed", "error": str(exc)})
    return results


def start_watchdog(interval: int = 60) -> None:
    """后台线程：每 interval 秒检查一次已启用的 bridge。"""
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return

    def loop():
        while True:
            try:
                reconcile()
            except Exception:
                logger.exception("[bridge] 看门狗执行失败")
            time.sleep(interval)

    _watchdog = threading.Thread(target=loop, daemon=True, name="bridge-watchdog")
    _watchdog.start()
    logger.info("[bridge] 看门狗已启动（每 %ss 检查）", interval)
