"""

Polymarket CLOB /v1/heartbeats session semantics (native live stack).



First POST uses heartbeat_id \"\". The venue returns (or supplies on 400) the active

session id; subsequent POSTs must use that exact string until the server rotates it again.



Note: published OpenAPI often shows 200 bodies as only ``{\"status\":\"ok\"}``; production

may omit ``heartbeat_id`` on 200. In that case the client keeps sending \"\" until the venue

returns a session id on a 400, then persists it for later ticks.

"""



from __future__ import annotations



import logging

from typing import Any



from py_clob_client_v2.exceptions import PolyApiException



from tyrex_pm.runtime.health_runtime import HealthRuntime



log = logging.getLogger(__name__)



HEARTBEAT_RECOVER_MAX_ATTEMPTS = 8



_ID_KEYS = ("heartbeat_id", "heartbeatId", "session_id", "sessionId")





def _extract_id_shallow(obj: dict[str, Any]) -> str | None:

    for key in _ID_KEYS:

        raw = obj.get(key)

        if isinstance(raw, str) and raw.strip():

            return raw.strip()

    return None





def _extract_id_nested(obj: dict[str, Any]) -> str | None:

    got = _extract_id_shallow(obj)

    if got:

        return got

    for nest in ("data", "result", "error"):

        inner = obj.get(nest)

        if isinstance(inner, dict):

            got = _extract_id_shallow(inner)

            if got:

                return got

    return None





def parse_heartbeat_id_from_success_body(body: Any) -> str | None:

    if not isinstance(body, dict):

        return None

    return _extract_id_nested(body)





def parse_heartbeat_id_from_error_body(error_msg: Any) -> str | None:

    if not isinstance(error_msg, dict):

        return None

    return _extract_id_nested(error_msg)





def apply_heartbeat_success(health: HealthRuntime, body: Any) -> None:

    parsed = parse_heartbeat_id_from_success_body(body)

    if parsed is not None:

        health.clob_heartbeat_id_next = parsed





async def post_heartbeat_with_recovery(health: HealthRuntime, bridge: Any) -> bool:

    """

    Send one logical heartbeat tick, rotating session id from success/400 bodies as needed.

    Returns True if the tick ended in HTTP success after any in-tick retries.

    Serialized per HealthRuntime so two callers cannot interleave POSTs for the same session.

    """

    async with health._heartbeat_send_lock:

        attempts = 0

        while attempts < HEARTBEAT_RECOVER_MAX_ATTEMPTS:

            attempts += 1

            to_send = "" if health.clob_heartbeat_id_next is None else health.clob_heartbeat_id_next

            try:

                resp = await bridge.post_heartbeat(to_send)

                apply_heartbeat_success(health, resp)

                return True

            except PolyApiException as e:

                repl = parse_heartbeat_id_from_error_body(e.error_msg)

                if e.status_code != 400 or repl is None:

                    log.warning("CLOB heartbeat failed HTTP %s: %s", e.status_code, e.error_msg)

                    return False

                health.clob_heartbeat_id_next = repl

                log.info("CLOB heartbeat: server returned replacement id on 400, retrying")

                continue

            except Exception:

                log.exception("CLOB heartbeat failed")

                return False

        log.error("CLOB heartbeat: exhausted recovery attempts (%s)", HEARTBEAT_RECOVER_MAX_ATTEMPTS)

        health.clob_heartbeat_id_next = None

        return False


