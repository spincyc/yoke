#pragma once
#include <stdint.h>
extern int64_t stub_time_us;   /* what esp_timer_get_time returns */
int64_t esp_timer_get_time(void);
