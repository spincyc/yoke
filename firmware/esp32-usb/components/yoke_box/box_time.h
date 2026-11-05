#pragma once
/* Pure rules of the box's wall clock, applied by box_clock.c (host-tested):
 * how far an operator may step it, and the synchronized reading kept in
 * RTC memory across software resets (the restored clock floor can be an
 * hour old after a watchdog reset; a power-on clears RTC memory). */
#include <stdbool.h>
#include <stdint.h>

#define BOX_TIME_STEP_MAX (366LL * 24 * 3600)  /* forward, without "force" */
#define BOX_TIME_KEPT_AHEAD (2LL * 3600)        /* floor saved hourly, plus slack */
#define BOX_TIME_KEPT_BEHIND 60LL               /* floor saved after the last keep */

typedef enum {
    BOX_TIME_OK,
    BOX_TIME_BACKWARDS, /* behind the clock: it never moves back */
    BOX_TIME_TOO_FAR,   /* more than BOX_TIME_STEP_MAX ahead: needs force */
} box_time_step_t;

box_time_step_t box_time_step(int64_t now_s, int64_t unix_s, bool force);

typedef struct {
    uint32_t magic, check;
    int64_t unix_s;
} box_time_kept_t;

/* Record a synchronized reading (each second while synchronized). */
void box_time_keep(box_time_kept_t *kept, int64_t unix_s);
/* At boot, after the floor was restored: the kept reading (never behind the
 * floor) when the reset kept RTC memory and the reading is plausible, else
 * -1. The record is consumed either way. */
int64_t box_time_restore(box_time_kept_t *kept, bool reset_kept_rtc, int64_t floor_s);
