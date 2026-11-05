/* Generic host stand-ins for the idf/ headers: one thread, a settable clock, queues
 * that never block, tasks recorded but never run. */
#include <stdlib.h>
#include <string.h>
#include "esp_err.h"
#include "esp_random.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

const char *esp_err_to_name(esp_err_t err) { return err == ESP_OK ? "ESP_OK" : "error"; }

int64_t stub_time_us;
int64_t esp_timer_get_time(void) { return stub_time_us; }

uint32_t esp_random(void) { return (uint32_t)rand(); }

unsigned stub_wdt_resets;
void (*stub_wdt_hook)(void);
esp_err_t esp_task_wdt_add(void *task)
{
    (void)task;
    return ESP_OK;
}
esp_err_t esp_task_wdt_reset(void)
{
    stub_wdt_resets++;
    if (stub_wdt_hook) stub_wdt_hook();
    return ESP_OK;
}

esp_err_t esp_vfs_eventfd_register(const esp_vfs_eventfd_config_t *config)
{
    (void)config;
    return ESP_OK;
}

int stub_eventfd_fd = -1;
#undef eventfd
int stub_eventfd(unsigned initial, int flags)
{
    return stub_eventfd_fd = eventfd(initial, flags | EFD_NONBLOCK);
}

stub_task_t stub_tasks[4];
unsigned stub_task_count, stub_delays;
BaseType_t xTaskCreatePinnedToCore(void (*fn)(void *), const char *name, uint32_t stack,
                                   void *arg, UBaseType_t priority, TaskHandle_t *handle,
                                   BaseType_t core)
{
    (void)stack; (void)arg; (void)priority; (void)handle; (void)core;
    if (stub_task_count == 4) return pdFALSE;
    stub_tasks[stub_task_count++] = (stub_task_t){fn, name};
    return pdPASS;
}
void vTaskDelay(TickType_t ticks)
{
    (void)ticks;
    stub_delays++;
}
UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t task)
{
    (void)task;
    return 0;
}

struct stub_queue {
    UBaseType_t length, size, head, count;
    unsigned char items[];
};
QueueHandle_t xQueueCreate(UBaseType_t length, UBaseType_t size)
{
    QueueHandle_t q = calloc(1, sizeof(*q) + (size_t)length * size);
    if (q) *q = (struct stub_queue){.length = length, .size = size};
    return q;
}
BaseType_t xQueueSend(QueueHandle_t q, const void *item, TickType_t ticks)
{
    (void)ticks;
    if (q->count == q->length) return pdFALSE;
    memcpy(q->items + (size_t)((q->head + q->count++) % q->length) * q->size, item, q->size);
    return pdTRUE;
}
BaseType_t xQueueReceive(QueueHandle_t q, void *item, TickType_t ticks)
{
    (void)ticks;
    if (!q->count) return pdFALSE;
    memcpy(item, q->items + (size_t)q->head * q->size, q->size);
    q->head = (q->head + 1) % q->length;
    q->count--;
    return pdTRUE;
}

struct stub_mutex {
    int unused;
};
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return calloc(1, sizeof(struct stub_mutex)); }
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t ticks)
{
    (void)mutex; (void)ticks;
    return pdTRUE;
}
BaseType_t xSemaphoreGive(SemaphoreHandle_t mutex)
{
    (void)mutex;
    return pdTRUE;
}
