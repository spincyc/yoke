#pragma once
#include <stddef.h>
#include "freertos/FreeRTOS.h"
typedef struct stub_queue *QueueHandle_t;   /* a ring that never blocks */
QueueHandle_t xQueueCreate(UBaseType_t length, UBaseType_t size);
BaseType_t xQueueSend(QueueHandle_t queue, const void *item, TickType_t ticks);
BaseType_t xQueueReceive(QueueHandle_t queue, void *item, TickType_t ticks);
