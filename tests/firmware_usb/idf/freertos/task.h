#pragma once
#include "freertos/FreeRTOS.h"
typedef void *TaskHandle_t;
typedef struct {
    void (*fn)(void *arg);
    const char *name;
} stub_task_t;
extern stub_task_t stub_tasks[4];   /* created tasks, never run by the stub */
extern unsigned stub_task_count, stub_delays;
BaseType_t xTaskCreatePinnedToCore(void (*fn)(void *), const char *name, uint32_t stack,
                                   void *arg, UBaseType_t priority, TaskHandle_t *handle,
                                   BaseType_t core);
void vTaskDelay(TickType_t ticks);
UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t task);
