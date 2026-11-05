#pragma once
/* A recording stand-in: every member added to any object is kept by key
 * (the last one wins), for the tests to read back. */
#include <stdbool.h>
typedef struct cJSON cJSON;
cJSON *cJSON_AddObjectToObject(cJSON *object, const char *name);
cJSON *cJSON_AddStringToObject(cJSON *object, const char *name, const char *value);
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *name, double value);
cJSON *cJSON_AddBoolToObject(cJSON *object, const char *name, bool value);
const char *stub_json_string(const char *name);   /* NULL: never added */
double stub_json_number(const char *name);         /* -1: never added */
