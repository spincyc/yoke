#pragma once
#include "esp_err.h"
extern unsigned stub_wdt_resets;
extern void (*stub_wdt_hook)(void);   /* run by every reset, e.g. to end a task loop */
esp_err_t esp_task_wdt_add(void *task);
esp_err_t esp_task_wdt_reset(void);
