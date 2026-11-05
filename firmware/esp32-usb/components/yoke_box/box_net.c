/* The network task: the YWP/1 shell, and socket readiness for the role's
 * USB task (which alone touches its USB/IP core). A select() over the
 * control socket, the watched USB/IP sockets and an eventfd that other
 * tasks write to wake it. Subscribed to the task watchdog. */
#include <errno.h>
#include <string.h>
#include <sys/select.h>
#include <unistd.h>
#include "box_ywp.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define WATCHES 4
#define INBOX 4
#define TASK_STACK 8192
#define TASK_PRIORITY 4
#define WAKE_S 1.0         /* at least once a second: the watchdog */

typedef struct {
    int fd;                /* -1: free */
    bool read, write, armed;
    void (*post)(void (*fn)(void *), void *);
    void (*ready)(void *arg);
    void *arg;
} watch_t;

typedef struct {
    enum { ROUTE_DONE, POLL_DONE, ADDRESS } kind;
    bool completed;
    uint32_t tag;          /* POLL_DONE; ADDRESS: the new IPv4 */
    ywp_state_t state;
    char detail[BOX_DETAIL_MAX + 1];
} message_t;

static const char *TAG = "yoke-net";
static watch_t watches[WATCHES] = {{.fd = -1}, {.fd = -1}, {.fd = -1}, {.fd = -1}};
static SemaphoreHandle_t lock;
static QueueHandle_t inbox;
static int wake_fd = -1;
static const box_config_t *config;
static const box_bridge_t *role;
static uint32_t address;

static void wake(void)
{
    uint64_t one = 1;
    (void)write(wake_fd, &one, sizeof(one));
}

void box_net_watch(int fd, bool read, bool write, void (*post)(void (*fn)(void *), void *),
                   void (*ready)(void *arg), void *arg)
{
    const watch_t next = {fd, read, write, read || write, post, ready, arg};
    bool changed = false;
    xSemaphoreTake(lock, portMAX_DELAY);
    watch_t *slot = NULL;
    for (size_t i = 0; i < WATCHES; ++i) {
        if (watches[i].fd == fd) slot = &watches[i];
        if (!slot && watches[i].fd < 0) slot = &watches[i];
    }
    if (slot) {
        changed = slot->fd != fd || slot->read != read || slot->write != write ||
                  slot->armed != next.armed || slot->post != post || slot->ready != ready ||
                  slot->arg != arg;
        *slot = next;
    }
    xSemaphoreGive(lock);
    if (!slot) ESP_LOGE(TAG, "no watch slot for socket %d", fd);
    if (changed) wake(); /* the select() set is unchanged otherwise */
}

void box_net_unwatch(int fd)
{
    bool found = false;
    xSemaphoreTake(lock, portMAX_DELAY);
    for (size_t i = 0; i < WATCHES; ++i) {
        if (watches[i].fd != fd) continue;
        watches[i] = (watch_t){.fd = -1};
        found = true;
    }
    xSemaphoreGive(lock);
    if (found) wake();
}

static void deliver(message_t *m)
{
    (void)xQueueSend(inbox, m, portMAX_DELAY);
    wake();
}

void box_net_route_done(bool completed, ywp_state_t state, const char *detail)
{
    message_t m = {.kind = ROUTE_DONE, .completed = completed, .state = state};
    snprintf(m.detail, sizeof(m.detail), "%s", detail);
    deliver(&m);
}

void box_net_poll_done(uint32_t tag, ywp_state_t state, const char *detail)
{
    message_t m = {.kind = POLL_DONE, .tag = tag, .state = state};
    snprintf(m.detail, sizeof(m.detail), "%s", detail);
    deliver(&m);
}

static double now(void) { return (double)esp_timer_get_time() / 1e6; }

static void dispatch(const message_t *m)
{
    if (m->kind == ROUTE_DONE) box_ywp_route_done(m->completed, m->state, m->detail);
    else if (m->kind == POLL_DONE) box_ywp_poll_done(m->tag, m->state, m->detail);
    else box_ywp_address(m->tag);
}

static void task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    bool ywp = box_ywp_open(config, role, address) == ESP_OK;
    for (;;) {
        esp_task_wdt_reset();
        fd_set readable, writable;
        FD_ZERO(&readable);
        FD_ZERO(&writable);
        FD_SET(wake_fd, &readable);
        int top = wake_fd;
        int control = ywp ? box_ywp_fd() : -1; /* -1 until a failed reopen succeeds */
        if (control >= 0) {
            FD_SET(control, &readable);
            if (control > top) top = control;
        }
        xSemaphoreTake(lock, portMAX_DELAY);
        for (size_t i = 0; i < WATCHES; ++i) {
            if (!watches[i].armed) continue;
            if (watches[i].read) FD_SET(watches[i].fd, &readable);
            if (watches[i].write) FD_SET(watches[i].fd, &writable);
            if (watches[i].fd > top) top = watches[i].fd;
        }
        xSemaphoreGive(lock);
        double wait = ywp ? box_ywp_deadline() - now() : WAKE_S;
        if (wait > WAKE_S) wait = WAKE_S;
        if (wait < 0) wait = 0;
        struct timeval tv = {.tv_sec = 0, .tv_usec = (long)(wait * 1e6)};
        if (select(top + 1, &readable, &writable, NULL, &tv) < 0) {
            if (errno != EINTR) ESP_LOGE(TAG, "select failed: errno %d", errno);
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (FD_ISSET(wake_fd, &readable)) {
            uint64_t count;
            (void)read(wake_fd, &count, sizeof(count));
            for (message_t m; xQueueReceive(inbox, &m, 0) == pdTRUE;)
                if (ywp) dispatch(&m);
        }
        if (control >= 0 && FD_ISSET(control, &readable) && box_ywp_receive())
            vTaskDelay(1); /* a full batch: let lower-priority tasks and idle run */
        if (ywp) box_ywp_timers();
        /* Ready sockets go to the USB task, disarmed until it re-arms them. */
        watch_t due[WATCHES];
        size_t n = 0;
        xSemaphoreTake(lock, portMAX_DELAY);
        for (size_t i = 0; i < WATCHES; ++i) {
            watch_t *w = &watches[i];
            if (w->armed && ((w->read && FD_ISSET(w->fd, &readable)) ||
                             (w->write && FD_ISSET(w->fd, &writable)))) {
                w->armed = false;
                due[n++] = *w;
            }
        }
        xSemaphoreGive(lock);
        for (size_t i = 0; i < n; ++i) due[i].post(due[i].ready, due[i].arg);
    }
}

void box_net_address(uint32_t ipv4)
{
    message_t m = {.kind = ADDRESS, .tag = ipv4};
    deliver(&m);
}

esp_err_t box_net_init(void)
{
    const esp_vfs_eventfd_config_t eventfd_config = ESP_VFS_EVENTD_CONFIG_DEFAULT();
    esp_err_t err = esp_vfs_eventfd_register(&eventfd_config);
    if (err != ESP_OK) return err;
    wake_fd = eventfd(0, 0);
    lock = xSemaphoreCreateMutex();
    inbox = xQueueCreate(INBOX, sizeof(message_t));
    return wake_fd < 0 || !lock || !inbox ? ESP_ERR_NO_MEM : ESP_OK;
}

esp_err_t box_net_start(const box_config_t *c, const box_bridge_t *r, uint32_t ipv4)
{
    config = c;
    role = r;
    address = ipv4;
    return xTaskCreatePinnedToCore(task, "yoke-net", TASK_STACK, NULL, TASK_PRIORITY, NULL, 0) ==
                   pdPASS
               ? ESP_OK
               : ESP_ERR_NO_MEM;
}

void box_net_status(cJSON *body)
{
    cJSON *ywp = cJSON_AddObjectToObject(body, "ywp");
    if (ywp) box_ywp_status(ywp);
}
