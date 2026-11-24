#pragma once
#include <stdint.h>
#include "esp_err.h"

typedef struct stub_timer *esp_timer_handle_t;
typedef struct {
    void (*callback)(void *arg);
    void *arg;
    const char *name;
} esp_timer_create_args_t;

extern int64_t stub_time_us;   /* what esp_timer_get_time returns */
int64_t esp_timer_get_time(void);
esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out);
esp_err_t esp_timer_start_once(esp_timer_handle_t timer, uint64_t timeout_us);
esp_err_t esp_timer_stop(esp_timer_handle_t timer);
