/* Optional integration check using the actual pinned mbedTLS library built
 * for the host. No firmware/device traffic; does not replace TLS bench tests. */
#define YOKE_OTA_HOST_TEST
#define YOKE_OTA_REAL_MBEDTLS
#include "../../firmware/components/yoke_ota/yoke_ota.c"
#include <assert.h>

static char *read_file(const char *path, size_t *size)
{
    FILE *stream = fopen(path, "rb");
    assert(stream);
    assert(fseek(stream, 0, SEEK_END) == 0);
    long bytes = ftell(stream);
    assert(bytes > 0 && bytes < 8192);
    rewind(stream);
    char *buffer = calloc(1, (size_t)bytes + 1);
    assert(buffer);
    assert(fread(buffer, 1, (size_t)bytes, stream) == (size_t)bytes);
    fclose(stream);
    *size = (size_t)bytes + 1;
    return buffer;
}

int main(int argc, char **argv)
{
    assert(argc == 4);
    server_cert = read_file(argv[1], &server_cert_len);
    server_key = read_file(argv[2], &server_key_len);
    client_cert = read_file(argv[3], &client_cert_len);
    esp_err_t result = validate_trust();
    free(server_cert); free(server_key); free(client_cert);
    return result == ESP_OK ? 0 : 1;
}
