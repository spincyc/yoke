#pragma once
#include "freertos/FreeRTOS.h"
typedef struct stub_mutex *SemaphoreHandle_t;   /* one thread: always free */
SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t ticks);
BaseType_t xSemaphoreGive(SemaphoreHandle_t mutex);
