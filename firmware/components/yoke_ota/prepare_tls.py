#!/usr/bin/env python3
"""Create a build-local, exact-source-checked ESP-IDF handshake override.

The SDK checkout stays unchanged. Updating the pinned SDK requires reviewing
this adapter, updating its hash, and re-running the real-library deadline test.
"""

import argparse
import hashlib
from pathlib import Path

EXPECTED_SHA256 = "5893293732ba4a73990bb9beb048fdf95e9c8931cb8f6acb43fde46590c54735"
START = (
    "int esp_mbedtls_server_session_create(esp_tls_cfg_server_t *cfg, "
    "int sockfd, esp_tls_t *tls)"
)
END = "\n/**\n * @brief      ESP-TLS server session initialization"
REPLACEMENT = """
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "handshake_deadline.h"

static int yoke_tls_step(void *context)
{
    return esp_mbedtls_server_session_continue_async(context);
}

/* Applications may feed a watchdog while a bounded handshake waits. */
void __attribute__((weak)) yoke_tls_handshake_tick(void)
{
}

static void yoke_tls_wait(void)
{
    yoke_tls_handshake_tick();
    vTaskDelay(1);
}

int esp_mbedtls_server_session_create(
    esp_tls_cfg_server_t *cfg, int sockfd, esp_tls_t *tls)
{
    int ret = esp_mbedtls_server_session_init(cfg, sockfd, tls);
    if (ret != 0) return ret;
    uint64_t timeout_ms = cfg->tls_handshake_timeout_ms ?
        cfg->tls_handshake_timeout_ms : ESP_TLS_DEFAULT_SERVER_HANDSHAKE_TIMEOUT_MS;
    ret = yoke_tls_handshake_deadline(
        sockfd, tls, yoke_tls_step, esp_tls_get_platform_time, yoke_tls_wait,
        timeout_ms * 1000, ESP_TLS_ERR_SSL_WANT_READ, ESP_TLS_ERR_SSL_WANT_WRITE,
        ESP_ERR_ESP_TLS_SERVER_HANDSHAKE_TIMEOUT);
    if (ret == 0) tls->conn_state = ESP_TLS_DONE;
    return ret;
}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    content = args.source.read_bytes()
    if hashlib.sha256(content).hexdigest() != EXPECTED_SHA256:
        parser.error(
            "ESP-TLS source differs from pinned ESP-IDF v5.5.6; review required"
        )
    source = content.decode()
    start = source.index(START)
    end = source.index(END, start)
    args.output.write_text(source[:start] + REPLACEMENT + source[end:])


if __name__ == "__main__":
    main()
