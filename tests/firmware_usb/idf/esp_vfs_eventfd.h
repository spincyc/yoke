#pragma once
#include <sys/eventfd.h>
#include "esp_err.h"
typedef struct { int max_fds; } esp_vfs_eventfd_config_t;
#define ESP_VFS_EVENTD_CONFIG_DEFAULT() {.max_fds = 5}
esp_err_t esp_vfs_eventfd_register(const esp_vfs_eventfd_config_t *config);
/* A real, nonblocking eventfd whose number the test reads back. */
extern int stub_eventfd_fd;
int stub_eventfd(unsigned initial, int flags);
#define eventfd stub_eventfd
