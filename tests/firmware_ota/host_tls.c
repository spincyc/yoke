/* Loopback-only mTLS admission test using the pinned mbedTLS implementation.
 * ESP-IDF config adapters and W5500 remain target/bench checks. */
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include "mbedtls/net_sockets.h"
#include "mbedtls/ssl.h"
#include "mbedtls/entropy.h"
#include "mbedtls/ctr_drbg.h"
#include "../../firmware/esp32/handshake_deadline.h"

static uint64_t now_us(void)
{
    struct timespec now;
    assert(clock_gettime(CLOCK_MONOTONIC, &now) == 0);
    return (uint64_t)now.tv_sec * 1000000 + now.tv_nsec / 1000;
}

static void wait_tick(void) { struct timespec tick = {0, 1000000}; nanosleep(&tick, NULL); }
static int handshake_step(void *context) { return mbedtls_ssl_handshake(context); }
static void *shutdown_after_deadline(void *context)
{
    /* Real blocked TLS I/O is interrupted from an independent thread. The
     * production esp_timer callback's disarm/generation policy is tested in
     * host_upload.c; this socket stays owned until after pthread_join. */
    usleep(200000);
    int fd = *(int *)context;
    shutdown(fd, SHUT_RDWR);
    return NULL;
}

int main(int argc, char **argv)
{
    assert(argc == 4 || argc == 5);
    bool upload = argc == 5 && !strcmp(argv[4], "upload");
    uint64_t timeout = argc == 5 && !upload ? strtoul(argv[4], NULL, 10) * 1000 : 10000000;
    mbedtls_x509_crt device, client;
    mbedtls_pk_context key;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context rng;
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config cfg;
    mbedtls_net_context listener, peer;
    mbedtls_x509_crt_init(&device); mbedtls_x509_crt_init(&client);
    mbedtls_pk_init(&key); mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&rng); mbedtls_ssl_init(&ssl);
    mbedtls_ssl_config_init(&cfg);
    mbedtls_net_init(&listener); mbedtls_net_init(&peer);
    assert(mbedtls_ctr_drbg_seed(&rng, mbedtls_entropy_func, &entropy, NULL, 0) == 0);
    assert(mbedtls_x509_crt_parse_file(&device, argv[1]) == 0);
    assert(mbedtls_pk_parse_keyfile(&key, argv[2], NULL, mbedtls_ctr_drbg_random, &rng) == 0);
    assert(mbedtls_x509_crt_parse_file(&client, argv[3]) == 0);
    assert(mbedtls_ssl_config_defaults(&cfg, MBEDTLS_SSL_IS_SERVER,
           MBEDTLS_SSL_TRANSPORT_STREAM, MBEDTLS_SSL_PRESET_DEFAULT) == 0);
    mbedtls_ssl_conf_authmode(&cfg, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_rng(&cfg, mbedtls_ctr_drbg_random, &rng);
    mbedtls_ssl_conf_ca_chain(&cfg, &client, NULL);
    assert(mbedtls_ssl_conf_own_cert(&cfg, &device, &key) == 0);
    assert(mbedtls_ssl_setup(&ssl, &cfg) == 0);
    assert(mbedtls_net_bind(&listener, "127.0.0.1", "0", MBEDTLS_NET_PROTO_TCP) == 0);
    struct sockaddr_in addr;
    socklen_t length = sizeof(addr);
    assert(getsockname(listener.fd, (struct sockaddr *)&addr, &length) == 0);
    printf("%u\n", (unsigned)ntohs(addr.sin_port)); fflush(stdout);
    assert(mbedtls_net_accept(&listener, &peer, NULL, 0, NULL) == 0);
    mbedtls_ssl_set_bio(&ssl, &peer, mbedtls_net_send, mbedtls_net_recv, NULL);
    int ret = yoke_tls_handshake_deadline(peer.fd, &ssl, handshake_step, now_us,
        wait_tick, timeout, MBEDTLS_ERR_SSL_WANT_READ, MBEDTLS_ERR_SSL_WANT_WRITE, -1);
    assert(!(fcntl(peer.fd, F_GETFL, 0) & O_NONBLOCK));
    if (!ret) ret = mbedtls_ssl_write(&ssl, (const unsigned char *)"ok", 2) == 2 ? 0 : 1;
    if (!ret && upload) {
        pthread_t watchdog;
        assert(pthread_create(&watchdog, NULL, shutdown_after_deadline, &peer.fd) == 0);
        unsigned char data[1000];
        ret = mbedtls_ssl_read(&ssl, data, sizeof(data));
        assert(pthread_join(watchdog, NULL) == 0);
        ret = ret <= 0 ? 1 : 0; /* shutdown must interrupt the incomplete record. */
    }
    mbedtls_net_free(&peer); mbedtls_net_free(&listener);
    mbedtls_ssl_free(&ssl); mbedtls_ssl_config_free(&cfg);
    mbedtls_x509_crt_free(&device); mbedtls_x509_crt_free(&client);
    mbedtls_pk_free(&key); mbedtls_ctr_drbg_free(&rng); mbedtls_entropy_free(&entropy);
    return ret ? 1 : 0;
}
