#pragma once
#include <stdio.h>
#define STUB_LOG(level, tag, format, ...) \
    fprintf(stderr, level " (%s) " format "\n", tag, ##__VA_ARGS__)
#define ESP_LOGE(tag, format, ...) STUB_LOG("E", tag, format, ##__VA_ARGS__)
#define ESP_LOGW(tag, format, ...) STUB_LOG("W", tag, format, ##__VA_ARGS__)
#define ESP_LOGI(tag, format, ...) STUB_LOG("I", tag, format, ##__VA_ARGS__)
#define ESP_LOGD(tag, format, ...) do { (void)(tag); } while (0)
